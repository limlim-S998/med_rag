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
variable "operator_object_id" {
  description = "Entra object ID of the administrator bootstrapping this installation."
  type        = string
}
variable "shared_resource_ids" {
  description = "Existing accounts the provisioning identity may manage children/role bindings in. Accounts remain outside Terraform ownership."
  type        = set(string)
  default     = []
}
variable "devops" {
  type = object({
    organization                 = string
    project                      = string
    github_service_connection_id = string
    repository                   = string
    branch                       = optional(string, "main")
    agent_pool                   = optional(string, "Azure Pipelines")
  })
}

locals {
  tags             = { application = "medwriter", environment = var.environment, managed-by = "terraform" }
  environment_rg   = "rg-medw-${var.environment}"
  bootstrap_rg     = "rg-medw-${var.environment}-bootstrap"
  connection_names = { infrastructure = "medw-${var.environment}-infrastructure", delivery = "medw-${var.environment}-delivery" }
}
