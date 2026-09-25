mock_provider "azurerm" {}
mock_provider "azurerm" { alias = "search" }
mock_provider "azurerm" { alias = "cosmos" }
mock_provider "azuread" {}
mock_provider "azapi" {}

override_data {
  target = data.azapi_resource.search[0]
  values = {
    id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/shared/providers/Microsoft.Search/searchServices/sharedsearch"
    output = {
      sku        = { name = "free" }
      properties = { disableLocalAuth = true }
    }
  }
}
override_data {
  target = data.azapi_resource.cosmos[0]
  values = {
    id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/shared/providers/Microsoft.DocumentDB/databaseAccounts/sharedcosmos"
    output = {
      kind = "GlobalDocumentDB"
      properties = {
        enableFreeTier   = true
        documentEndpoint = "https://sharedcosmos.documents.azure.com:443/"
        locations        = [{ locationName = "Australia East" }]
      }
    }
  }
}

variables {
  subscription_id    = "11111111-1111-1111-1111-111111111111"
  tenant_id          = "22222222-2222-2222-2222-222222222222"
  name_prefix        = "medwtest"
  kubernetes_version = "1.34"
  operator = {
    object_id      = "33333333-3333-3333-3333-333333333333"
    sql_admin_name = "Test operator"
  }
  platform = {
    environment_resource_group  = "rg-medw-test"
    key_vault_id                = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/bootstrap/providers/Microsoft.KeyVault/vaults/medwkey"
    sops_key_url                = "https://medwkey.vault.azure.net/keys/sops/version"
    infrastructure_principal_id = "44444444-4444-4444-4444-444444444444"
    delivery_client_id          = "55555555-5555-5555-5555-555555555555"
    delivery_principal_id       = "66666666-6666-6666-6666-666666666666"
  }
  certificate_email = "operator@example.com"
  git               = { repository_url = "https://github.com/example/medw" }
  search = {
    existing_account_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/shared/providers/Microsoft.Search/searchServices/sharedsearch"
  }
  cosmos = {
    existing_account_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/shared/providers/Microsoft.DocumentDB/databaseAccounts/sharedcosmos"
  }
}

run "supplied_accounts_remain_unmanaged" {
  command = plan
  plan_options {
    target = [azurerm_search_service.application, azurerm_cosmosdb_account.application,
    azurerm_role_assignment.search, azurerm_user_assigned_identity.workload]
  }
  assert {
    condition     = length(azurerm_search_service.application) == 0 && length(azurerm_cosmosdb_account.application) == 0
    error_message = "Supplying accounts must not put those accounts under Terraform create/update/destroy ownership."
  }
  assert {
    condition     = endswith(azurerm_role_assignment.search["retrieval"].scope, "/indexes/medw-chunks") && endswith(azurerm_role_assignment.search["ingestion-worker"].scope, "/indexes/medw-chunks")
    error_message = "Runtime Search roles must be scoped to the application index."
  }
  assert {
    condition     = !contains(keys(azurerm_user_assigned_identity.workload), "demo-client")
    error_message = "A demonstration identity must not return to the installed application."
  }
}

run "new_accounts_never_fall_back_to_paid_tiers" {
  command = plan
  plan_options {
    target = [azurerm_search_service.application, azurerm_cosmosdb_account.application,
    azurerm_role_assignment.search, azurerm_user_assigned_identity.workload]
  }
  variables {
    search = {}
    cosmos = {}
  }
  assert {
    condition     = azurerm_search_service.application[0].sku == "free" && azurerm_cosmosdb_account.application[0].free_tier_enabled
    error_message = "New accounts must request the free tier; subscription conflicts must fail creation."
  }
}

run "reject_paid_shared_search" {
  command = plan
  plan_options {
    target = [azurerm_search_service.application, azurerm_cosmosdb_account.application,
    azurerm_role_assignment.search, azurerm_user_assigned_identity.workload]
  }
  override_data {
    target = data.azapi_resource.search[0]
    values = {
      id     = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/shared/providers/Microsoft.Search/searchServices/sharedsearch"
      output = { sku = { name = "basic" }, properties = { disableLocalAuth = true } }
    }
  }
  expect_failures = [data.azapi_resource.search[0]]
}

run "reject_excess_cosmos_throughput" {
  command = plan
  plan_options {
    target = [azurerm_search_service.application, azurerm_cosmosdb_account.application,
    azurerm_role_assignment.search, azurerm_user_assigned_identity.workload]
  }
  variables { cosmos = { throughput = 1200 } }
  expect_failures = [var.cosmos]
}
