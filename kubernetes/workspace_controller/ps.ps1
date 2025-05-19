# Set the timestamp as the tag
$TAG = (Get-Date -Format "yyyyMMddHHmmss")

# Build and push the Docker image
docker buildx build --platform linux/amd64 -t xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:latest --push .

# Tag the image with the generated timestamp tag
docker tag xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:latest xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG

# Push the newly tagged image
docker push xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-controller:$TAG

# Output the tag
echo $TAG

# Apply the Kubernetes manifest
kubectl apply -f k8s/deployment.yaml
