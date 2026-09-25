variable "subscription_id" { type = string }
variable "tenant_id" { type = string }
variable "location" {
  type    = string
  default = "australiaeast"
}
variable "name_prefix" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,17}$", var.name_prefix))
    error_message = "Use 3–18 lowercase letters/digits, starting with a letter."
  }
}
variable "environment" {
  type    = string
  default = "dev"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Use dev, staging or prod."
  }
}
variable "platform" {
  description = "Non-secret outputs from the separately managed bootstrap stack."
  type = object({
    environment_resource_group  = string
    key_vault_id                = string
    sops_key_url                = string
    infrastructure_principal_id = string
    delivery_client_id          = string
    delivery_principal_id       = string
  })
}
variable "operator" {
  type = object({
    object_id      = string
    sql_admin_name = string
  })
}
variable "sql_database" {
  type    = string
  default = "medw"
}
variable "sql_operator_ipv4" {
  description = "Optional administrator IPv4 for initial SQL grants; remove after bootstrap."
  type        = string
  default     = null
  validation {
    condition     = var.sql_operator_ipv4 == null ? true : can(cidrnetmask("${var.sql_operator_ipv4}/32"))
    error_message = "Supply a single IPv4 address, without a CIDR suffix."
  }
}
variable "kubernetes_version" {
  description = "A supported AKS minor or patch version, explicitly selected after quota/version preflight."
  type        = string
}
variable "node_vm_size" {
  type    = string
  default = "Standard_D4s_v5"
}
variable "search" {
  type = object({
    existing_account_id = optional(string)
    subscription_id     = optional(string)
    resource_group      = optional(string)
    index_name          = optional(string, "medw-chunks")
  })
  default = {}
  validation {
    condition     = var.search.existing_account_id == null ? true : can(regex("(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Search/searchServices/[^/]+$", var.search.existing_account_id))
    error_message = "existing_account_id must identify an Azure Search service."
  }
}
variable "cosmos" {
  type = object({
    existing_account_id = optional(string)
    subscription_id     = optional(string)
    resource_group      = optional(string)
    database_name       = optional(string, "medw")
    throughput          = optional(number, 400)
  })
  default = {}
  validation {
    condition     = var.cosmos.existing_account_id == null ? true : can(regex("(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.DocumentDB/databaseAccounts/[^/]+$", var.cosmos.existing_account_id))
    error_message = "existing_account_id must identify a Cosmos DB account."
  }
  validation {
    condition     = var.cosmos.throughput >= 400 && var.cosmos.throughput <= 1000 && var.cosmos.throughput % 100 == 0
    error_message = "Free-tier configuration requires 400–1000 RU/s, in increments of 100; shared remaining capacity is checked separately."
  }
}
variable "certificate_email" {
  description = "Certificate-expiry/account contact for the ACME issuer."
  type        = string
}
variable "git" {
  type = object({
    repository_url = string
    branch         = optional(string, "main")
  })
}
variable "batch" {
  type = object({
    schedule      = optional(string, "0 2 * * *")
    timezone      = optional(string, "Australia/Brisbane")
    max_documents = optional(number, 500)
  })
  default = {}
}

locals {
  tags           = { application = "medwriter", environment = var.environment, managed-by = "terraform" }
  workload_names = toset(["gateway", "retrieval", "generation", "ingestion-worker", "reranker", "airflow", "qdrant-backup", "airflow-backup", "flux"])
  rg             = var.platform.environment_resource_group
  search_group   = var.search.existing_account_id == null ? coalesce(var.search.resource_group, local.rg) : split("/", var.search.existing_account_id)[4]
  cosmos_group   = var.cosmos.existing_account_id == null ? coalesce(var.cosmos.resource_group, local.rg) : split("/", var.cosmos.existing_account_id)[4]
  search_name    = var.search.existing_account_id == null ? "${var.name_prefix}search" : split("/", var.search.existing_account_id)[8]
  cosmos_name    = var.cosmos.existing_account_id == null ? "${var.name_prefix}cosmos" : split("/", var.cosmos.existing_account_id)[8]
  search_account = {
    id   = var.search.existing_account_id == null ? azurerm_search_service.application[0].id : data.azapi_resource.search[0].id
    name = local.search_name
  }
  cosmos_account = {
    id       = var.cosmos.existing_account_id == null ? azurerm_cosmosdb_account.application[0].id : data.azapi_resource.cosmos[0].id
    name     = local.cosmos_name
    endpoint = var.cosmos.existing_account_id == null ? azurerm_cosmosdb_account.application[0].endpoint : data.azapi_resource.cosmos[0].output.properties.documentEndpoint
  }
}
