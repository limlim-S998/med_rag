resource "azurerm_user_assigned_identity" "workload" {
  for_each            = local.workload_names
  name                = "${var.name_prefix}-${each.key}"
  resource_group_name = local.rg
  location            = var.location
  tags                = local.tags
}
resource "azurerm_federated_identity_credential" "workload" {
  for_each                  = local.workload_names
  name                      = "aks"
  user_assigned_identity_id = azurerm_user_assigned_identity.workload[each.key].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.application.oidc_issuer_url
  subject                   = each.key == "flux" ? "system:serviceaccount:flux-system:kustomize-controller" : "system:serviceaccount:medw:${each.key}"
}

locals {
  api_access_scope_id = uuidv5("url", "${var.name_prefix}/api/access")
  application_owners  = [var.operator.object_id, var.platform.infrastructure_principal_id]
}

# Entra automatically makes the creating service principal an application owner.
# Manage the complete owner set on the application, so both operator and pipeline
# creation work without separately attempting to create that existing binding.
resource "azuread_application" "api" {
  display_name = "${var.name_prefix}-api"
  owners       = local.application_owners
  api {
    requested_access_token_version = 2
    oauth2_permission_scope {
      id                         = local.api_access_scope_id
      value                      = "access"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Access medical writer API"
      admin_consent_description  = "Use the API subject to study membership"
      user_consent_display_name  = "Access medical writer API"
      user_consent_description   = "Use the API subject to study membership"
    }
  }
  # The URI includes the client ID Azure assigns during creation; its dedicated
  # resource owns that property after the application exists.
  lifecycle { ignore_changes = [identifier_uris] }
}
resource "azuread_application_identifier_uri" "api" {
  application_id = azuread_application.api.id
  identifier_uri = "api://${azuread_application.api.client_id}"
}
resource "azuread_service_principal" "api" { client_id = azuread_application.api.client_id }
resource "azuread_application" "client" {
  display_name     = "${var.name_prefix}-client"
  sign_in_audience = "AzureADMyOrg"
  owners           = local.application_owners
  api { requested_access_token_version = 2 }
  public_client { redirect_uris = ["http://localhost"] }
  required_resource_access {
    resource_app_id = azuread_application.api.client_id
    resource_access {
      id   = local.api_access_scope_id
      type = "Scope"
    }
  }
}
resource "azuread_service_principal" "client" { client_id = azuread_application.client.client_id }
resource "azuread_application_pre_authorized" "client" {
  application_id       = azuread_application.api.id
  authorized_client_id = azuread_application.client.client_id
  permission_ids       = [local.api_access_scope_id]
}

locals {
  storage_access = {
    gateway-upload   = { service = "gateway", role = "Storage Blob Data Contributor", container = "raw" }
    generation-read  = { service = "generation", role = "Storage Blob Data Reader", container = "raw" }
    ingestion-raw    = { service = "ingestion-worker", role = "Storage Blob Data Contributor", container = "raw" }
    ingestion-parsed = { service = "ingestion-worker", role = "Storage Blob Data Contributor", container = "parsed" }
    qdrant-backup    = { service = "qdrant-backup", role = "Storage Blob Data Contributor", container = "snapshots" }
    airflow-backup   = { service = "airflow-backup", role = "Storage Blob Data Contributor", container = "airflow-backups" }
  }
  cosmos_access = {
    gateway-documents = { service = "gateway", role = "writer", container = "documents" }
    gateway-sessions  = { service = "gateway", role = "writer", container = "sessions" }
    retrieval-state   = { service = "retrieval", role = "reader", container = "platform-state" }
    generation-state  = { service = "generation", role = "evidence-appender", container = "platform-state" }
    ingestion-state   = { service = "ingestion-worker", role = "platform-writer", container = "platform-state" }
    ingestion-docs    = { service = "ingestion-worker", role = "writer", container = "documents" }
  }
  cosmos_role_ids = merge({
    reader = "${local.cosmos_account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001"
    writer = "${local.cosmos_account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  }, { for key, role in azurerm_cosmosdb_sql_role_definition.application : key => role.id })
}
resource "azurerm_role_assignment" "blob" {
  for_each             = local.storage_access
  scope                = "${azurerm_storage_account.application.id}/blobServices/default/containers/${azurerm_storage_container.application[each.value.container].name}"
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.workload[each.value.service].principal_id
}
resource "azurerm_role_assignment" "delegation" {
  scope                = azurerm_storage_account.application.id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = azurerm_user_assigned_identity.workload["gateway"].principal_id
}
resource "azurerm_role_assignment" "search" {
  provider             = azurerm.search
  for_each             = { retrieval = "Search Index Data Reader", ingestion-worker = "Search Index Data Contributor" }
  scope                = "${local.search_account.id}/indexes/${azapi_data_plane_resource.search_index.name}"
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.workload[each.key].principal_id
}
resource "azurerm_role_assignment" "search_schema" {
  provider             = azurerm.search
  for_each             = toset(var.search.existing_account_id == null ? [var.operator.object_id, var.platform.infrastructure_principal_id] : [var.platform.infrastructure_principal_id])
  scope                = local.search_account.id
  role_definition_name = "Search Service Contributor"
  principal_id         = each.value
}
resource "azurerm_cosmosdb_sql_role_definition" "application" {
  provider            = azurerm.cosmos
  for_each            = toset(["evidence-appender", "platform-writer"])
  role_definition_id  = uuidv5("url", "${local.cosmos_account.id}/${var.cosmos.database_name}/${each.key}")
  resource_group_name = local.cosmos_group
  account_name        = local.cosmos_account.name
  name                = "${var.cosmos.database_name}-${each.key}"
  type                = "CustomRole"
  assignable_scopes   = ["${local.cosmos_account.id}/dbs/${azurerm_cosmosdb_sql_database.application.name}/colls/platform-state"]
  permissions {
    data_actions = jsondecode(file("${path.module}/../../cosmos/${each.key}.json")).Permissions[0].DataActions
  }
}
resource "azurerm_cosmosdb_sql_role_assignment" "application" {
  provider            = azurerm.cosmos
  for_each            = local.cosmos_access
  resource_group_name = local.cosmos_group
  account_name        = local.cosmos_account.name
  role_definition_id  = local.cosmos_role_ids[each.value.role]
  principal_id        = azurerm_user_assigned_identity.workload[each.value.service].principal_id
  scope               = "${local.cosmos_account.id}/dbs/${azurerm_cosmosdb_sql_database.application.name}/colls/${azurerm_cosmosdb_sql_container.application[each.value.container].name}"
}
resource "azurerm_role_assignment" "flux_decryption" {
  scope                = var.platform.key_vault_id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.workload["flux"].principal_id
}
