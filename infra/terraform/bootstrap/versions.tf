terraform {
  required_version = "= 1.16.4"
  required_providers {
    azurerm     = { source = "hashicorp/azurerm", version = "= 5.6.0" }
    azuread     = { source = "hashicorp/azuread", version = "= 3.9.0" }
    azuredevops = { source = "microsoft/azuredevops", version = "= 1.16.0" }
  }
  # First apply uses local state. Add the supplied backend override afterwards
  # and use init -migrate-state to move this same state into its managed account.
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
  storage_use_azuread             = true
  features {}
}

provider "azuread" { tenant_id = var.tenant_id }
provider "azuredevops" { org_service_url = var.devops.organization }
