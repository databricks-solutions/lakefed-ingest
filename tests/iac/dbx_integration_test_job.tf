# https://registry.terraform.io/providers/databricks/databricks/latest/docs/data-sources/node_type
data "databricks_node_type" "general_purpose" {
  category = "General Purpose"
  min_cores   = 8
  is_io_cache_enabled = true
}

data "databricks_spark_version" "latest_lts" {
  latest = true
  long_term_support = true
}

resource "databricks_job" "this" {
  name = "lakefed_ingest_integration_test"
  description = "Loads synthetic data for Lakehouse Federation Bulk Ingestion job"

  parameter {
    name = "catalog"
    default = "lakefed_ingest"
  }

  parameter {
    name = "schema"
    default = "default"
  }

  parameter {
    name = "src_table"
    default = "lakefed_src"
  }

  job_cluster {
    job_cluster_key = "default"
    new_cluster {
      num_workers   = 4
      spark_version = data.databricks_spark_version.latest_lts.id
      node_type_id  = data.databricks_node_type.general_purpose.id
      # data_security_mode = "SINGLE_USER"
      # single_user_name = data.databricks_current_user.me.user_name
      init_scripts {
        volumes {
          destination = databricks_file.init_script.path
        }
      }
    }
  }

  task {
    task_key = "generate_src_table"
    notebook_task {
      notebook_path = databricks_notebook.generate_src_data_notebook.path
    }
    job_cluster_key = "default"
    library {
      pypi {
        package = "dbldatagen==0.4.0.post1"
      }
    }
  }

  task {
    task_key = "write_to_external_db"
    notebook_task {
      notebook_path = databricks_notebook.write_to_external_db_notebook.path
      base_parameters = {
        host = "${random_pet.name_prefix.id}-pgserver.postgres.database.azure.com"
        secret_scope_name = "${random_pet.name_prefix.id}-scope"
     }
    }
    job_cluster_key = "default"
    depends_on {
      task_key = "generate_src_table"
    }
  }

  email_notifications {
    on_success = [ data.databricks_current_user.me.user_name ]
    on_failure = [ data.databricks_current_user.me.user_name ]
  }

}