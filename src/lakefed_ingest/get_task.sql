-- Get task metadata
-- Workflow dynamic value references for SQL output are limited to 1,000 rows and 48 KB
select * from identifier(:ctrl_catalog || '.' || :ctrl_schema || '.' || :ctrl_table)
where id = :task_id
|> set sink_cluster_cols = case when array_size(sink_cluster_cols) > 0 then array_join(sink_cluster_cols, ', ') else null end