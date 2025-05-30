#!/bin/bash
set -euo pipefail

# Load environment variables
if [[ -f ../../.env ]]; then
  set -a
  source ../../.env
  set +a
else
  echo "❌ .env file not found!"
  exit 1
fi

echo "AWS_ACCOUNT_ID: $AWS_ACCOUNT_ID"

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

TAG=$(date +%Y%m%d%H%M%S)

docker buildx build --platform linux/amd64 -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG .

docker push $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG

export DEPLOYMENT_TAG=$TAG

echo "DEPLOYMENT_TAG: $DEPLOYMENT_TAG"

envsubst < ./k8s/deployment.yaml > ./k8s/deployment-generated.yaml
kubectl apply -f ./k8s/deployment-generated.yaml
