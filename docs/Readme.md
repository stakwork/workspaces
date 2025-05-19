## Scope of the project

- Spin up instances that will hold containers (workspaces, similar to Codespaces in Github)
- Workspaces clone the repo and wrap it up in a code-server environment and reads basic config from devcontainers.json
- Workspaces automatically open ports of exposed ports within the container
- Workspaces live in subdomains like Github Codespaces

## How this repo is organised

- Terraform scripts in main.tf that applies most of the config (needs to be tested and rectified anything that does not work correctly)
- Workspaces kubectl scripts for cluster config are in the main folder
- Workspaces kubectl script and python app inside workspace_controller for app deployment
- Workspaces automatically port opener inside port_detector folder

## Summary of Kubernetes & AWS Setup Script
This script establishes a complete workspace environment similar to GitHub Codespaces, involving:

### Infrastructure Setup:

- Initializes and applies Terraform configuration
- Configures kubectl to connect with an EKS cluster

### Networking & Security:

- Installs Nginx Ingress Controller for routing
- Sets up cert-manager for SSL certificates
- Configures external-DNS for domain management

### Storage Configuration:

- Sets up EFS (Elastic File System) with CSI driver
- Creates storage class for persistent workspace data

### Workspace Components:

- Deploys workspace controller and admin UI
- Creates necessary namespaces, service accounts, and RBAC permissions

### Monitoring & Scaling:

- Installs Prometheus and Grafana
- Configures Horizontal Pod Autoscaler for the workspace controller

