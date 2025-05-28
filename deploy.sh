#!/bin/bash
set -euo pipefail

# Load environment variables
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
else
  echo "❌ .env file not found!"
  exit 1
fi

# Step 1: Initialize and apply Terraform
echo "Step 1: Initializing and applying Terraform..."
cd terraform
terraform init
terraform plan -out=tfplan
terraform apply -auto-approve
cd ..

# Step 2: Get Terraform outputs
echo "Step 2: Getting Terraform outputs..."
EFS_ID=$(terraform -chdir=terraform output -raw efs_id)
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
AWS_REGION=${AWS_REGION:-us-east-1}
export AWS_ACCOUNT_ID AWS_REGION

# Step 3: Configure kubectl for EKS
echo "Step 3: Configuring kubectl..."
aws eks update-kubeconfig --region "$AWS_REGION" --name workspace-cluster

# Step 4: Wait for nodes to be ready
echo "⏳ Waiting for all nodes to be ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s

# Step 5: Create namespaces
echo "Step 5: Creating namespaces..."
for ns in workspace-system monitoring ingress-nginx external-dns; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

# Step 5b: Install External DNS
echo "Step 5b: Installing External DNS..."
envsubst < ./kubernetes/base/apps/external-dns.yaml > ./kubernetes/base/apps/external-dns-generated.yaml
kubectl apply -f ./kubernetes/base/apps/external-dns-generated.yaml
echo "⏳ Waiting for External DNS to be ready..."
kubectl -n external-dns wait --for=condition=available deployment/external-dns --timeout=120s

# Step 6: Install cert-manager
echo "Step 6: Installing cert-manager..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.12.0/cert-manager.yaml

# Wait for cert-manager to be ready
echo "⏳ Waiting for cert-manager to be ready..."
kubectl wait --for=condition=Available deployment/cert-manager-webhook -n cert-manager --timeout=120s


envsubst < ./kubernetes/base/config/workspace-domain-settings.yaml > ./kubernetes/base/config/workspace-domain-settings-generated.yaml
kubectl apply -f ./kubernetes/base/config/workspace-domain-settings-generated.yaml
envsubst < ./kubernetes/base/ingress/workspace-ingress-admin.yaml > ./kubernetes/base/ingress/workspace-ingress-admin-generated.yaml
kubectl apply -f ./kubernetes/base/ingress/workspace-ingress-admin-generated.yaml
envsubst < ./kubernetes/cert-manager/certificates/workspace-cert.yaml > ./kubernetes/cert-manager/certificates/workspace-cert-generated.yaml
kubectl apply -f ./kubernetes/cert-manager/certificates/workspace-cert-generated.yaml
envsubst < ./kubernetes/port_detector/port-detector-configmap.yaml > ./kubernetes/port_detector/port-detector-configmap-generated.yaml
kubectl apply -f ./kubernetes/port_detector/port-detector-configmap-generated.yaml
envsubst < ./kubernetes/base/service-accounts/workspace-service-account.yaml > ./kubernetes/base/service-accounts/workspace-service-account-generated.yaml
kubectl apply -f ./kubernetes/base/service-accounts/workspace-service-account-generated.yaml

echo "Creating ClusterIssuer for cert-manager..."
AWS_HOSTED_ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$DOMAIN" --query "HostedZones[0].Id" --output text | sed 's|/hostedzone/||')
echo "Hosted Zone ID for $DOMAIN is: $AWS_HOSTED_ZONE_ID"
export AWS_HOSTED_ZONE_ID AWS_REGION EFS_ID
envsubst < ./kubernetes/cert-manager/issuers/workspace-cluster-issuer.yaml > ./kubernetes/cert-manager/issuers/workspace-cluster-issuer-generated.yaml
kubectl apply -f ./kubernetes/cert-manager/issuers/workspace-cluster-issuer-generated.yaml


# Verify AWS CLI identity
echo "🌐 Verifying AWS identity..."
if aws sts get-caller-identity > /dev/null 2>&1; then
  aws sts get-caller-identity
else
  echo "❌ AWS CLI not configured correctly."
  exit 1
fi

# Step 9: Apply additional base components
echo "Step 9: Applying additional base components..."
kubectl apply -f ./kubernetes/base/cluster-roles/workspace-cluster-role-binding.yaml
kubectl apply -f ./kubernetes/base/cluster-roles/namespace-creator-role-binding.yaml
kubectl apply -f ./kubernetes/base/rbac/workspace-rbac-permissions.yaml
kubectl apply -f ./kubernetes/base/rbac/workspace-read-node.yaml
kubectl apply -f ./kubernetes/base/rbac/workspace-registry-admin.yaml
kubectl apply -f ./kubernetes/base/service-accounts/workspace-registry-service-account.yaml

# Step 9.1: Setup Registry TLS
echo "Setting up Registry TLS certificates..."
envsubst < ./kubernetes/base/tls/workspace-registry-tls.yaml | kubectl apply -f -
echo "⏳ Waiting for certificate generation job to complete..."
kubectl wait --for=condition=complete job/create-registry-certs -n workspace-system --timeout=60s


kubectl apply -f ./kubernetes/base/apps/workspace-registry.yaml
kubectl apply -f ./kubernetes/base/apps/workspace-ui.yaml

# Step 10: Apply Port Detector RBAC
echo "Step 10: Applying port detector RBAC..."
kubectl apply -f ./kubernetes/port_detector/port-detector-rbac.yaml


# Step 12: Install NGINX Ingress Controller with Helm
echo "Step 12: Installing NGINX Ingress Controller..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true
helm repo update
if ! helm status nginx-ingress -n ingress-nginx >/dev/null 2>&1; then
  helm install nginx-ingress ingress-nginx/ingress-nginx \
    --namespace ingress-nginx \
    --set controller.service.type=LoadBalancer
else
  echo "✅ NGINX Ingress Controller already installed"
fi

# Step 13: Install AWS EFS CSI Driver
echo "Step 13: Installing AWS EFS CSI Driver..."
kubectl apply -k "github.com/kubernetes-sigs/aws-efs-csi-driver/deploy/kubernetes/overlays/stable/?ref=master"
envsubst < ./kubernetes/storage/efs-csi-controller-sa.yaml > ./kubernetes/storage/efs-csi-controller-sa-generated.yaml
kubectl apply -f ./kubernetes/storage/efs-csi-controller-sa-generated.yaml

echo "⏳ Waiting for EFS CSI Driver to be ready..."
kubectl rollout status daemonset/efs-csi-node -n kube-system --timeout=120s || true

# Step 14: Create EFS StorageClass
echo "Step 14: Creating EFS StorageClass..."
envsubst < ./kubernetes/storage/storage-class.yaml > ./kubernetes/storage/storage-class-generated.yaml
if kubectl get storageclass efs-sc >/dev/null 2>&1; then
  echo "⚠️ StorageClass 'efs-sc' already exists, skipping."
else
  kubectl apply -f ./kubernetes/storage/storage-class-generated.yaml
  echo "✅ StorageClass 'efs-sc' created."
fi

# Step 15: Create PersistentVolume
echo "Step 15: Creating PersistentVolume..."
envsubst < ./kubernetes/storage/persistent-volume.yaml > ./kubernetes/storage/persistent-volume-generated.yaml
if kubectl get pv registry-storage >/dev/null 2>&1; then
  echo "⚠️ PersistentVolume 'registry-storage' exists. Updating..."
else
  echo "ℹ️ Creating PersistentVolume 'registry-storage'..."
fi
kubectl apply -f ./kubernetes/storage/persistent-volume-generated.yaml
echo "✅ PersistentVolume 'registry-storage' applied."
# Step 16: Create PersistentVolumeClaim
echo "Step 16: Creating PersistentVolumeClaim..."
kubectl apply -f ./kubernetes/storage/persistent-volume-claim.yaml

# Generate deployment manifest with proper image name
echo "Generating deployment manifest with image: ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest"
envsubst < ./kubernetes/workspace_controller/k8s/deployment.yaml > ./kubernetes/workspace_controller/k8s/deployment-generated.yaml
echo "🚀 Deploying workspace controller..."
kubectl apply -f ./kubernetes/workspace_controller/k8s/deployment-generated.yaml
# Step 19: Deploy Controller components
echo "Step 19: Deploying Controller components..."
kubectl apply -f kubernetes/workspace_controller/k8s/service.yaml

# Step 17: Build and push Docker image BEFORE deploying
echo "Step 17: Building and pushing Docker image..."
cd kubernetes/workspace_controller
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker build -t workspace-controller .
docker tag workspace-controller:latest "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest"
docker push "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest"
cd ../..

# Step 18: Update deployment manifest with latest image
echo "Step 18: Updating deployment manifest..."
# Ensure AWS_ACCOUNT_ID and AWS_REGION are set
if [[ -z "${AWS_ACCOUNT_ID}" ]] || [[ -z "${AWS_REGION}" ]]; then
  echo "❌ AWS_ACCOUNT_ID or AWS_REGION not set"
  exit 1
fi

# Step 18: Set service account for EFS CSI Controller
echo "Step 18: Setting service account for EFS CSI Controller..."
kubectl set serviceaccount deployment/efs-csi-controller -n kube-system efs-csi-controller-sa

# Verify controller is running
echo "⏳ Waiting for workspace controller to be ready..."
kubectl rollout status deployment/workspace-controller -n workspace-system --timeout=120s

# Step 20: Verify deployment status
echo "Step 20: Verifying deployment..."
kubectl get pods,svc,ingress -n workspace-system
echo "🎉 Deployment completed successfully!"
