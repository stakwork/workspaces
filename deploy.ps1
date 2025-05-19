# Clear any existing environment variables to ensure fresh loading
Remove-Item Env:AWS_ACCESS_KEY_ID -ErrorAction SilentlyContinue
Remove-Item Env:AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
Remove-Item Env:AWS_PROFILE -ErrorAction SilentlyContinue
Remove-Item Env:AWS_REGION -ErrorAction SilentlyContinue

# Load .env variables
$envVars = @{}
if (Test-Path ".env") {
    Get-Content .env | ForEach-Object {
        if ($_ -match "^\s*([^#][^=]*)=(.*)$") {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim().Trim('"')  # Trim extra quotes if present
            $envVars[$key] = $value
            [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
} else {
    Write-Host ".env file not found!"
    exit 1
}

# Print a few for verification
Write-Host "Loaded env vars:"
$envVars.GetEnumerator() | ForEach-Object { Write-Host "$($_.Key) = $($_.Value)" }

# Replace placeholders in kubernetes files
$filesToProcess = @(
    "kubernetes/core/workspace-domain-settings.yaml",
    "kubernetes/core/workspace-ingress-admin.yaml",
    "kubernetes/port_detector/port-detector-configmap.yaml"
)

foreach ($file in $filesToProcess) {
    if (Test-Path $file) {
        $content = Get-Content -Path $file -Raw
        foreach ($key in $envVars.Keys) {
            $pattern = "\{$($key)\}"  # Matches {DOMAIN}
            $content = $content -replace $pattern, $envVars[$key]  # No escaping
        }
        Set-Content -Path $file -Value $content
        Write-Host "Processed $file"
    } else {
        Write-Host "File not found: $file"
    }
}


# Check if AWS CLI is working now
aws sts get-caller-identity

# Step 1: Initialize and Apply Terraform
Write-Host "Step 1: Initializing and applying Terraform..."
Set-Location terraform
terraform init
terraform apply
Set-Location ..

# Step 2: Get Terraform outputs
Write-Host "Step 2: Getting Terraform outputs..."
$EFS_ID = (terraform output -raw efs_id)
$AWS_ACCOUNT_ID = (aws sts get-caller-identity --query "Account" --output text)


# Step 3: Configure kubectl
Write-Host "Step 3: Configuring kubectl..."
$kubeconfig_command = "aws eks update-kubeconfig --region us-east-1 --name workspace-cluster"
$kubeconfig_command | Invoke-Expression

# Step 4: Create namespaces
Write-Host "Step 4: Creating namespaces..."
@(
    "ingress-nginx",
    "cert-manager",
    "workspace-system",
    "monitoring"
) | ForEach-Object {
    kubectl create namespace $_ --dry-run=client -o yaml | kubectl apply -f -
}

# Step 5: Apply Kubernetes configurations and deploy components
Write-Host "Step 5: Applying Kubernetes configurations..."
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

# Step 6: Port detector
kubectl apply -f kubernetes/port_detector/port-detector-rbac.yaml
kubectl apply -f kubernetes/port_detector/port-detector-configmap.yaml

# Step 7: Deploy Controller components
Write-Host "Step 6: Deploying Controller components..."
kubectl apply -f kubernetes/workspace_controller/k8s/deployment.yaml

# Step 8: Install Nginx Ingress Controller
Write-Host "Step 7: Installing Nginx Ingress Controller..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm install nginx-ingress ingress-nginx/ingress-nginx `
    --namespace ingress-nginx `
    --set controller.service.type=LoadBalancer

# Step 9: Install cert-manager
Write-Host "Step 8: Installing cert-manager..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.12.0/cert-manager.yaml

# Step 10: Create EFS StorageClass
Write-Host "Step 9: Creating EFS StorageClass..."
@"
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: $EFS_ID
  directoryPerms: "700"
"@ | Out-File -FilePath "./kubernetes/core/storage-class.yaml" -Encoding UTF8
kubectl apply -f ./kubernetes/core/storage-class.yaml

# Step 11: Update deployment.yaml with correct image
Write-Host "Step 10: Updating deployment configuration..."
$deploymentContent = @"
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
        image: ${AWS_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/workspace-controller:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 3000
"@

$deploymentContent | Out-File -FilePath ".\kubernetes\workspace_controller\k8s\deployment.yaml" -Encoding UTF8

# Step 12: Build and push Docker image
Write-Host "Step 11: Building and pushing Docker image..."
Set-Location .\kubernetes\workspace_controller
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
docker build -t workspace-controller .
docker tag workspace-controller:latest "$AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/workspace-controller:latest"
docker push "$AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/workspace-controller:latest"
Set-Location ..



# Step 13: Update deployment.yaml with correct image
Write-Host "Step 12: Updating deployment configuration..."
$deploymentContent = @"
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
        image: ${AWS_ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/workspace-controller:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 3000
"@

$deploymentContent | Out-File -FilePath ".\workspace_controller\k8s\deployment.yaml" -Encoding UTF8
Set-Location ..


# Step 14: Verify deployment
Write-Host "Step 13: Verifying deployment..."
kubectl get pods,svc,ingress -n workspace-system

# Final Step: Display access information
Write-Host "Deployment completed!"
Write-Host "To access your application locally, we shall run these commands in the background terminals:"
# CONTROLLER Port-forward the workspace-controller service to localhost:3000
Write-Host "Port-forwarding workspace-controller service..."
Start-Process kubectl -ArgumentList "port-forward -n workspace-system svc/workspace-controller 3000:3000"

# UI Port-forward the workspace-ui service to localhost:8080
Write-Host "Port-forwarding workspace-ui service..."
Start-Process kubectl -ArgumentList "port-forward -n workspace-system svc/workspace-ui 8080:80"

Write-Host "Then access:"
Write-Host "API: http://localhost:3000"
Write-Host "UI: http://localhost:8080"
