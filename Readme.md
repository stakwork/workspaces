# Initialize Terraform
terraform init

# Apply the Terraform configuration
terraform apply

# Configure kubectl to work with the EKS cluster
aws --profile MY_PROFILE eks update-kubeconfig --region us-east-1 --name workspace-cluster

# Add the Nginx Ingress Controller repo
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

# Install Nginx Ingress Controller
helm install nginx-ingress ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.type=LoadBalancer

  # Install cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.12.0/cert-manager.yaml

# Wait for cert-manager to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=cert-manager -n cert-manager

# Create a wildcard cert and ClusterIssuer for Let's Encrypt
kubectl apply -f workspace-certs.yaml
kubectl apply -f workspace-cluster-issuer.yaml

# Get EFS ID from Terraform output
EFS_ID=$(terraform output -raw efs_id)

# Install EFS CSI Driver
kubectl apply -k "github.com/kubernetes-sigs/aws-efs-csi-driver/deploy/kubernetes/overlays/stable/?ref=master"

# Create a StorageClass for EFS
cat <<EOF | kubectl apply -f -
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

# Create a namespace for external-dns
kubectl create namespace external-dns

# Create a service account for external-dns
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: external-dns
  namespace: external-dns
EOF

# Create RBAC permissions for external-dns
cat <<EOF | kubectl apply -f -
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: external-dns
rules:
- apiGroups: [""]
  resources: ["services","endpoints","pods"]
  verbs: ["get","watch","list"]
- apiGroups: ["extensions","networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get","watch","list"]
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["list","watch"]
EOF

cat <<EOF | kubectl apply -f -
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: external-dns-viewer
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: external-dns
subjects:
- kind: ServiceAccount
  name: external-dns
  namespace: external-dns
EOF

# Deploy external-dns with the IAM role
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
ZONE_ID=REPLACE_ME
DOMAIN_NAME=REPLACE_ME

cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: external-dns
  namespace: external-dns
spec:
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: external-dns
  template:
    metadata:
      labels:
        app: external-dns
    spec:
      serviceAccountName: external-dns
      containers:
      - name: external-dns
        image: k8s.gcr.io/external-dns/external-dns:v0.12.0
        args:
        - --source=ingress
        - --provider=aws
        - --aws-zone-type=public
        - --domain-filter=${DOMAIN_NAME}
        - --registry=txt
        - --txt-owner-id=${ZONE_ID}
      securityContext:
        fsGroup: 65534
EOF

# Create a namespace for the workspace controller
kubectl create namespace workspace-system

# Create a ConfigMap with the domain settings
kubectl apply -f workspace-domain-settings.yaml

# Create a service account for the controller
kubectl apply -f workspace-service-account.yaml

# Create RBAC permissions
kubectl apply -f workspace-rbac-permissions.yaml

# Cluster Role Binding
kubectl apply -f workspace-cluster-role-binding.yaml

# Create the port forwarding sidecar deployment
kubectl apply -f port_detector/*.yaml

# Create the controller deployment
kubectl apply -f workspace_controller/k8s/deployment.yaml

# Create a deployment for the admin UI
kubectl apply -f workspace-ui.yaml

# Create ingress for the workspace admin UI
kubectl apply -f workspace-ingress-admin.yaml

# Install Prometheus and Grafana for monitoring
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install prometheus prometheus-community/prometheus \
  --namespace monitoring \
  --create-namespace

helm install grafana grafana/grafana \
  --namespace monitoring \
  --set persistence.enabled=true \
  --set adminPassword=MY_ADMIN_PASSWORD


# Apply Horizontal Pod Autoscaler for the workspace deployments
cat <<EOF | kubectl apply -f -
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: workspace-controller
  namespace: workspace-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: workspace-controller
  minReplicas: 1
  maxReplicas: 5
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 80
EOF

ROLE_ARN=$(terraform output -raw efs_csi_driver_role_arn)
kubectl annotate serviceaccount -n kube-system efs-csi-controller-sa --overwrite \
  eks.amazonaws.com/role-arn=$ROLE_ARN

kubectl rollout restart deployment efs-csi-controller -n kube-system
kubectl get serviceaccount efs-csi-controller-sa -n kube-system -o yaml

# Update security group for EFS -> EKS
aws ec2 authorize-security-group-ingress \
  --group-id sg-0e0fd53dc0dcb9546 \
  --protocol tcp \
  --port 2049 \
  --source-group sg-0e46cfe9e2ee450e7
