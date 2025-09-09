-- Get partitions ids for specified batch
select id
from identifier(:tgt_catalog || '.' || :tgt_schema || '.' || :tgt_table || '_partitions')
where batch_id = :batch_id