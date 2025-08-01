#!/bin/bash
set -euo pipefail

# Load environment variables
if [[ -f ../../../.env ]]; then
  set -a
  source ../../../.env
  set +a
else
  echo "❌ .env file not found!"
  exit 1
fi

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)

TAG=$(date +%Y%m%d%H%M%S)

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker buildx build --platform linux/amd64 --push \
  -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG \
  -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:latest .

export DEPLOYMENT_TAG=$TAG

envsubst < ../k8s/deployment.yaml > ../k8s/deployment-generated.yaml

echo "🚀 Applying deployment to cluster..."
kubectl apply -f ../k8s/deployment-generated.yaml

cd ../frontend

docker buildx build --platform linux/amd64 --push \
  -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-ui:$TAG \
  -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-ui:latest .

envsubst < ../../base/apps/workspace-ui.yaml > ../../base/apps/workspace-ui-generated.yaml
kubectl apply -f ../../base/apps/workspace-ui-generated.yaml

echo "⏳ Waiting for rollout to complete..."
kubectl rollout status deployment/workspace-controller -n workspace-system

echo "✅ Deployment completed!"
kubectl get pods -l app=workspace-controller -n workspace-system
