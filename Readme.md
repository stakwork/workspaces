# Summary of Kubernetes & AWS Setup Script
This script establishes a complete workspace environment similar to GitHub Codespaces, involving:

## Infrastructure Setup:

- Initializes and applies Terraform configuration
- Configures kubectl to connect with an EKS cluster

## Networking & Security:

- Installs Nginx Ingress Controller for routing
- Sets up cert-manager for SSL certificates
- Configures external-DNS for domain management

## Storage Configuration:

- Sets up EFS (Elastic File System) with CSI driver
- Creates storage class for persistent workspace data

## Workspace Components:

- Deploys workspace controller and admin UI
- Creates necessary namespaces, service accounts, and RBAC permissions

## Monitoring & Scaling:

- Installs Prometheus and Grafana
- Configures Horizontal Pod Autoscaler for the workspace controller
