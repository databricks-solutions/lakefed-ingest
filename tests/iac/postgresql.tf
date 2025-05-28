locals {
  base_tags = {
    Owner = data.databricks_current_user.me.user_name
  }
}

resource "random_pet" "name_prefix" {
  prefix = var.name_prefix
  length = 1
}

resource "azurerm_resource_group" "default" {
  name     = random_pet.name_prefix.id
  location = var.location
  tags = merge(
    var.common_tags,
    local.base_tags
  )
}

resource "random_password" "pass" {
  length = 20
}

resource "azurerm_postgresql_flexible_server" "default" {
  name                   = "${random_pet.name_prefix.id}-pgserver"
  location               = azurerm_resource_group.default.location
  resource_group_name    = azurerm_resource_group.default.name
  
  administrator_login    = var.jdbc_user
  administrator_password = random_password.pass.result

  zone                   = "1"
  sku_name               = "GP_Standard_D16s_v3"
  version                = "16"
  storage_mb             = 1048576

  backup_retention_days  = 7

  tags = merge(
    var.common_tags,
    local.base_tags
  )
}