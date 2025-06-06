update identifier(:ctrl_catalog || '.' || :ctrl_schema || '.' || :ctrl_table)
set watermark_col_start_value = :watermark_col_end_value
where id = :task_id;