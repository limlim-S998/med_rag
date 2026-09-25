output "backend" {
  value = {
    resource_group_name  = azurerm_resource_group.bootstrap.name
    storage_account_name = azurerm_storage_account.state.name
    container_name       = azurerm_storage_container.state.name
    subscription_id      = var.subscription_id
    tenant_id            = var.tenant_id
    use_azuread_auth     = true
  }
}

output "platform" {
  value = {
    environment_resource_group  = azurerm_resource_group.environment.name
    key_vault_id                = azurerm_key_vault.secrets.id
    sops_key_url                = azurerm_key_vault_key.sops.id
    infrastructure_principal_id = azurerm_user_assigned_identity.pipeline["infrastructure"].principal_id
    delivery_client_id          = azurerm_user_assigned_identity.pipeline["delivery"].client_id
    delivery_principal_id       = azurerm_user_assigned_identity.pipeline["delivery"].principal_id
    pipeline_ids = merge(
      { for key, value in azuredevops_build_definition.pipeline : key => value.id },
      { validation = azuredevops_build_definition.validation.id }
    )
  }
}
