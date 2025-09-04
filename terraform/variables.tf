# variables.tf
variable "aws_region" {
  description = "AWS region to deploy resources"
  default     = "us-east-1"
}

variable "availability_zones" {
  description = "Availability zones to use"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "cluster_name" {
  description = "Name for the EKS cluster"
  default     = "workspace-cluster"
}

variable "kubernetes_version" {
  description = "Kubernetes version"
  default     = "1.32"
}

variable "domain_name" {
  description = "Domain name for workspaces"
  default     = "REPLACE_ME"
}

variable "dockerhub_username" {
  description = "DockerHub username for ECR pull through cache"
  type        = string
  sensitive   = true
}

variable "dockerhub_access_token" {
  description = "DockerHub access token for ECR pull through cache"
  type        = string
  sensitive   = true
}