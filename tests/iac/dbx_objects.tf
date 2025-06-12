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

resource "databricks_volume" "this" {
  name         = "init_scripts"
  catalog_name = databricks_catalog.lakefed_ingest.name
  schema_name  = databricks_schema.lakefed_ingest_default.name
  volume_type  = "MANAGED"
  comment      = "Transient volume for used for Lakehouse Federation Bulk Ingest integration tests"
}

resource "databricks_file" "init_script" {
  content_base64 = base64encode(<<-EOT
    #!/bin/bash
    sudo apt-get update && apt-get install -y postgresql-client
    EOT
  )
  path = "${databricks_volume.this.volume_path}/init_script.sh"
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