create or replace view public.vw_pg_table_size
 as
 select
  table_schema,
  table_name,
  pg_table_size(quote_ident(table_name)),
  pg_size_pretty(pg_table_size(quote_ident(table_name))) as pg_table_size_pretty
from information_schema.tables
where table_schema not in ('pg_catalog', 'information_schema')
and table_type = 'BASE TABLE';