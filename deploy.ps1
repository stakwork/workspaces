# === Load .env variables into $envVars and environment ===
$envVars = @{}

if (Test-Path ".env") {
    Get-Content .env | ForEach-Object {
        if ($_ -match "^\s*([^#][^=]*)=(.*)$") {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim().Trim('"')  # Remove quotes if any
            $envVars[$key] = $value
            [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
} else {
    Write-Host ".env file not found!"
    exit 1
}

Write-Host "Loaded env vars:"
$envVars.GetEnumerator() | ForEach-Object { Write-Host "$($_.Key) = $($_.Value)" }

# === Define function to expand template variables ===
function Expand-TemplateString {
    param (
        [string]$content,
        [hashtable]$variables
    )

    foreach ($key in $variables.Keys) {
        $placeholder = "\$\{$key\}"  # Match ${KEY}
        $value = [Regex]::Escape($variables[$key])
        $content = [regex]::Replace($content, $placeholder, $value)
    }

    return $content
}

# === Process YAML templates in kubernetes/core ===
$templatesDir = "kubernetes/core"
$templates = Get-ChildItem -Path $templatesDir -Filter *.yaml

foreach ($template in $templates) {
    try {
        $content = Get-Content -Path $template.FullName -Raw
        $expandedContent = Expand-TemplateString -content $content -variables $envVars
        Set-Content -Path $template.FullName -Value $expandedContent -Encoding UTF8 -Force
        Write-Host "Processed $($template.Name)"
    } catch {
        Write-Host "Error processing template $($template.Name): $_"
        exit 1
    }
}

# === Process YAML templates in kubernetes/port_detector ===
$portDetectorDir = "kubernetes/port_detector"
$portDetectorTemplates = Get-ChildItem -Path $portDetectorDir -Filter *.yaml

foreach ($template in $portDetectorTemplates) {
    try {
        $content = Get-Content -Path $template.FullName -Raw
        $expandedContent = Expand-TemplateString -content $content -variables $envVars
        Set-Content -Path $template.FullName -Value $expandedContent -Encoding UTF8 -Force
        Write-Host "Processed $($template.Name)"
    } catch {
        Write-Host "Error processing template $($template.Name): $_"
        exit 1
    }
}


# Check if AWS CLI is working now
aws sts get-caller-identity

# Step 1: Initialize and Apply Terraform
Write-Host "Step 1: Initializing and applying Terraform..."
$terraformPath = Join-Path -Path $PSScriptRoot -ChildPath "terraform"
Push-Location -Path $terraformPath
try {
    terraform init
    terraform apply -auto-approve
} catch {
    Write-Error "Error during Terraform operations: $_"
    Pop-Location
    exit 1
}
Pop-Location

# Step 2: Get Terraform outputs
Write-Host "Step 2: Getting Terraform outputs..."
$EFS_ID = (terraform output -raw efs_id)
$AWS_ACCOUNT_ID = (aws sts get-caller-identity --query "Account" --output text)
$AWS_REGION = (aws configure get region)

# Step 3: Configure kubectl
Write-Host "Step 3: Configuring kubectl..."
$kubeconfig_command = "aws eks update-kubeconfig --region $AWS_REGION --name workspace-cluster"
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

# Step 5: Install cert-manager
Write-Host "Step 5: Installing cert-manager..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.12.0/cert-manager.yaml


# Step 6: Apply Kubernetes configurations and deploy components
Write-Host "Step 6: Applying Kubernetes configurations..."
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
kubectl apply -f kubernetes/port_detector/port-detector-rbac.yaml
kubectl apply -f kubernetes/port_detector/port-detector-configmap.yaml


# Step 8: Install Nginx Ingress Controller
Write-Host "Step 8: Installing Nginx Ingress Controller..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm upgrade --install nginx-ingress ingress-nginx/ingress-nginx `
    --namespace ingress-nginx `
    --set controller.service.type=LoadBalancer


# Step 9: Install EFS CSI Driver
kubectl apply -k "github.com/kubernetes-sigs/aws-efs-csi-driver/deploy/kubernetes/overlays/stable/?ref=master"

# Step 10: Create EFS StorageClass
Write-Host "Step 10: Creating EFS StorageClass..."
$storageClassPath = Join-Path -Path $PSScriptRoot -ChildPath "kubernetes/core/storage-class.yaml"
$storageClassContent = @"
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: ${EFS_ID}
  directoryPerms: "700"
"@
try {
    $storageClassContent | Out-File -FilePath $storageClassPath -Encoding UTF8 -Force
    kubectl apply -f $storageClassPath
} catch {
    Write-Error "Error creating storage class: $_"
}

# Step 11: Update deployment.yaml with correct image
Write-Host "Step 11: Updating deployment configuration..."
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
        image: ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 3000
"@

$deploymentContent | Out-File -FilePath ".\kubernetes\workspace_controller\k8s\deployment.yaml" -Encoding UTF8

# Step 12: Deploy Controller components
Write-Host "Step 12: Deploying Controller components..."
kubectl apply -f kubernetes/workspace_controller/k8s/deployment.yaml

# Step 13: Build and push Docker image
Write-Host "Step 13: Building and pushing Docker image..."
$controllerPath = Join-Path -Path $PSScriptRoot -ChildPath "kubernetes/workspace_controller"
Push-Location -Path $controllerPath
try {
    aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
    docker build -t workspace-controller .
    docker tag workspace-controller:latest "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/workspace-controller:latest"
    docker push "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/workspace-controller:latest"
} catch {
    Write-Error "Error during Docker operations: $_"
    Pop-Location
    exit 1
}
Pop-Location



# Step 14: Verify deployment
Write-Host "Step 14: Verifying deployment..."
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
        image: ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/workspace-controller:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 3000
"@

$deploymentContent | Out-File -FilePath ".\kubernetes\workspace_controller\k8s\deployment.yaml" -Encoding UTF8
Set-Location .


# Step 15: Verify deployment
Write-Host "Step 15: Verifying deployment..."
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
