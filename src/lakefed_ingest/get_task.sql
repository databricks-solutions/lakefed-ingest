-- Get task metadata
-- Workflow dynamic value references for SQL output are limited to 1,000 rows and 48 KB
select * from identifier(:ctrl_catalog || '.' || :ctrl_schema || '.' || :ctrl_table)
where id = :task_id