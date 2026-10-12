resource "random_password" "synapse_pass" {
  length  = 20
  special = true
}

resource "azurerm_storage_account" "synapse" {
  name                     = substr(replace("${random_pet.name_prefix.id}synapse", "-", ""), 0, 24)
  resource_group_name      = azurerm_resource_group.default.name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true

  tags = merge(
    var.common_tags,
    local.base_tags
  )
}

resource "azurerm_storage_data_lake_gen2_filesystem" "synapse" {
  name               = "synapse"
  storage_account_id = azurerm_storage_account.synapse.id
}

resource "azurerm_synapse_workspace" "default" {
  name                                 = "${random_pet.name_prefix.id}-synapse"
  resource_group_name                  = azurerm_resource_group.default.name
  location                             = var.location
  storage_data_lake_gen2_filesystem_id = azurerm_storage_data_lake_gen2_filesystem.synapse.id
  sql_administrator_login              = var.synapse_admin_user
  sql_administrator_login_password     = random_password.synapse_pass.result

  identity {
    type = "SystemAssigned"
  }

  tags = merge(
    var.common_tags,
    local.base_tags
  )
}


resource "azurerm_synapse_sql_pool" "default" {
  name                 = "lakefed_ingest_test"
  synapse_workspace_id = azurerm_synapse_workspace.default.id
  sku_name                   = "DW100c"
  create_mode                = "Default"
  storage_account_type       = "LRS"
  geo_backup_policy_enabled  = false

  tags = merge(
    var.common_tags,
    local.base_tags
  )
}

