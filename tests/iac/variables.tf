variable "name_prefix" {
  default     = "lakefed"
  description = "Prefix of the resource name"
}

variable "location" {
  default     = "eastus"
  description = "Location of the resource"
}

variable "common_tags" {
  default     = { Project = "lakefed_ingest" }
  description = "Additional resource tags"
  type        = map(string)
}

variable "secret_scope_name" {
  default     = "lakefed_ingest"
  description = "Databricks secret scope name"
}

variable "jdbc_user" {
  default     = "db_admin"
  description = "Database username"
}
