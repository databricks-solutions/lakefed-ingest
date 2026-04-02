terraform {
  required_version = ">=1.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.82"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.4"
    }
  }
}

provider "databricks" {
  profile = "DEFAULT"
}

provider "azurerm" {
  features {}
  subscription_id = "3f2e4d32-8e8d-46d6-82bc-5bb8d962328b"
}

data "databricks_current_user" "me" {}