#!/bin/bash

# Load environment variables from .env file
source .env

# Process Kubernetes templates
TEMPLATES_DIR="kubernetes/core"
TEMPLATES=$(find "$TEMPLATES_DIR" -type f -name "*.yaml")

for TEMPLATE in $TEMPLATES; do
  echo "Processing $TEMPLATE..."
  envsubst < "$TEMPLATE" > "$TEMPLATE.tmp"
  mv "$TEMPLATE.tmp" "$TEMPLATE"
  echo "Processed $TEMPLATE"
done

TEMPLATES_DIR="kubernetes/port_detector"
TEMPLATES=$(find "$TEMPLATES_DIR" -type f -name "*.yaml")

for TEMPLATE in $TEMPLATES; do
  echo "Processing $TEMPLATE..."
  envsubst < "$TEMPLATE" > "$TEMPLATE.tmp"
  mv "$TEMPLATE.tmp" "$TEMPLATE"
  echo "Processed $TEMPLATE"
done

# Create/Update ConfigMap
echo "Creating/Updating ConfigMap..."
kubectl apply -f kubernetes/config/configmap.yaml

# Create/Update Secrets
echo "Creating/Updating Secrets..."
kubectl apply -f kubernetes/config/secrets.yaml


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
REGION=${envVars["REGION"]:-us-east-1}

# Step 3: Configure kubectl
echo "Step 3: Configuring kubectl..."
aws eks update-kubeconfig --region "$REGION" --name workspace-cluster

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

# Step 6.1: Update cluster issuer with the correct values
echo "Step 6.1: Updating ClusterIssuer configuration..."
cat <<EOF > ./kubernetes/core/workspace-cluster-issuer.yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-dns01
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: gonzaloaune@stakwork.com
    privateKeySecretRef:
      name: letsencrypt-dns01-account-key
    solvers:
    - dns01:
        route53:
          region: ${AWS_REGION}
          hostedZoneID: ${AWS_HOSTED_ZONE_ID}
EOF

# Apply the ClusterIssuer configuration
echo "Step 6.1: Applying ClusterIssuer configuration..."
kubectl apply -f ./kubernetes/core/workspace-cluster-issuer.yaml

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
  fileSystemId: ${EFS_ID}
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
