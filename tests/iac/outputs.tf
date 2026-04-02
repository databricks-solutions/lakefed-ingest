output "azurerm_postgresql_flexible_server" {
  description = "Name of the PostgreSQL Flexible Server"
  value       = azurerm_postgresql_flexible_server.default.name
}

output "azurerm_synapse_sql_pool" {
  description = "Name of the Synapse SQL pool"
  value       = azurerm_synapse_sql_pool.default.name
}

output "azurerm_synapse_workspace" {
  description = "Name of the Synapse workspace"
  value       = azurerm_synapse_workspace.default.name
}

output "databricks_secret_scope_name" {
  description = "Name of the Databricks secret scope"
  value       = databricks_secret_scope.this.name
}

output "postgresql_flexible_server_admin_password" {
  description = "PostgreSQL Flexible Server administrator password"
  sensitive   = true
  value       = azurerm_postgresql_flexible_server.default.administrator_password
}

output "resource_group_name" {
  description = "Name of the Azure resource group"
  value       = azurerm_resource_group.default.name
}

output "synapse_admin_password" {
  description = "Synapse Analytics SQL administrator password"
  sensitive   = true
  value       = random_password.synapse_pass.result
}
