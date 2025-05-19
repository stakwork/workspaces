# Workspace Management System

A cloud-native workspace management system deployed using Terraform and Kubernetes.

## Project Overview

This project provides infrastructure as code (IaC) and deployment automation for a workspace management system. It includes:
- Infrastructure provisioning using Terraform
- Kubernetes cluster deployment
- Workspace controller for managing workspaces
- Port detection and management
- Domain configuration and ingress management

## Prerequisites

- AWS CLI configured with appropriate credentials
- Terraform installed
- Kubernetes CLI (kubectl) installed
- PowerShell 5.1 or higher

## Environment Configuration

The project uses environment variables loaded from a `.env` file. Create a copy of `.env.example` and modify it with your specific values:

```bash
cp .env.example .env
```

The `.env` file should contain:
- AWS credentials
- Domain configuration
- Other deployment-specific variables

## Deployment

### Using PowerShell (Windows)

```powershell
./deploy.ps1
```

### Using Bash (Linux/Mac)

```bash
./deploy.sh
```

The deployment script:
1. Loads environment variables from `.env`
2. Replaces placeholders in Kubernetes configuration files
3. Sets up AWS credentials
4. Deploys the infrastructure and workloads

## Project Structure

```
.
├── .env                # Environment variables
├── .env.example        # Example environment variables
├── deploy.ps1          # PowerShell deployment script
├── deploy.sh           # Bash deployment script
├── docs/               # Documentation
├── kubernetes/         # Kubernetes manifests
│   ├── core/          # Core Kubernetes resources
│   ├── port_detector/ # Port detection service
│   └── workspace_controller/ # Workspace management controller
├── terraform/         # Terraform infrastructure code
└── .terraform/        # Terraform state and cache
```

## Security

- AWS credentials are managed through environment variables
- Sensitive values should be stored in environment variables
- `.env` file is gitignored for security
