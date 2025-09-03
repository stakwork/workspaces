# main.tf

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.23"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.11"
    }
  }
  required_version = ">= 1.0"
}

provider "aws" {
  region = var.aws_region
}

provider "kubernetes" {
  host                   = aws_eks_cluster.workspace_cluster.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.workspace_cluster.certificate_authority[0].data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.workspace_cluster.name]
    command     = "aws"
  }
}

provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.workspace_cluster.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.workspace_cluster.certificate_authority[0].data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.workspace_cluster.name]
      command     = "aws"
    }
  }
}

# VPC for the Kubernetes cluster
resource "aws_vpc" "workspace_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  
  tags = {
    Name = "workspace-vpc"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }
}

# Public subnets
resource "aws_subnet" "public" {
  count                   = length(var.availability_zones)
  vpc_id                  = aws_vpc.workspace_vpc.id
  cidr_block              = "10.0.${count.index + 100}.0/24"
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true
  
  tags = {
    Name                                       = "workspace-public-${var.availability_zones[count.index]}"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
    "kubernetes.io/role/elb"                    = "1"
  }
}

# Private subnets
resource "aws_subnet" "private" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.workspace_vpc.id
  cidr_block        = "10.0.${count.index}.0/24"
  availability_zone = var.availability_zones[count.index]
  
  tags = {
    Name                                        = "workspace-private-${var.availability_zones[count.index]}"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
    "kubernetes.io/role/internal-elb"           = "1"
  }
}

# Internet Gateway
resource "aws_internet_gateway" "workspace_igw" {
  vpc_id = aws_vpc.workspace_vpc.id
  
  tags = {
    Name = "workspace-igw"
  }
}

# Elastic IPs for NAT Gateways
resource "aws_eip" "nat" {
  count  = 1
  domain = "vpc"
  
  tags = {
    Name = "workspace-nat-eip-${count.index + 1}"
  }
}

# NAT Gateways
resource "aws_nat_gateway" "workspace_nat" {
  count         = 1
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  
  tags = {
    Name = "workspace-nat-${count.index + 1}"
  }

  depends_on = [aws_internet_gateway.workspace_igw]
}

# Route table for public subnets
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.workspace_vpc.id
  
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.workspace_igw.id
  }
  
  tags = {
    Name = "workspace-public-rt"
  }
}

# Route table for private subnets
resource "aws_route_table" "private" {
  count  = 1
  vpc_id = aws_vpc.workspace_vpc.id
  
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.workspace_nat[0].id
  }
  
  tags = {
    Name = "workspace-private-rt-${count.index + 1}"
  }
}

# Associate route tables with subnets
resource "aws_route_table_association" "public" {
  count          = length(var.availability_zones)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = length(var.availability_zones)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# EKS Cluster
resource "aws_eks_cluster" "workspace_cluster" {
  name     = var.cluster_name
  role_arn = aws_iam_role.eks_cluster_role.arn
  version  = var.kubernetes_version

  enabled_cluster_log_types = [
    "scheduler"
  ]

  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    security_group_ids      = [aws_security_group.eks_cluster_sg.id]
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
    aws_iam_role_policy_attachment.eks_vpc_resource_controller,
  ]

  tags = {
    Name = var.cluster_name
  }
}

# EKS Node Group
resource "aws_eks_node_group" "workspace_nodes" {
  cluster_name    = aws_eks_cluster.workspace_cluster.name
  node_group_name = "workspace-nodes"
  node_role_arn   = aws_iam_role.eks_node_role.arn
  subnet_ids      = aws_subnet.private[*].id
  instance_types  = ["m6i.2xlarge"]
  disk_size       = 80

  scaling_config {
    desired_size = 5
    min_size     = 1
    max_size     = 5
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_worker_node_policy,
    aws_iam_role_policy_attachment.eks_cni_policy,
    aws_iam_role_policy_attachment.ec2_container_registry_readonly,
  ]

  tags = {
    Name = "workspace-node-group"
  }
}

# EKS Cluster IAM Role
resource "aws_iam_role" "eks_cluster_role" {
  name = "eks-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "eks.amazonaws.com"
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.eks_cluster_role.name
}

resource "aws_iam_role_policy_attachment" "eks_vpc_resource_controller" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  role       = aws_iam_role.eks_cluster_role.name
}

# EKS Node IAM Role
resource "aws_iam_role" "eks_node_role" {
  name = "eks-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "eks_worker_node_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.eks_node_role.name
}

resource "aws_iam_role_policy_attachment" "eks_cni_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.eks_node_role.name
}

resource "aws_iam_role_policy_attachment" "ec2_container_registry_readonly" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.eks_node_role.name
}

# Security group for EKS cluster
resource "aws_security_group" "eks_cluster_sg" {
  name        = "eks-cluster-sg"
  description = "Security group for EKS cluster"
  vpc_id      = aws_vpc.workspace_vpc.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "eks-cluster-sg"
  }
}

# EFS File System
resource "aws_efs_file_system" "workspace_efs" {
  creation_token = "workspace-efs"
  encrypted      = true

  tags = {
    Name = "workspace-efs"
  }
}

resource "aws_security_group" "efs_sg" {
  name        = "workspace-efs-sg"
  description = "Allow NFS traffic from EKS nodes"
  vpc_id      = aws_vpc.workspace_vpc.id

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_eks_cluster.workspace_cluster.vpc_config[0].cluster_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "workspace-efs-sg"
  }
}

# Security Group for EFS
# resource "aws_security_group" "efs_sg" {
#   name        = "workspace-efs-sg"
#   description = "Allow NFS traffic from EKS nodes"
#   vpc_id      = aws_vpc.workspace_vpc.id

#   ingress {
#     from_port       = 2049
#     to_port         = 2049
#     protocol        = "tcp"
#     security_groups = [aws_security_group.eks_cluster_sg.id]
#   }

#   egress {
#     from_port   = 0
#     to_port     = 0
#     protocol    = "-1"
#     cidr_blocks = ["0.0.0.0/0"]
#   }

#   tags = {
#     Name = "workspace-efs-sg"
#   }
# }

# EFS Mount Targets
resource "aws_efs_mount_target" "workspace_efs_mount" {
  count           = length(var.availability_zones)
  file_system_id  = aws_efs_file_system.workspace_efs.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs_sg.id]
}

# OIDC Provider for IAM roles for service accounts
data "tls_certificate" "eks" {
  url = aws_eks_cluster.workspace_cluster.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.workspace_cluster.identity[0].oidc[0].issuer
}

# IAM Policy for External DNS
resource "aws_iam_policy" "external_dns" {
  name        = "ExternalDNSPolicy"
  description = "Policy for External DNS"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow",
        Action = [
          "route53:ChangeResourceRecordSets"
        ],
        Resource = [
          "arn:aws:route53:::hostedzone/*"
        ]
      },
      {
        Effect = "Allow",
        Action = [
          "route53:ListHostedZones",
          "route53:ListResourceRecordSets"
        ],
        Resource = [
          "*"
        ]
      }
    ]
  })
}

# IAM Role for External DNS
resource "aws_iam_role" "external_dns_role" {
  name = "external-dns-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRoleWithWebIdentity"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Condition = {
          StringEquals = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub": "system:serviceaccount:external-dns:external-dns"
          }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "external_dns_policy_attachment" {
  role       = aws_iam_role.external_dns_role.name
  policy_arn = aws_iam_policy.external_dns.arn
}

# IAM Policy for EFS CSI Driver
resource "aws_iam_policy" "efs_csi_driver_policy" {
  name        = "EFSCSIDriverPolicy"
  description = "Policy for EFS CSI Driver to create and manage access points"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "elasticfilesystem:DescribeAccessPoints",
          "elasticfilesystem:DescribeFileSystems",
          "elasticfilesystem:DescribeMountTargets",
          "elasticfilesystem:CreateAccessPoint",
          "elasticfilesystem:DeleteAccessPoint",
          "elasticfilesystem:TagResource"
        ]
        Resource = "*"
      }
    ]
  })
}

# IAM Role for EFS CSI Driver using IRSA
resource "aws_iam_role" "efs_csi_driver_role" {
  name = "efs-csi-driver-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRoleWithWebIdentity"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Condition = {
          StringEquals = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub": "system:serviceaccount:kube-system:efs-csi-controller-sa"
          }
        }
      }
    ]
  })
}

# Attach the EFS policy to the role
resource "aws_iam_role_policy_attachment" "efs_csi_driver_attachment" {
  role       = aws_iam_role.efs_csi_driver_role.name
  policy_arn = aws_iam_policy.efs_csi_driver_policy.arn
}

# Create an ECR repository for your workspace controller
resource "aws_ecr_repository" "workspace_controller" {
  force_delete = true
  name                 = "workspace-controller"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "workspace-controller"
  }
}

# Create an ECR repository for your workspace controller
resource "aws_ecr_repository" "workspace_ui" {
  force_delete = true
  name                 = "workspace-ui"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "workspace-ui"
  }
}

resource "aws_ecr_repository" "workspace_images" {
  force_delete = true
  name                 = "workspace-images"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "workspace-images"
  }
}

# Set up ECR lifecycle policy (optional but recommended)
resource "aws_ecr_lifecycle_policy" "workspace_controller_lifecycle" {
  repository = aws_ecr_repository.workspace_controller.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1,
        description  = "Keep last 10 images",
        selection = {
          tagStatus     = "any",
          countType     = "imageCountMoreThan",
          countNumber   = 10
        },
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "workspace_ui_lifecycle" {
  repository = aws_ecr_repository.workspace_ui.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1,
        description  = "Keep last 10 images",
        selection = {
          tagStatus     = "any",
          countType     = "imageCountMoreThan",
          countNumber   = 10
        },
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# Create a custom policy for limited ECR access
resource "aws_iam_policy" "ecr_limited_access" {
  name        = "ECRLimitedAccessPolicy"
  description = "Policy that allows pulling only from the workspace-controller ECR repository"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability"
        ]
        Resource = aws_ecr_repository.workspace_controller.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload"
        ]
        Resource = aws_ecr_repository.workspace_images.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload"
        ]
        Resource = aws_ecr_repository.workspace_ui.arn
      },
      {
        Effect = "Allow"
        Action = "ecr:GetAuthorizationToken"
        Resource = "*"
      }
    ]
  })
}

# Attach the limited policy to the node role instead of full access
resource "aws_iam_role_policy_attachment" "ecr_limited_access_attachment" {
  policy_arn = aws_iam_policy.ecr_limited_access.arn
  role       = aws_iam_role.eks_node_role.name
}

# Output ECR repository URL
output "ecr_repository_url" {
  description = "ECR repository URL for workspace-controller"
  value       = aws_ecr_repository.workspace_controller.repository_url
}

# Add commands to push images to ECR
output "ecr_push_commands" {
  description = "Commands to authenticate and push to ECR"
  value       = <<-EOT
    # Login to ECR
    aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${aws_ecr_repository.workspace_controller.repository_url}
    
    # Build and push the image
    cd workspace-controller
    docker build -t ${aws_ecr_repository.workspace_controller.repository_url}:latest .
    docker push ${aws_ecr_repository.workspace_controller.repository_url}:latest
  EOT
}

# IAM Policy for Cert-Manager DNS-01 Route53 access
resource "aws_iam_policy" "cert_manager_route53" {
  name        = "CertManagerRoute53Policy"
  description = "Policy for cert-manager to create DNS records for ACME DNS-01 challenges"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow",
        Action   = "route53:GetChange",
        Resource = "arn:aws:route53:::change/*"
      },
      {
        Effect = "Allow",
        Action = [
          "route53:ChangeResourceRecordSets",
          "route53:ListResourceRecordSets"
        ],
        Resource = "arn:aws:route53:::hostedzone/*"
      },
      {
        Effect   = "Allow",
        Action   = "route53:ListHostedZonesByName",
        Resource = "*"
      }
    ]
  })
}

# IAM Role for Cert-Manager DNS-01 using IRSA
resource "aws_iam_role" "cert_manager_dns01_role" {
  name = "cert-manager-dns01-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRoleWithWebIdentity"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Condition = {
          StringEquals = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub": "system:serviceaccount:cert-manager:cert-manager"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role" "workspace_controller_role" {
  name = "workspace-controller-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRoleWithWebIdentity"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Condition = {
          StringLike = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub": "system:serviceaccount:*:workspace-controller"
          },
          StringEquals = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud": "sts.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role" "workspace_ui_role" {
  name = "workspace-ui-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRoleWithWebIdentity"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Condition = {
          StringLike = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub": "system:serviceaccount:*:workspace-ui"
          },
          StringEquals = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud": "sts.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "workspace_controller_role_attachment" {
  role       = aws_iam_role.workspace_controller_role.name
  policy_arn = aws_iam_policy.ecr_limited_access.arn
}

resource "aws_iam_role_policy_attachment" "workspace_ui_role_attachment" {
  role       = aws_iam_role.workspace_ui_role.name
  policy_arn = aws_iam_policy.ecr_limited_access.arn
}

# Attach the Route53 policy to the cert-manager role
resource "aws_iam_role_policy_attachment" "cert_manager_dns01_attachment" {
  role       = aws_iam_role.cert_manager_dns01_role.name
  policy_arn = aws_iam_policy.cert_manager_route53.arn
}

# Output the role ARN so we can use it in kubectl commands
output "efs_csi_driver_role_arn" {
  description = "ARN of the IAM role for EFS CSI Driver"
  value       = aws_iam_role.efs_csi_driver_role.arn
}

# Outputs
output "cluster_endpoint" {
  description = "Endpoint for EKS cluster"
  value       = aws_eks_cluster.workspace_cluster.endpoint
}

output "cluster_name" {
  description = "Name of the EKS cluster"
  value       = aws_eks_cluster.workspace_cluster.name
}

output "kubeconfig_command" {
  description = "Command to update kubeconfig"
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${aws_eks_cluster.workspace_cluster.name}"
}

output "efs_id" {
  description = "ID of the EFS filesystem"
  value       = aws_efs_file_system.workspace_efs.id
}
