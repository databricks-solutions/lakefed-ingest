variable "common_tags" {
  default     = { Project = "lakefed_ingest" }
  description = "Additional resource tags"
  type        = map(string)
}

variable "jdbc_user" {
  default     = "db_admin"
  description = "PostgreSQL administrator username"
  type        = string
}

variable "location" {
  default     = "eastus"
  description = "Azure region for all resources"
  type        = string
}

variable "name_prefix" {
  default     = "lakefed"
  description = "Prefix of the resource name"
  type        = string
}

variable "secret_scope_name" {
  default     = "lakefed_ingest"
  description = "Databricks secret scope name"
  type        = string
}

variable "synapse_admin_user" {
  default     = "sqladmin"
  description = "Synapse Analytics SQL administrator username"
  type        = string
}
