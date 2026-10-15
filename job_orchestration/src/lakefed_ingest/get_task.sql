-- Get task metadata
-- Workflow dynamic value references for SQL output are limited to 1,000 rows and 48 KB
-- Convert arrays to comma separated string for use with workflow dynamic value references (Arrays are not a supported type)
select *
from identifier(:ctrl_catalog || '.' || :ctrl_schema || '.' || :ctrl_table)
where id = :task_id
|> set
     primary_key = case when array_size(primary_key) > 0 then array_join(primary_key, ',') else null end,
     sink_cluster_cols = case when array_size(sink_cluster_cols) > 0 then array_join(sink_cluster_cols, ',') else null end