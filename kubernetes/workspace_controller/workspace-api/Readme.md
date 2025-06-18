# Build & Push the Docker image
docker buildx build --platform linux/amd64 -t xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:latest --push .

TAG=$(date +%Y%m%d%H%M%S)

docker tag xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:latest xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG

docker push xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG

echo $TAG

# Apply the Kubernetes manifest
kubectl apply -f k8s/deployment.yaml