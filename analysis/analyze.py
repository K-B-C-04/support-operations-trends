"""
Support Operations Trends: offline replica of the Sheets pipeline.

  clean -> tag (keyword rules on title + description) -> trending issues
        -> triangulate the source -> app health -> department resolution view
        -> routing and downtime -> charts in images/

In production (apps-script/Code.gs) tickets the rules can't tag go to Gemini. Here
we score the rules against the dataset's ground-truth tags, then use ground truth
for the leftovers so the charts reflect the full picture.

Usage (from the repo root):  python analysis/analyze.py
"""
import csv
import json
import re
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "images"
IMG.mkdir(exist_ok=True)

INK, MUTED, GRID = "#1f2937", "#6b7280", "#e5e7eb"
ACCENT, ALERT, CALM, WARN = "#2563eb", "#dc2626", "#cbd5e1", "#f59e0b"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
    "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.spines.top": False, "axes.spines.right": False,
})


def save(fig, name):
    fig.savefig(IMG / name, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def title(ax, text, sub=None):
    ax.set_title(text, loc="left", color=INK, fontweight="bold", pad=18 if sub else 8)
    if sub:
        ax.text(0, 1.02, sub, transform=ax.transAxes, color=MUTED, fontsize=9)


# ------------------------------------------------------------------ 1. Clean
df = pd.read_csv(ROOT / "data" / "tickets_synthetic.csv", parse_dates=["submitted_at", "closed_at"])
as_of = df["submitted_at"].max().normalize() + pd.Timedelta(days=1)
df["year_month"] = df["submitted_at"].dt.to_period("M")
df["days_to_close"] = (df["closed_at"] - df["submitted_at"]).dt.total_seconds() / 86400
df["text"] = (df["title"] + " " + df["description"]).str.lower()

# ------------------------------------------------------------------- 2. Tag
rules = list(csv.DictReader(open(ROOT / "data" / "taxonomy.csv")))
for r in rules:
    r["kw"] = [k.strip() for k in r["keywords"].split("|")]


def tag(text):
    for r in rules:
        if any(k in text for k in r["kw"]):
            return r["tag"]
    return None


df["tag"] = df["text"].map(tag)
ruled = df["tag"].notna()
coverage = ruled.mean()
accuracy = (df.loc[ruled, "tag"] == df.loc[ruled, "true_tag"]).mean()
df["tag"] = df["tag"].fillna(df["true_tag"])  # stands in for the Gemini pass

# ------------------------------------------------- 3. Trending issues (window)
WINDOW, BASELINE = 30, 90
recent = df[df["submitted_at"] >= as_of - pd.Timedelta(days=WINDOW)]
base = df[(df["submitted_at"] < as_of - pd.Timedelta(days=WINDOW)) &
          (df["submitted_at"] >= as_of - pd.Timedelta(days=WINDOW + BASELINE))]
pair = (recent.groupby(["application", "tag"]).size().rename("last_30d").to_frame()
        .join(base.groupby(["application", "tag"]).size().rename("prior_90d"), how="outer").fillna(0))
pair["expected_30d"] = pair["prior_90d"] * WINDOW / BASELINE
pair["lift"] = (pair["last_30d"] + 1) / (pair["expected_30d"] + 1)  # +1 smoothing for small counts
pair["trending"] = (pair["last_30d"] >= 8) & (pair["lift"] >= 3)
trending = pair[pair["trending"]].sort_values("lift", ascending=False)

# keyword view: which description terms jumped in the window
STOP = set("""a an the to in of for and on is it my i me we our has have had been be was were this that
with from not no can can't cannot does doesn't don't won't keeps keep since after before when what who
who's who are at as by or but so if up out get getting seem seems same just still about some all any
records record items item days day today yesterday week monday friday morning afternoon again team shows moving gives
""".split()) | {a.lower() for a in df["application"].unique() for a in a.split()}


def terms(texts):
    c = Counter()
    for t in texts:
        c.update({w for w in re.findall(r"[a-z][a-z\-]+", t) if w not in STOP and len(w) > 2})
    return c


t_recent, t_base = terms(recent["text"]), terms(base["text"])
kw = pd.DataFrame({"last_30d": pd.Series(t_recent), "prior_90d": pd.Series(t_base)}).fillna(0)
kw["lift"] = (kw["last_30d"] + 1) / (kw["prior_90d"] * WINDOW / BASELINE + 1)
kw_trending = kw[kw["last_30d"] >= 10].sort_values("lift", ascending=False).head(8)

# ------------------------------------------------ 4. Triangulate the source
exp_perf = df[(df["application"] == "Expense Portal") & (df["tag"] == "Performance / Slowness")]
month_end_share = (exp_perf["submitted_at"].dt.day >= 25).mean()
exp_perf_depts = exp_perf["requester_department"].nunique()

esign = df[(df["application"] == "Contract Hub") & (df["tag"] == "Integration Failure")]
esign_window = esign[(esign["submitted_at"] >= "2026-06-15") & (esign["submitted_at"] < "2026-08-10")]
ch_records = df.loc[df["application"] == "Contract Hub", "records_impacted"].sum()
esign_records_share = esign_window["records_impacted"].sum() / ch_records

# ---------------------------------------------------------- 5. App health
health = df.pivot_table(index="year_month", columns="application", values="downtime_hours",
                        aggfunc="sum").fillna(0)
app_now = recent.groupby("application").agg(tickets=("ticket_id", "count"),
                                            records=("records_impacted", "sum"),
                                            downtime=("downtime_hours", "sum"))
app_base = base.groupby("application")["downtime_hours"].sum() * WINDOW / BASELINE
app_now["downtime_vs_baseline"] = (app_now["downtime"] + 1) / (app_base.reindex(app_now.index).fillna(0) + 1)
app_now["status"] = np.select([app_now["downtime_vs_baseline"] >= 2, app_now["downtime_vs_baseline"] >= 1.3],
                              ["Degraded", "Watch"], "Healthy")

# --------------------------------------- 6. Department resolution (L/F/HR)
DEPTS3 = ["Legal", "Finance", "HR"]
tags_order = [r["tag"] for r in rules]
dept = df[df["requester_department"].isin(DEPTS3)]
dept_counts = dept.pivot_table(index="requester_department", columns="tag", values="ticket_id",
                               aggfunc="count").reindex(index=DEPTS3, columns=tags_order).fillna(0)
dept_days = dept.pivot_table(index="requester_department", columns="tag", values="days_to_close",
                             aggfunc="median").reindex(index=DEPTS3, columns=tags_order)
dept_summary = dept.groupby("requester_department").agg(
    tickets=("ticket_id", "count"), categories=("tag", "nunique"),
    median_days=("days_to_close", "median"), reassigned=("reassignments", lambda s: (s > 0).mean()),
).reindex(DEPTS3)

# ------------------------------------------------- 7. Routing and downtime
df["routed"] = np.where(df["reassignments"] > 0, "Reassigned", "Direct")
route = df[df["tag"] != "How-to / Training"].pivot_table(index="tag", columns="routed",
                                                          values="days_to_close", aggfunc="median")
route = route.sort_values("Reassigned")
dt = df.groupby(["tag", "routed"])["downtime_hours"].mean().unstack()
n_re = df[df["routed"] == "Reassigned"].groupby("tag").size()
avoidable_hours = ((dt["Reassigned"] - dt["Direct"]).clip(lower=0) * n_re).sum()
reassigned_share = (df["reassignments"] > 0).mean()

# ================================================================= Charts
# A. Trending issues: app + tag pairs, last 30 days vs expected
top = pair.sort_values("lift", ascending=False).head(8).iloc[::-1]
fig, ax = plt.subplots(figsize=(9, 4.6))
labels = [f"{a}  ·  {t}" for a, t in top.index]
y = np.arange(len(top))
ax.barh(y, top["expected_30d"], color=CALM, height=0.55, label="Expected (prior 90-day rate)")
ax.barh(y, top["last_30d"], color=[ALERT if t else ACCENT for t in top["trending"]], height=0.25,
        label="Last 30 days")
for i, (v, lift) in enumerate(zip(top["last_30d"], top["lift"])):
    ax.text(max(v, top["expected_30d"].iloc[i]) + 0.4, i, f"{lift:.1f}×", va="center", fontsize=9, color=INK)
ax.set_yticks(y, labels, fontsize=9)
ax.set_xlabel("Tickets")
ax.legend(frameon=False, loc="lower right", fontsize=8)
title(ax, "What's trending right now", f"Last {WINDOW} days vs the prior {BASELINE}-day rate · red = flagged (3× lift and 8+ tickets)")
save(fig, "trending_issues.png")

# A2. Trending keywords pulled from titles + descriptions
kt = kw_trending.iloc[::-1]
fig, ax = plt.subplots(figsize=(7, 3.6))
ax.barh(kt.index, kt["lift"], color=[ALERT if v >= 3 else ACCENT for v in kt["lift"]])
for i, (lift, c) in enumerate(zip(kt["lift"], kt["last_30d"])):
    ax.text(lift + 0.2, i, f"{lift:.1f}×  ({int(c)} mentions)", va="center", fontsize=8, color=MUTED)
ax.set_xlabel("Lift vs prior 90-day rate")
ax.set_xlim(right=kt["lift"].max() * 1.35)
title(ax, "Keywords surging in ticket text", f"Last {WINDOW} days · terms with 10+ mentions")
save(fig, "trending_keywords.png")

# B. App health: monthly downtime hours, small multiples
apps = health.sum().sort_values(ascending=False).index
fig, axes = plt.subplots(2, 4, figsize=(12, 5), sharey=True)
x = health.index.to_timestamp()
for ax, app in zip(axes.flat, apps):
    s = health[app]
    for other in apps:
        ax.plot(x, health[other], color=GRID, linewidth=0.8)
    hot = app_now.loc[app, "status"] if app in app_now.index else "Healthy"
    color = {"Degraded": ALERT, "Watch": WARN}.get(hot, ACCENT)
    ax.plot(x, s, color=color, linewidth=2.2)
    ax.set_title(f"{app}\n{hot}", loc="left", fontsize=9, color=INK)
    ax.tick_params(labelsize=7)
    ax.set_xticks([x[0], x[len(x) // 2], x[-1]], [d.strftime("%b %y") for d in (x[0], x[len(x) // 2], x[-1])])
axes.flat[-1].axis("off")
axes.flat[-1].text(0, 0.6, "Downtime hours per month\n(user hours blocked)\n\nStatus = last 30 days\nvs its own baseline",
                   color=MUTED, fontsize=9, va="center")
fig.suptitle("Application health", x=0.01, ha="left", fontweight="bold", color=INK)
fig.tight_layout()
save(fig, "app_health.png")

# C. Triangulation: Expense Portal slowness by day of month and department
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 3.8), gridspec_kw={"width_ratios": [2, 1]})
dom = exp_perf["submitted_at"].dt.day.value_counts().reindex(range(1, 32), fill_value=0)
ax1.bar(dom.index, dom.values, color=[ALERT if d >= 25 else CALM for d in dom.index])
ax1.set_xlabel("Day of month")
ax1.set_ylabel("Tickets")
title(ax1, "Expense Portal slowness clusters at month-end",
      f"{month_end_share:.0%} of tickets fall on day 25 or later")
dep = exp_perf["requester_department"].value_counts().iloc[::-1]
ax2.barh(dep.index, dep.values, color=ACCENT)
title(ax2, f"Reported by {exp_perf_depts} departments", "...but the source is one application")
save(fig, "triangulation.png")

# D. Department resolution heatmap (Legal, Finance, HR)
fig, ax = plt.subplots(figsize=(12, 3.6))
vals = dept_days.values.astype(float)
im = ax.imshow(np.nan_to_num(vals), cmap="Blues", aspect="auto", vmin=0, vmax=np.nanmax(vals))
for i in range(vals.shape[0]):
    for j in range(vals.shape[1]):
        n_t = int(dept_counts.values[i, j])
        if n_t:
            v = vals[i, j]
            ax.text(j, i, f"{v:.1f}d\n{n_t} tickets", ha="center", va="center", fontsize=8,
                    color="white" if v > np.nanmax(vals) * 0.55 else INK)
resolver = {r["tag"]: r["resolver_team"] for r in rules}
ax.set_xticks(range(len(tags_order)), [t.replace(" / ", " /\n").replace(" & ", " &\n") + "\n→ " + resolver[t].replace(" ", "\n", 1) for t in tags_order], fontsize=7.5)
ax.set_yticks(range(3), DEPTS3)
ax.tick_params(length=0)
title(ax, "What Legal, Finance and HR need to reach a resolution",
      "Median days to resolve by issue type · arrow = team that resolves it")
save(fig, "department_resolution.png")

# E. Routing: direct vs reassigned
fig, ax = plt.subplots(figsize=(8.5, 4))
yy = np.arange(len(route))
for i, (d, r) in enumerate(zip(route["Direct"], route["Reassigned"])):
    ax.plot([d, r], [i, i], color=GRID, linewidth=3, zorder=1)
ax.scatter(route["Direct"], yy, color=ACCENT, s=60, zorder=2, label="Routed to the right team first")
ax.scatter(route["Reassigned"], yy, color=ALERT, s=60, zorder=2, label="Reassigned at least once")
ax.set_yticks(yy, route.index, fontsize=9)
ax.set_xlabel("Median days to resolve")
ax.legend(frameon=False, fontsize=8, loc="lower right")
title(ax, "Every handoff adds days", f"{reassigned_share:.0%} of tickets were reassigned before reaching the right team")
save(fig, "routing.png")

# ================================================================= Summary
summary = {
    "tickets": int(len(df)),
    "period": f"{df['submitted_at'].min():%b %Y} to {df['submitted_at'].max():%b %Y}",
    "departments": int(df["requester_department"].nunique()),
    "applications": int(df["application"].nunique()),
    "rule_coverage_pct": round(coverage * 100, 1),
    "rule_accuracy_pct": round(accuracy * 100, 1),
    "expense_month_end_share_pct": round(month_end_share * 100),
    "expense_perf_departments": int(exp_perf_depts),
    "esign_tickets": int(len(esign_window)),
    "esign_records": int(esign_window["records_impacted"].sum()),
    "esign_share_of_contract_hub_records_pct": round(esign_records_share * 100),
    "esign_downtime_hours": round(float(esign_window["downtime_hours"].sum())),
    "reassigned_share_pct": round(reassigned_share * 100),
    "avoidable_downtime_hours": round(float(avoidable_hours)),
}
print(json.dumps(summary, indent=2))
print("\nTrending (app, tag):\n", trending.round(2))
print("\nTrending terms:\n", kw_trending.round(2))
print("\nApp health, last 30 days:\n", app_now.round(2))
print("\nDepartments:\n", dept_summary.round(2))
print("\nRouting medians:\n", route.round(2))
