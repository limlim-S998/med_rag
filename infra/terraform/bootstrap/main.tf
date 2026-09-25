resource "azurerm_resource_group" "bootstrap" {
  name     = local.bootstrap_rg
  location = var.location
  tags     = local.tags
  lifecycle { prevent_destroy = true }
}

# Keeping this empty group in bootstrap lets the infrastructure pipeline have
# resource-group permissions instead of subscription-wide Contributor/Owner.
resource "azurerm_resource_group" "environment" {
  name     = local.environment_rg
  location = var.location
  tags     = local.tags
}

resource "azurerm_storage_account" "state" {
  name                            = "${var.name_prefix}tf"
  resource_group_name             = azurerm_resource_group.bootstrap.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  tags                            = local.tags
  blob_properties {
    versioning_enabled = true
    delete_retention_policy { days = 14 }
    container_delete_retention_policy { days = 14 }
  }
  lifecycle { prevent_destroy = true }
}

resource "azurerm_role_assignment" "operator_state" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.operator_object_id
}

resource "azurerm_storage_container" "state" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"
  depends_on            = [azurerm_role_assignment.operator_state]
  lifecycle { prevent_destroy = true }
}

resource "azurerm_user_assigned_identity" "pipeline" {
  for_each            = local.connection_names
  name                = "${var.name_prefix}-${each.key}"
  resource_group_name = azurerm_resource_group.bootstrap.name
  location            = var.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "pipeline_state" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.pipeline["infrastructure"].principal_id
}

resource "azurerm_role_assignment" "provision" {
  for_each             = toset(["Contributor", "Role Based Access Control Administrator"])
  scope                = azurerm_resource_group.environment.id
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.pipeline["infrastructure"].principal_id
}

# Quota, provider-registration and free-account inventory are subscription reads.
# Write access remains limited to the owned group and explicitly supplied scopes.
resource "azurerm_role_assignment" "capacity_inventory" {
  for_each             = toset(concat([var.subscription_id], [for id in var.shared_resource_ids : split("/", id)[2]]))
  scope                = "/subscriptions/${each.value}"
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.pipeline["infrastructure"].principal_id
}

resource "azurerm_role_assignment" "shared" {
  for_each             = { for pair in setproduct(var.shared_resource_ids, ["Contributor", "Role Based Access Control Administrator"]) : "${pair[0]}:${pair[1]}" => pair }
  scope                = each.value[0]
  role_definition_name = each.value[1]
  principal_id         = azurerm_user_assigned_identity.pipeline["infrastructure"].principal_id
}

# Preflight reads index capacity using Entra authentication. It never copies a
# supplied account's administrator key into state or changes its auth settings.
resource "azurerm_role_assignment" "operator_search_capacity" {
  for_each             = toset([for id in var.shared_resource_ids : id if can(regex("(?i)/Microsoft.Search/searchServices/", id))])
  scope                = each.value
  role_definition_name = "Search Service Contributor"
  principal_id         = var.operator_object_id
}

data "azuread_service_principal" "graph" { client_id = "00000003-0000-0000-c000-000000000000" }
resource "azuread_app_role_assignment" "provision_apps" {
  principal_object_id = azurerm_user_assigned_identity.pipeline["infrastructure"].principal_id
  resource_object_id  = data.azuread_service_principal.graph.object_id
  app_role_id         = data.azuread_service_principal.graph.app_role_ids["Application.ReadWrite.OwnedBy"]
}

resource "azurerm_key_vault" "secrets" {
  name                       = "${var.name_prefix}kv"
  resource_group_name        = azurerm_resource_group.bootstrap.name
  location                   = var.location
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 7
  purge_protection_enabled   = true
  tags                       = local.tags
  lifecycle { prevent_destroy = true }
}

resource "azurerm_role_assignment" "operator_key" {
  scope                = azurerm_key_vault.secrets.id
  role_definition_name = "Key Vault Crypto Officer"
  principal_id         = var.operator_object_id
}

resource "azurerm_key_vault_key" "sops" {
  name         = "sops"
  key_vault_id = azurerm_key_vault.secrets.id
  key_type     = "RSA"
  key_size     = 3072
  key_opts     = ["encrypt", "decrypt", "wrapKey", "unwrapKey"]
  depends_on   = [azurerm_role_assignment.operator_key]
  lifecycle { prevent_destroy = true }
}

# Environment Terraform can assign Flux decrypt rights, but not read secret
# values or administer the encryption key itself.
resource "azurerm_role_assignment" "provision_key_roles" {
  scope                = azurerm_key_vault.secrets.id
  role_definition_name = "Role Based Access Control Administrator"
  principal_id         = azurerm_user_assigned_identity.pipeline["infrastructure"].principal_id
}
