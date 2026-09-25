resource "azurerm_storage_account" "application" {
  name                            = "${var.name_prefix}sa"
  resource_group_name             = local.rg
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
    delete_retention_policy { days = 7 }
    container_delete_retention_policy { days = 7 }
  }
}

resource "azurerm_role_assignment" "storage_administration" {
  for_each             = toset([var.operator.object_id, var.platform.infrastructure_principal_id])
  scope                = azurerm_storage_account.application.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
}

resource "azurerm_storage_container" "application" {
  for_each              = toset(["raw", "parsed", "snapshots", "airflow-backups"])
  name                  = each.value
  storage_account_id    = azurerm_storage_account.application.id
  container_access_type = "private"
  depends_on            = [azurerm_role_assignment.storage_administration]
}

resource "azurerm_storage_management_policy" "archives" {
  storage_account_id = azurerm_storage_account.application.id
  rule {
    name    = "airflow-metadata-retention"
    enabled = true
    filters {
      prefix_match = ["airflow-backups/metadata/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob { delete_after_days_since_modification_greater_than = 14 }
      version { delete_after_days_since_creation = 14 }
    }
  }
}

# ARM metadata only: AzureRM's account data sources also retrieve account keys.
# Borrowed account credentials must not be copied into our Terraform state.
data "azapi_resource" "search" {
  count                  = var.search.existing_account_id == null ? 0 : 1
  type                   = "Microsoft.Search/searchServices@2023-11-01"
  resource_id            = var.search.existing_account_id
  response_export_values = ["sku", "properties.authOptions", "properties.disableLocalAuth", "properties.publicNetworkAccess"]
  lifecycle {
    postcondition {
      condition     = lower(self.output.sku.name) == "free"
      error_message = "This low-cost configuration requires a free-tier Search account."
    }
    postcondition {
      condition     = try(self.output.properties.disableLocalAuth, false) || can(self.output.properties.authOptions.aadOrApiKey)
      error_message = "The supplied Search account must already support Entra authentication; shared settings will not be changed."
    }
  }
}
resource "azurerm_search_service" "application" {
  provider                     = azurerm.search
  count                        = var.search.existing_account_id == null ? 1 : 0
  name                         = local.search_name
  resource_group_name          = local.search_group
  location                     = var.location
  sku                          = "free"
  local_authentication_enabled = false
  authentication_failure_mode  = "http401WithBearerChallenge"
  tags                         = local.tags
}

# The index is a first-class Terraform data-plane resource: state/import/destroy
# track this application's index without claiming the supplied Search account.
resource "azapi_data_plane_resource" "search_index" {
  type       = "Microsoft.Search/searchServices/indexes@2024-07-01"
  parent_id  = "${local.search_account.name}.search.windows.net"
  name       = var.search.index_name
  body       = merge(jsondecode(file("${path.module}/../../search/csr-chunks-index.json")), { name = var.search.index_name })
  depends_on = [azurerm_role_assignment.search_schema]
}

data "azapi_resource" "cosmos" {
  count                  = var.cosmos.existing_account_id == null ? 0 : 1
  type                   = "Microsoft.DocumentDB/databaseAccounts@2024-11-15"
  resource_id            = var.cosmos.existing_account_id
  response_export_values = ["kind", "properties.enableFreeTier", "properties.documentEndpoint", "properties.locations", "properties.capabilities"]
  lifecycle {
    postcondition {
      condition     = self.output.properties.enableFreeTier && self.output.kind == "GlobalDocumentDB" && length(self.output.properties.locations) == 1
      error_message = "This configuration requires a free-tier Cosmos NoSQL account."
    }
  }
}
resource "azurerm_cosmosdb_account" "application" {
  provider                     = azurerm.cosmos
  count                        = var.cosmos.existing_account_id == null ? 1 : 0
  name                         = local.cosmos_name
  resource_group_name          = local.cosmos_group
  location                     = var.location
  offer_type                   = "Standard"
  kind                         = "GlobalDocumentDB"
  free_tier_enabled            = true
  local_authentication_enabled = false
  consistency_policy { consistency_level = "Session" }
  geo_location {
    location          = var.location
    failover_priority = 0
  }
  tags = local.tags
}
resource "azurerm_cosmosdb_sql_database" "application" {
  provider            = azurerm.cosmos
  name                = var.cosmos.database_name
  resource_group_name = local.cosmos_group
  account_name        = local.cosmos_account.name
  throughput          = var.cosmos.throughput
}
resource "azurerm_cosmosdb_sql_container" "application" {
  provider = azurerm.cosmos
  for_each = {
    documents      = { partition = "/study_id", ttl = null }
    sessions       = { partition = "/user_id", ttl = 43200 }
    platform-state = { partition = "/study_id", ttl = null }
  }
  name                = each.key
  resource_group_name = local.cosmos_group
  account_name        = local.cosmos_account.name
  database_name       = azurerm_cosmosdb_sql_database.application.name
  partition_key_paths = [each.value.partition]
  default_ttl         = each.value.ttl
}

resource "azurerm_mssql_server" "application" {
  name                = "${var.name_prefix}sql"
  resource_group_name = local.rg
  location            = var.location
  version             = "12.0"
  minimum_tls_version = "1.2"
  connection_policy   = "Proxy"
  azuread_administrator {
    login_username              = var.operator.sql_admin_name
    object_id                   = var.operator.object_id
    tenant_id                   = var.tenant_id
    azuread_authentication_only = true
  }
  tags = local.tags
}
resource "azurerm_mssql_database" "application" {
  name                 = var.sql_database
  server_id            = azurerm_mssql_server.application.id
  sku_name             = "Basic"
  max_size_gb          = 2
  storage_account_type = "Local"
  tags                 = local.tags
}
resource "azurerm_mssql_firewall_rule" "azure" {
  name             = "azure-services"
  server_id        = azurerm_mssql_server.application.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
resource "azurerm_mssql_firewall_rule" "operator" {
  count            = var.sql_operator_ipv4 == null ? 0 : 1
  name             = "bootstrap-operator"
  server_id        = azurerm_mssql_server.application.id
  start_ip_address = var.sql_operator_ipv4
  end_ip_address   = var.sql_operator_ipv4
}

resource "azurerm_log_analytics_workspace" "application" {
  name                = "${var.name_prefix}logs"
  resource_group_name = local.rg
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.1
  tags                = local.tags
}
resource "azurerm_application_insights" "application" {
  name                 = "${var.name_prefix}ai"
  resource_group_name  = local.rg
  location             = var.location
  workspace_id         = azurerm_log_analytics_workspace.application.id
  application_type     = "web"
  daily_data_cap_in_gb = 0.1
  tags                 = local.tags
}
