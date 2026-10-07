-- Create (idempotently) a Unity Catalog connection to a SQL Server instance for the governed
-- remote_query CT path. UC connections are NOT a DAB resource, so this is a parameterized helper
-- you run once per instance (on the warehouse) rather than part of `bundle deploy`.
--
-- The password is read from a Databricks secret scope via the secret() function — it is never
-- passed as a plaintext parameter. Put it first:
--   databricks secrets put-secret <sqlserver_scope> password
--
-- Parameters: connection_name, host, port, user, sqlserver_scope.

DECLARE OR REPLACE qry_str STRING;

SET VAR qry_str =
    'CREATE CONNECTION IF NOT EXISTS ' || :connection_name
    || ' TYPE sqlserver OPTIONS ('
    || 'host '  || chr(39) || :host || chr(39) || ', '
    || 'port '  || chr(39) || :port || chr(39) || ', '
    || 'user '  || chr(39) || :user || chr(39) || ', '
    || 'password secret(' || chr(39) || :sqlserver_scope || chr(39) || ', ' || chr(39) || 'password' || chr(39) || '))';

EXECUTE IMMEDIATE qry_str;
