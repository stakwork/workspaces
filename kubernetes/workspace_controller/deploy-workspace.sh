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

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)

docker buildx build --platform linux/amd64 -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:latest --push .

TAG=$(date +%Y%m%d%H%M%S)

docker tag $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:latest $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG

docker push $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG

export DEPLOYMENT_TAG=$TAG

envsubst < ./k8s/deployment.yaml > ./k8s/deployment-generated.yaml
