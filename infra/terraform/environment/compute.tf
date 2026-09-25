data "azurerm_resource_group" "environment" { name = local.rg }

resource "azurerm_container_registry" "application" {
  name                = "${var.name_prefix}acr"
  resource_group_name = local.rg
  location            = var.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_kubernetes_cluster" "application" {
  # The controller uses this address. Destroy AKS (and its load balancer)
  # before attempting to delete the externally allocated ingress address.
  depends_on                        = [azurerm_public_ip.ingress]
  name                              = "${var.name_prefix}aks"
  resource_group_name               = local.rg
  location                          = var.location
  dns_prefix                        = var.name_prefix
  kubernetes_version                = var.kubernetes_version
  sku_tier                          = "Free"
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true
  local_account_disabled            = true
  role_based_access_control_enabled = true
  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = var.tenant_id
  }
  node_provisioning_profile { mode = "Manual" }
  default_node_pool {
    name                 = "system"
    node_count           = 1
    vm_size              = var.node_vm_size
    os_disk_size_gb      = 64
    auto_scaling_enabled = false
    upgrade_settings { max_surge = "1" }
  }
  identity { type = "SystemAssigned" }
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium"
    pod_cidr            = "192.168.0.0/16"
    service_cidr        = "10.0.0.0/16"
    dns_service_ip      = "10.0.0.10"
    load_balancer_sku   = "standard"
  }
  tags = local.tags
}

resource "azurerm_role_assignment" "image_pull" {
  scope                            = azurerm_container_registry.application.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.application.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "image_push" {
  scope                = azurerm_container_registry.application.id
  role_definition_name = "AcrPush"
  principal_id         = var.platform.delivery_principal_id
}

resource "azurerm_role_assignment" "cluster_operator" {
  scope                = azurerm_kubernetes_cluster.application.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = var.operator.object_id
}

resource "azurerm_public_ip" "ingress" {
  name                = "${var.name_prefix}-ingress"
  resource_group_name = local.rg
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  domain_name_label   = var.name_prefix
  tags                = local.tags
}
resource "azurerm_role_assignment" "ingress_address" {
  scope                = azurerm_public_ip.ingress.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_kubernetes_cluster.application.identity[0].principal_id
}
