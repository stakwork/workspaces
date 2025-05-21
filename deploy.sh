#!/bin/bash

set -e  # Exit immediately if a command exits with a non-zero status

# Clear AWS-related environment variables
unset AWS_ACCESS_KEY_ID
unset AWS_SECRET_ACCESS_KEY
unset AWS_PROFILE
unset AWS_REGION
unset AWS_HOSTED_ZONE_ID

# Load environment variables from .env
if [[ -f ".env" ]]; then
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        key=$(echo "$key" | xargs)
        value=$(echo "$value" | sed 's/^"\(.*\)"$/\1/' | xargs)
        export "$key"="$value"
    done < .env
else
    echo ".env file not found!"
    exit 1
fi

# Replace placeholders in Kubernetes YAML files
filesToProcess=(
    "kubernetes/core/workspace-certs.yaml"
    "kubernetes/core/workspace-domain-settings.yaml"
    "kubernetes/core/workspace-ingress-admin.yaml"
    "kubernetes/port_detector/port-detector-configmap.yaml"
    "kubernetes/core/workspace-cluster-issuer.yaml"
)

for file in "${filesToProcess[@]}"; do
    if [[ -f "$file" ]]; then
        content=$(cat "$file")
        for key in "${!envVars[@]}"; do
            content=${content//\{$key\}/${envVars[$key]}}
        done
        echo "$content" > "$file"
        echo "Processed $file"
    else
        echo "File not found: $file"
    fi
done

# Check AWS CLI
aws sts get-caller-identity

# Step 1: Initialize and apply Terraform
echo "Step 1: Initializing and applying Terraform..."
cd terraform
terraform init
terraform apply -auto-approve
cd ..

# Step 2: Get Terraform outputs
echo "Step 2: Getting Terraform outputs..."
EFS_ID=$(terraform -chdir=terraform output -raw efs_id)
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
AWS_REGION=${envVars["AWS_REGION"]:-us-east-1}

# Step 3: Configure kubectl
echo "Step 3: Configuring kubectl..."
aws eks update-kubeconfig --region "$AWS_REGION" --name workspace-cluster

# Step 4: Create namespaces
echo "Step 4: Creating namespaces..."
for ns in ingress-nginx cert-manager workspace-system monitoring; do
    kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

# Step 5: Install cert-manager
echo "Step 5: Installing cert-manager..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.12.0/cert-manager.yaml


# Step 6: Apply Kubernetes core configs
echo "Step 6: Applying Kubernetes configurations..."
kubectl apply -f kubernetes/core/workspace-certs.yaml
kubectl apply -f kubernetes/core/workspace-cluster-issuer.yaml
kubectl apply -f kubernetes/core/workspace-cluster-role-binding.yaml
kubectl apply -f kubernetes/core/workspace-domain-settings.yaml
kubectl apply -f kubernetes/core/workspace-ingress-admin.yaml
kubectl apply -f kubernetes/core/workspace-rbac-permissions.yaml
kubectl apply -f kubernetes/core/workspace-read-node.yaml
kubectl apply -f kubernetes/core/workspace-registry-admin.yaml
kubectl apply -f kubernetes/core/workspace-registry-service-account.yaml
kubectl apply -f kubernetes/core/workspace-registry-tls.yaml
kubectl apply -f kubernetes/core/workspace-registry.yaml
kubectl apply -f kubernetes/core/workspace-service-account.yaml
kubectl apply -f kubernetes/core/workspace-ui.yaml

# Step 7: Port detector
kubectl apply -f kubernetes/port_detector/port-detector-configmap.yaml
kubectl apply -f kubernetes/port_detector/port-detector-rbac.yaml

# Step 8: Install NGINX Ingress
echo "Step 8: Installing Nginx Ingress Controller..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm upgrade --install nginx-ingress ingress-nginx/ingress-nginx \
    --namespace ingress-nginx \
    --set controller.service.type=LoadBalancer

# Step 8: Install EFS CSI Driver
kubectl apply -k "github.com/kubernetes-sigs/aws-efs-csi-driver/deploy/kubernetes/overlays/stable/?ref=master"

# Step 9: Create EFS StorageClass
echo "Step 9: Creating EFS StorageClass..."
cat <<EOF > ./kubernetes/core/storage-class.yaml
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: $EFS_ID
  directoryPerms: "700"
EOF

kubectl apply -f ./kubernetes/core/storage-class.yaml

# Step 10: Update deployment image
echo "Step 10: Updating deployment configuration..."
cat <<EOF > ./kubernetes/workspace_controller/k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: workspace-controller
  namespace: workspace-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: workspace-controller
  template:
    metadata:
      labels:
        app: workspace-controller
    spec:
      containers:
      - name: workspace-controller
        image: ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 3000
EOF

# Step 11: Deploy Controller
echo "Step 11: Deploying Controller components..."
kubectl apply -f kubernetes/workspace_controller/k8s/deployment.yaml

# Step 12: Build and push Docker image
echo "Step 12: Building and pushing Docker image..." 
cd kubernetes/workspace_controller
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker build -t workspace-controller .
docker tag workspace-controller:latest "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest"
docker push "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest"
cd ../..

# Step 13: Verify Deployment
echo "Step 13: Verifying deployment..."
kubectl get pods,svc,ingress -n workspace-system

# Final Step: Port Forwarding
echo "Deployment completed!"
echo "Starting port-forwarding..."

kubectl port-forward -n workspace-system svc/workspace-ui 8080:80 &

echo "Access your application at:"
echo "API: http://localhost:3000"
echo "UI:  http://localhost:8080"
