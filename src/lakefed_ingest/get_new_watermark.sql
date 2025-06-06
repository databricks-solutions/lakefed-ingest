declare or replace qry_str string;

-- Build query string using parameters and where_clause variable
set var qry_str = "select max(" || :watermark_col || ") as watermark from identifier(:src_catalog || '.' || :src_schema || '.' || :src_table) ";

execute immediate qry_str
using (
    :watermark_col AS watermark_col,
    :src_catalog AS src_catalog,
    :src_schema AS src_schema,
    :src_table AS src_table
);