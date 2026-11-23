"""
Generate a synthetic support-operations ticket dataset.

Every app, team, number and ticket here is invented for Wazobia Corp, a fictional company.
No real company data is used.

Tickets come from business departments (Legal, Finance, HR, Sales, Operations,
Engineering) about the internal applications they rely on. Each ticket has a title
and a free-text description, the way users actually write them. Realistic patterns
are planted so the analysis has something real to find:

  1. A June release breaks Contract Hub's e-signature connector: integration failures
     and impacted contract records spike until a patch in August.
  2. Expense Portal slows down every month-end. It is reported by every department,
     but the source is a single application.
  3. People Portal approvals get stuck during benefits open enrollment (October).
  4. Invoice Center starts creating duplicate invoice records in late September, and it is still growing.
  5. Tickets triaged to the wrong team get reassigned, which adds days of downtime.

Usage (from data/):  python generate_synthetic_tickets.py  -> tickets_synthetic.csv
"""
import csv
import math
import random
from datetime import datetime, timedelta

random.seed(7)

START = datetime(2025, 11, 3)
END = datetime(2026, 10, 30)

DEPTS = ["Legal", "Finance", "HR", "Sales", "Operations", "Engineering"]

# application -> (owning department, requester department weights)
APPS = {
    "Contract Hub":      ("Legal",      {"Legal": 70, "Sales": 25, "Operations": 5}),
    "Matter Tracker":    ("Legal",      {"Legal": 100}),
    "Expense Portal":    ("Finance",    {"Legal": 12, "Finance": 18, "HR": 10, "Sales": 30, "Operations": 15, "Engineering": 15}),
    "Invoice Center":    ("Finance",    {"Finance": 80, "Operations": 20}),
    "People Portal":     ("HR",         {"HR": 35, "Legal": 8, "Finance": 10, "Sales": 20, "Operations": 12, "Engineering": 15}),
    "Quote Builder":     ("Sales",      {"Sales": 85, "Finance": 15}),
    "Vendor Onboarding": ("Operations", {"Operations": 50, "Legal": 20, "Finance": 30}),
}

TAGS = ["Access & Permissions", "Data / Record Error", "Integration Failure", "Performance / Slowness",
        "Workflow Stuck / Approval", "Report / Dashboard", "Change Request", "How-to / Training"]

# base tickets per week, by app, in TAGS order
BASE = {
    "Contract Hub":      [2.0, 1.0, 0.8, 0.3, 1.0, 0.4, 0.6, 0.8],
    "Matter Tracker":    [1.0, 0.5, 0.1, 0.2, 0.5, 0.5, 0.4, 0.5],
    "Expense Portal":    [1.0, 1.0, 0.3, 0.5, 1.2, 0.3, 0.3, 1.5],
    "Invoice Center":    [0.6, 0.8, 0.5, 0.3, 0.6, 0.6, 0.3, 0.4],
    "People Portal":     [1.2, 0.6, 0.3, 0.3, 1.0, 0.4, 0.4, 1.2],
    "Quote Builder":     [1.0, 1.0, 0.5, 0.4, 0.6, 0.6, 0.7, 0.6],
    "Vendor Onboarding": [0.6, 0.6, 0.4, 0.2, 1.0, 0.3, 0.3, 0.5],
}

# tag -> team that can actually resolve it, median days when routed correctly,
#        chance triage routes it right first time, share of open time the user is blocked
TAG_PROFILE = {
    "Access & Permissions":      ("Identity & Access",         0.5, 0.85, 0.6),
    "Data / Record Error":       ("Application Support",       2.0, 0.50, 0.4),
    "Integration Failure":       ("Integrations Engineering",  2.5, 0.30, 1.0),
    "Performance / Slowness":    ("Platform Engineering",      1.2, 0.40, 1.0),
    "Workflow Stuck / Approval": ("Application Support",       1.0, 0.55, 0.8),
    "Report / Dashboard":        ("Data & Analytics",          2.5, 0.65, 0.1),
    "Change Request":            ("Application Development",   6.0, 0.75, 0.0),
    "How-to / Training":         ("Service Desk",              0.3, 1.00, 0.1),
}
WRONG_TEAMS = ["Service Desk", "Application Support", "Identity & Access", "Platform Engineering"]

# Titles are short and often vague; the description carries most of the signal.
# Some entries deliberately avoid keywords so the LLM fallback has work to do.
TEMPLATES = {
    "Access & Permissions": [
        ("Can't open {app}", "Getting a permission denied message when I open {app}. I had access last week."),
        ("Need access to {app}", "Starting on the {dept} team and need a role in {app} to do my job."),
        ("Locked out", "Locked out of {app} after the password change, SSO keeps looping."),
        ("Role missing", "My {app} role seems to have been removed, I can't see my team's queue."),
    ],
    "Data / Record Error": [
        ("Wrong amount on record", "{app} shows the wrong amount on {n} records, does not match the source."),
        ("Duplicate records", "Seeing duplicate entries in {app} for the same item, {n} records affected."),
        ("Record missing", "A record I created in {app} is missing from the list. {n} records seem affected."),
        ("Numbers look off", "Values in {app} look off for {n} items since yesterday."),
    ],
    "Integration Failure": [
        ("{app} not syncing", "{app} sync to the downstream system failed for {n} records, error 502 returned."),
        ("Integration error", "Getting an integration error in {app}; {n} records failed to send."),
        ("Connector failing", "The {app} connector keeps failing with a timeout from the API. {n} records stuck."),
        ("Nothing went through", "Submitted {n} items in {app} this morning and none of them showed up on the other side."),
    ],
    "Performance / Slowness": [
        ("{app} very slow", "{app} pages take over a minute to load, keeps spinning."),
        ("Timing out", "{app} is timing out when I try to submit."),
        ("Lagging", "Everything in {app} is lagging badly today."),
        ("Can't get anything done", "{app} is unusable this afternoon, whole team affected."),
    ],
    "Workflow Stuck / Approval": [
        ("Approval stuck", "My request in {app} has been stuck in approval for 3 days, approver says they never got it."),
        ("Request not moving", "Workflow in {app} is not moving past the review step for {n} items."),
        ("Pending forever", "{app} item still pending, nobody can see it in their queue."),
        ("Where did my request go", "Sent something through {app} on Monday and it just disappeared from my view."),
    ],
    "Report / Dashboard": [
        ("Report not loading", "The {app} report won't load, need it for month-end."),
        ("Dashboard numbers wrong", "The {app} dashboard totals don't match what's in the records."),
        ("Export broken", "Export to spreadsheet from {app} gives an empty file."),
    ],
    "Change Request": [
        ("Add a field", "Can we add a field to the {app} form for the {dept} team? Change request."),
        ("New option needed", "Need a new option in the {app} dropdown for a process change."),
        ("Enhancement request", "Enhancement: let {app} send a reminder before the due date."),
    ],
    "How-to / Training": [
        ("How do I submit", "How do I submit a new request in {app}?"),
        ("Training on {app}", "Is there training or a walkthrough for {app}?"),
        ("Quick question", "Quick question about {app}, who should I talk to?"),
    ],
}

# planted-pattern descriptions (replace the generic text during the pattern window)
SPECIAL = {
    "esign": [
        ("Contracts not going out for signature",
         "Since the June release, Contract Hub sends to the e-signature service are failing. {n} contracts stuck, sync error."),
        ("E-signature integration down",
         "Contract Hub integration with e-signature is failing again, {n} contract records not sent to counterparties."),
    ],
    "enrollment": [
        ("Benefits enrollment approval stuck",
         "My benefits enrollment in People Portal is stuck in approval, deadline is Friday."),
        ("Enrollment pending",
         "People Portal enrollment request pending for days, workflow not moving to HR review."),
    ],
    "duplicate_invoice": [
        ("Duplicate invoices",
         "Invoice Center created duplicate invoice records for {n} vendors after import."),
        ("Paid twice?",
         "Invoice Center shows the same invoice twice for {n} vendors, duplicate record risk of double payment."),
    ],
}


def poisson(lam):
    if lam <= 0:
        return 0
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def multiplier(app, tag, day):
    m = {12: 0.75, 7: 0.85, 1: 1.05, 9: 1.05}.get(day.month, 1.0)
    if app == "Contract Hub" and tag == "Integration Failure" and datetime(2026, 6, 15) <= day < datetime(2026, 8, 10):
        m *= 7
    if app == "Expense Portal" and tag == "Performance / Slowness" and day.day >= 25:
        m *= 6
    if app == "People Portal" and tag == "Workflow Stuck / Approval" and day.month == 10:
        m *= 5
    if app == "Invoice Center" and tag == "Data / Record Error" and day >= datetime(2026, 9, 21):
        m *= 2 + (day - datetime(2026, 9, 21)).days / 8  # still climbing
    return m


def special_key(app, tag, day):
    if app == "Contract Hub" and tag == "Integration Failure" and datetime(2026, 6, 15) <= day < datetime(2026, 8, 10):
        return "esign"
    if app == "People Portal" and tag == "Workflow Stuck / Approval" and day.month == 10:
        return "enrollment"
    if app == "Invoice Center" and tag == "Data / Record Error" and day >= datetime(2026, 9, 21):
        return "duplicate_invoice"
    return None


def records_impacted(tag, key):
    if tag == "Integration Failure":
        return int(random.lognormvariate(3.2 if key else 2.3, 0.7))
    if tag == "Data / Record Error":
        return int(random.lognormvariate(2.6 if key else 1.8, 0.8))
    if tag == "Workflow Stuck / Approval":
        return int(random.lognormvariate(0.8, 0.7))
    return 0


rows, n = [], 50000
day = START
while day <= END:
    if day.weekday() < 5:
        for app, (owner, dept_w) in APPS.items():
            for tag, base in zip(TAGS, BASE[app]):
                for _ in range(poisson(base / 5 * multiplier(app, tag, day))):
                    key = special_key(app, tag, day)
                    dept = random.choices(list(dept_w), list(dept_w.values()))[0]
                    resolver, med, p_direct, block = TAG_PROFILE[tag]
                    priority = random.choices(["Low", "Medium", "High", "Critical"], [30, 45, 20, 5])[0]
                    if key or tag in ("Integration Failure", "Performance / Slowness"):
                        priority = random.choice(["High", "High", "Critical"])

                    reassign = 0 if random.random() < p_direct else 1 + (random.random() < 0.35) + (random.random() < 0.1)
                    if key == "esign" and day < datetime(2026, 7, 6):
                        reassign = max(reassign, 1)  # nobody knew it was the connector at first
                    first_team = resolver if reassign == 0 else random.choice([t for t in WRONG_TEAMS if t != resolver])

                    speed = {"Low": 1.4, "Medium": 1.0, "High": 0.7, "Critical": 0.45}[priority]
                    days = random.lognormvariate(0, 0.7) * med * speed + reassign * random.uniform(0.6, 1.4)
                    submitted = day.replace(hour=int(random.triangular(8, 18, 10)), minute=random.randint(0, 59))
                    closed = submitted + timedelta(days=days)
                    open_now = closed > END + timedelta(days=1)

                    recs = records_impacted(tag, key)
                    title, desc = random.choice(SPECIAL[key] if key else TEMPLATES[tag])
                    fmt = dict(app=app, dept=dept, n=max(recs, 1))
                    n += 1
                    rows.append({
                        "ticket_id": f"SUP-{n}",
                        "submitted_at": submitted.strftime("%Y-%m-%d %H:%M"),
                        "closed_at": "" if open_now else closed.strftime("%Y-%m-%d %H:%M"),
                        "status": "Open" if open_now else "Closed",
                        "requester_department": dept,
                        "application": app,
                        "app_owner_department": owner,
                        "title": title.format(**fmt),
                        "description": desc.format(**fmt),
                        "priority": priority,
                        "first_assigned_team": first_team,
                        "resolver_team": resolver,
                        "reassignments": reassign,
                        "records_impacted": recs,
                        "downtime_hours": round(min(days, 30) * 8 * block, 1),
                        "true_tag": tag,  # ground truth, used only to score the tagger
                    })
    day += timedelta(days=1)

rows.sort(key=lambda r: r["submitted_at"])
with open("tickets_synthetic.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
print(f"Wrote {len(rows)} tickets")
