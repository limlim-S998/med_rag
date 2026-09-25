data "azuredevops_project" "application" { name = var.devops.project }

resource "azuredevops_serviceendpoint_azurerm" "pipeline" {
  for_each                               = local.connection_names
  project_id                             = data.azuredevops_project.application.id
  service_endpoint_name                  = each.value
  service_endpoint_authentication_scheme = "WorkloadIdentityFederation"
  credentials {
    serviceprincipalid = azurerm_user_assigned_identity.pipeline[each.key].client_id
  }
  azurerm_spn_tenantid      = var.tenant_id
  azurerm_subscription_id   = var.subscription_id
  azurerm_subscription_name = "medwriter-${var.environment}"
}

resource "azurerm_federated_identity_credential" "pipeline" {
  for_each                  = local.connection_names
  name                      = "azure-pipelines"
  user_assigned_identity_id = azurerm_user_assigned_identity.pipeline[each.key].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azuredevops_serviceendpoint_azurerm.pipeline[each.key].workload_identity_federation_issuer
  subject                   = azuredevops_serviceendpoint_azurerm.pipeline[each.key].workload_identity_federation_subject
}

resource "azuredevops_build_definition" "pipeline" {
  for_each   = local.connection_names
  project_id = data.azuredevops_project.application.id
  name       = each.value
  ci_trigger { use_yaml = true }
  repository {
    repo_type             = "GitHub"
    repo_id               = var.devops.repository
    branch_name           = "refs/heads/${var.devops.branch}"
    yml_path              = "deploy/azure-pipelines/${each.key}.yml"
    service_connection_id = var.devops.github_service_connection_id
  }
  agent_pool_name = var.devops.agent_pool
  variable {
    name  = "azureConnection"
    value = each.value
  }
  variable {
    name  = "environment"
    value = var.environment
  }
  variable {
    name  = "stateStorageAccount"
    value = azurerm_storage_account.state.name
  }
  variable {
    name  = "stateResourceGroup"
    value = azurerm_resource_group.bootstrap.name
  }
  variable {
    name  = "releaseBranch"
    value = var.devops.branch
  }
}

# Validation can read this repository but has no Azure endpoint authorization.
# Keep it distinct from pipelines permitted to provision, publish or migrate.
resource "azuredevops_build_definition" "validation" {
  project_id      = data.azuredevops_project.application.id
  name            = "medw-${var.environment}-validation"
  agent_pool_name = "Azure Pipelines"
  ci_trigger { use_yaml = true }
  pull_request_trigger {
    use_yaml       = true
    initial_branch = "refs/heads/${var.devops.branch}"
    forks {
      enabled       = false
      share_secrets = false
    }
  }
  repository {
    repo_type             = "GitHub"
    repo_id               = var.devops.repository
    branch_name           = "refs/heads/${var.devops.branch}"
    yml_path              = "deploy/azure-pipelines/validation.yml"
    service_connection_id = var.devops.github_service_connection_id
  }
}

resource "azuredevops_pipeline_authorization" "azure" {
  for_each    = local.connection_names
  project_id  = data.azuredevops_project.application.id
  resource_id = azuredevops_serviceendpoint_azurerm.pipeline[each.key].id
  type        = "endpoint"
  pipeline_id = azuredevops_build_definition.pipeline[each.key].id
}

resource "azuredevops_pipeline_authorization" "github" {
  for_each = merge(
    { for key, value in azuredevops_build_definition.pipeline : key => value.id },
    { validation = azuredevops_build_definition.validation.id }
  )
  project_id  = data.azuredevops_project.application.id
  resource_id = var.devops.github_service_connection_id
  type        = "endpoint"
  pipeline_id = each.value
}
