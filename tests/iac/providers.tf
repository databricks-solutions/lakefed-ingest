terraform {
  required_version = ">=1.0"

  required_providers {
    databricks = {
      source = "databricks/databricks"
      version = "1.82.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~>4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.4.0"
    }
  }
}

provider "databricks" {
  profile = "DEFAULT"
}

provider "azurerm" {
  features {}
}