output "resources" {
  description = "Resource references for administration and evidence; no credentials or kubeconfig."
  value = {
    resource_group     = local.rg
    cluster            = azurerm_kubernetes_cluster.application.id
    cluster_name       = azurerm_kubernetes_cluster.application.name
    registry           = azurerm_container_registry.application.id
    registry_host      = azurerm_container_registry.application.login_server
    storage            = azurerm_storage_account.application.id
    blob_url           = trimsuffix(azurerm_storage_account.application.primary_blob_endpoint, "/")
    cosmos             = local.cosmos_account.id
    cosmos_url         = local.cosmos_account.endpoint
    cosmos_database    = var.cosmos.database_name
    search             = local.search_account.id
    search_url         = "https://${local.search_name}.search.windows.net"
    search_index       = var.search.index_name
    sql                = azurerm_mssql_server.application.id
    sql_server         = azurerm_mssql_server.application.fully_qualified_domain_name
    sql_database       = var.sql_database
    insights           = azurerm_application_insights.application.id
    hostname           = azurerm_public_ip.ingress.fqdn
    api_client_id      = azuread_application.api.client_id
    user_client_id     = azuread_application.client.client_id
    tenant_id          = var.tenant_id
    identities         = { for name, identity in azurerm_user_assigned_identity.workload : name => { client_id = identity.client_id, principal_id = identity.principal_id } }
    delivery_client_id = var.platform.delivery_client_id
  }
}

output "cluster_config" {
  description = "Non-secret Flux substitution values; publish this manifest in the cluster's Git configuration."
  value = yamlencode({
    apiVersion = "v1"
    kind       = "ConfigMap"
    metadata   = { name = "medw-platform", namespace = "flux-system" }
    data = merge({
      ENVIRONMENT         = var.environment
      TENANT_ID           = var.tenant_id
      API_CLIENT_ID       = azuread_application.api.client_id
      USER_CLIENT_ID      = azuread_application.client.client_id
      REGISTRY_HOST       = azurerm_container_registry.application.login_server
      BLOB_URL            = trimsuffix(azurerm_storage_account.application.primary_blob_endpoint, "/")
      COSMOS_URL          = local.cosmos_account.endpoint
      COSMOS_DATABASE     = var.cosmos.database_name
      SEARCH_URL          = "https://${local.search_name}.search.windows.net"
      SEARCH_INDEX        = var.search.index_name
      SQL_SERVER          = azurerm_mssql_server.application.fully_qualified_domain_name
      SQL_DATABASE        = var.sql_database
      DELIVERY_CLIENT_ID  = var.platform.delivery_client_id
      BATCH_PRINCIPAL_ID  = azurerm_user_assigned_identity.workload["airflow"].principal_id
      BATCH_SCHEDULE      = var.batch.schedule
      BATCH_TIMEZONE      = var.batch.timezone
      BATCH_MAX_DOCUMENTS = tostring(var.batch.max_documents)
      INGRESS_HOST        = azurerm_public_ip.ingress.fqdn
      INGRESS_IP          = azurerm_public_ip.ingress.ip_address
      INGRESS_IP_NAME     = azurerm_public_ip.ingress.name
      RESOURCE_GROUP      = local.rg
      CERTIFICATE_EMAIL   = var.certificate_email
      GIT_URL             = var.git.repository_url
      GIT_BRANCH          = var.git.branch
    }, { for name, identity in azurerm_user_assigned_identity.workload : "${upper(replace(name, "-", "_"))}_CLIENT_ID" => identity.client_id })
  })
}

output "telemetry_connection_string" {
  value     = azurerm_application_insights.application.connection_string
  sensitive = true
}

output "flux_bootstrap" {
  description = "Bootstrap-time controller identity patch; write to flux-system/kustomization.yaml before flux bootstrap."
  value = yamlencode({
    apiVersion = "kustomize.config.k8s.io/v1beta1"
    kind       = "Kustomization"
    resources  = ["gotk-components.yaml", "gotk-sync.yaml"]
    patches = [
      {
        target = { kind = "ServiceAccount", name = "kustomize-controller" }
        patch = yamlencode({
          apiVersion = "v1", kind = "ServiceAccount"
          metadata = { name = "kustomize-controller", annotations = {
            "azure.workload.identity/client-id" = azurerm_user_assigned_identity.workload["flux"].client_id
            "azure.workload.identity/tenant-id" = var.tenant_id
          } }
        })
      },
      {
        target = { kind = "Deployment", name = "kustomize-controller" }
        patch = yamlencode({
          apiVersion = "apps/v1", kind = "Deployment", metadata = { name = "kustomize-controller" }
          spec       = { template = { metadata = { labels = { "azure.workload.identity/use" = "true" } } } }
        })
      },
      {
        target = { kind = "Deployment", name = "kustomize-controller" }
        patch = jsonencode([{
          op    = "add", path = "/spec/template/spec/containers/0/args/-"
          value = "--feature-gates=StrictPostBuildSubstitutions=true"
        }])
      }
    ]
  })
}
