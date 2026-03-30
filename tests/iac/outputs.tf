output "resource_group_name" {
  value = azurerm_resource_group.default.name
}

output "azurerm_postgresql_flexible_server" {
  value = azurerm_postgresql_flexible_server.default.name
}

output "postgresql_flexible_server_admin_password" {
  sensitive = true
  value     = azurerm_postgresql_flexible_server.default.administrator_password
}

output "databricks_secret_scope_name" {
 value = databricks_secret_scope.this.name
}

