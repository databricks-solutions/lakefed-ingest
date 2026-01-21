-- Create view of source table in order to infer the schema
-- Result of describe extended is returned to the job for use downstream
declare or replace qry_str string;

set var qry_str = 
  "create or replace temp view vw_src as "
  "select " || :select_list || " from " || :src_catalog || '.' || :src_schema || '.' || :src_table || " "  
  "where 1 = 0";

execute immediate qry_str;

describe extended vw_src as json;