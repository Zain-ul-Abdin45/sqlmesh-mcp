MODEL (
  name sqlmesh_example.incremental_model,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  ),
  start '2020-01-01',
  cron '@daily',
  grain (id, event_date)
);

SELECT
  id,
  item_id,
  event_date,
  -- Synthetic PII for the dataveil demo (not real data): a fabricated
  -- customer email, one per row. Lets profile_model/classify flag this
  -- column and propose_cleansing_plan/apply_cleansing_plan mask it.
  'user' || CAST(id AS VARCHAR) || '@example.com' AS customer_email,
FROM
  sqlmesh_example.seed_model
WHERE
  event_date BETWEEN @start_date AND @end_date
  