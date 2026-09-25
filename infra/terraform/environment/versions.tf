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
# Only the application's automatically generated Failure Anomalies rule uses
# this alias. All other resources retain the normal explicit-import safeguard.
provider "azurerm" {
  alias                           = "generated_monitoring"
  subscription_id                 = var.subscription_id
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
  features {
    skip_import_check_on_create_and_allow_overwriting_existing_resources = true
  }
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
