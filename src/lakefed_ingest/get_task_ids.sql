-- Get task ids for specified job and task collection
-- Workflow dynamic value references for SQL output are limited to 1,000 rows and 48 KB
-- Convert array to comma separated string for use with workflow dynamic value references
select id
from identifier(:ctrl_catalog || '.' || :ctrl_schema || '.' || :ctrl_table)
where task_collection = :task_collection
and load_partitioned = :load_partitioned
and task_enabled is true