declare or replace qry_str string;

create catalog if not exists identifier(:tgt_catalog);
use catalog identifier(:tgt_catalog);
create schema if not exists identifier(:tgt_schema);
use schema identifier(:tgt_schema);

-- Create sink table using schema inferred from foreign table
set var qry_str = 
  "create table if not exists " || :tgt_table || " as "
  "select " || :select_list || " from " || :src_catalog || '.' || :src_schema || '.' || :src_table || " "  
  "where 1 = 0";

execute immediate qry_str;