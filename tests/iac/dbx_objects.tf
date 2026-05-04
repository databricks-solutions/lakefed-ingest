resource "databricks_catalog" "lakefed_ingest_src" {
  name    = "lakefed_ingest_src"
  comment = "Transient federated catalog used for Lakehouse Federation Bulk Ingest integration tests"
  connection_name = databricks_connection.postgresql.name
  options = {
    database = "postgres"
  }
  properties = {
    purpose = "Transient federated catalog used for Lakehouse Federation Bulk Ingest integration tests"
  }
}

resource "databricks_catalog" "lakefed_ingest" {
  name    = "lakefed_ingest"
  comment = "Transient catalog used for Lakehouse Federation Bulk Ingest integration tests"
  properties = {
    purpose = "Transient catalog used for Lakehouse Federation Bulk Ingest integration tests"
  }
}

resource "databricks_schema" "lakefed_ingest_default" {
  catalog_name = databricks_catalog.lakefed_ingest.id
  name         = "default"
  comment      = "Transient schema used for Lakehouse Federation Bulk Ingest integration tests"
  properties = {
    purpose = "Transient schema used for Lakehouse Federation Bulk Ingest integration tests"
  }
}

resource "databricks_connection" "postgresql" {
  name            = "${random_pet.name_prefix.id}-conn"
  connection_type = "POSTGRESQL"
  comment         = "Connection to postgresql database"
  options = {
    host     = "${random_pet.name_prefix.id}-pgserver.postgres.database.azure.com"
    port     = "5432"
    user     = var.jdbc_user
    password = random_password.pass.result
  }
  properties = {
    purpose = "Used for Lakehouse Federation Bulk Ingest integration tests"
  }
}

resource "databricks_secret_scope" "this" {
  name = "${random_pet.name_prefix.id}-scope"
}

resource "databricks_secret" "jdbc_user" {
    key = "jdbc_user"
    string_value = var.jdbc_user
    scope = databricks_secret_scope.this.name
}

resource "databricks_secret" "jdbc_password" {
    key = "jdbc_pwd"
    string_value = random_password.pass.result
    scope = databricks_secret_scope.this.name
}

resource "databricks_connection" "synapse" {
  name            = "${random_pet.name_prefix.id}-synapse-conn"
  connection_type = "SQLDW"
  comment         = "Connection to Azure Synapse Analytics"
  options = {
    host     = "${random_pet.name_prefix.id}-synapse.sql.azuresynapse.net"
    port     = "1433"
    user     = var.synapse_admin_user
    password = random_password.synapse_pass.result
  }
  properties = {
    purpose = "Used for Lakehouse Federation Bulk Ingest integration tests"
  }
  depends_on = [azurerm_synapse_sql_pool.default]
}

resource "databricks_catalog" "lakefed_ingest_synapse_src" {
  name            = "${replace(random_pet.name_prefix.id, "-", "_")}_synapse_src"
  comment         = "Transient federated catalog used for Lakehouse Federation Bulk Ingest integration tests"
  connection_name = databricks_connection.synapse.name
  options = {
    database = azurerm_synapse_sql_pool.default.name
  }
  properties = {
    purpose = "Transient federated catalog used for Lakehouse Federation Bulk Ingest integration tests"
  }
}

resource "databricks_secret" "synapse_user" {
  key          = "synapse_user"
  string_value = var.synapse_admin_user
  scope        = databricks_secret_scope.this.name
}

resource "databricks_secret" "synapse_password" {
  key          = "synapse_pwd"
  string_value = random_password.synapse_pass.result
  scope        = databricks_secret_scope.this.name
}