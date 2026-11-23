-- Support Operations Trends: core metrics in SQL (PostgreSQL).
-- Assumes the tagged, cleaned data is loaded into:
--   tickets(ticket_id, submitted_at timestamp, closed_at timestamp, status, requester_department,
--           application, app_owner_department, title, description, priority, first_assigned_team,
--           resolver_team, reassignments int, records_impacted int, downtime_hours numeric, tag)

-- 1. Trending issues: last 30 days vs the rate in the 90 days before, by application + tag
WITH bounds AS (
    SELECT date_trunc('day', MAX(submitted_at)) + INTERVAL '1 day' AS as_of FROM tickets
),
counts AS (
    SELECT t.application, t.tag,
           COUNT(*) FILTER (WHERE t.submitted_at >= b.as_of - INTERVAL '30 days')            AS last_30d,
           COUNT(*) FILTER (WHERE t.submitted_at <  b.as_of - INTERVAL '30 days'
                              AND t.submitted_at >= b.as_of - INTERVAL '120 days')           AS prior_90d,
           SUM(t.records_impacted) FILTER (WHERE t.submitted_at >= b.as_of - INTERVAL '30 days') AS records_30d,
           COUNT(DISTINCT t.requester_department)
                 FILTER (WHERE t.submitted_at >= b.as_of - INTERVAL '30 days')            AS depts_reporting
    FROM tickets t CROSS JOIN bounds b
    GROUP BY t.application, t.tag
)
SELECT application, tag, last_30d,
       ROUND(prior_90d / 3.0, 1)                                 AS expected_30d,
       ROUND((last_30d + 1) / (prior_90d / 3.0 + 1), 2)          AS lift,
       records_30d, depts_reporting,
       (last_30d >= 8 AND (last_30d + 1) / (prior_90d / 3.0 + 1) >= 3) AS is_trending
FROM counts
ORDER BY lift DESC
LIMIT 10;

-- 2. Triangulate the source: one application, many reporting departments, concentrated in time
SELECT application, tag,
       COUNT(*)                                                        AS tickets,
       COUNT(DISTINCT requester_department)                            AS departments_reporting,
       ROUND(100.0 * AVG((EXTRACT(DAY FROM submitted_at) >= 25)::int), 0) AS pct_at_month_end
FROM tickets
GROUP BY application, tag
HAVING COUNT(DISTINCT requester_department) >= 4
ORDER BY pct_at_month_end DESC
LIMIT 5;

-- 3. Application health report: monthly tickets, records impacted, and downtime
SELECT date_trunc('month', submitted_at)::date AS month,
       application,
       COUNT(*)                                AS tickets,
       SUM(records_impacted)                   AS records_impacted,
       ROUND(SUM(downtime_hours), 1)           AS downtime_hours
FROM tickets
GROUP BY 1, 2
ORDER BY 1, downtime_hours DESC;

-- 4. Legal, Finance and HR: what each issue type needs to reach a resolution
SELECT requester_department,
       tag,
       COUNT(*)                                                       AS tickets,
       ROUND(percentile_cont(0.5) WITHIN GROUP (
             ORDER BY EXTRACT(EPOCH FROM closed_at - submitted_at) / 86400)::numeric, 2) AS median_days,
       SUM(records_impacted)                                          AS records_impacted,
       ROUND(100.0 * AVG((reassignments > 0)::int), 0)                AS pct_reassigned,
       MODE() WITHIN GROUP (ORDER BY resolver_team)                   AS resolving_team
FROM tickets
WHERE requester_department IN ('Legal', 'Finance', 'HR')
  AND closed_at IS NOT NULL
GROUP BY requester_department, tag
ORDER BY requester_department, tickets DESC;

-- 5. Routing: downtime lost to tickets that went to the wrong team first
WITH by_route AS (
    SELECT tag,
           AVG(downtime_hours) FILTER (WHERE reassignments = 0) AS avg_down_direct,
           AVG(downtime_hours) FILTER (WHERE reassignments > 0) AS avg_down_reassigned,
           COUNT(*)            FILTER (WHERE reassignments > 0) AS reassigned_tickets
    FROM tickets
    GROUP BY tag
)
SELECT tag, reassigned_tickets,
       ROUND(avg_down_direct, 1)                                                    AS avg_downtime_direct,
       ROUND(avg_down_reassigned, 1)                                                AS avg_downtime_reassigned,
       ROUND(GREATEST(avg_down_reassigned - avg_down_direct, 0) * reassigned_tickets) AS avoidable_downtime_hours
FROM by_route
ORDER BY avoidable_downtime_hours DESC NULLS LAST;
