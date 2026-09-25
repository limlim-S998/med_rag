terraform {
  required_version = "= 1.16.4"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "= 5.6.0" }
    azuread = { source = "hashicorp/azuread", version = "= 3.9.0" }
    azapi   = { source = "Azure/azapi", version = "= 2.12.0" }
  }
  backend "azurerm" {}
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
  storage_use_azuread             = true
  features {}
}
provider "azurerm" {
  alias                           = "search"
  subscription_id                 = var.search.existing_account_id == null ? coalesce(var.search.subscription_id, var.subscription_id) : split("/", var.search.existing_account_id)[2]
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
  features {}
}
provider "azurerm" {
  alias                           = "cosmos"
  subscription_id                 = var.cosmos.existing_account_id == null ? coalesce(var.cosmos.subscription_id, var.subscription_id) : split("/", var.cosmos.existing_account_id)[2]
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
  features {}
}
provider "azuread" { tenant_id = var.tenant_id }
provider "azapi" {
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
}
