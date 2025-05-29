NGINX_RELEASE=$(helm list -A | grep nginx-ingress | awk '{print $1}')
NGINX_NAMESPACE=$(helm list -A | grep nginx-ingress | awk '{print $2}')

if [ -n "$NGINX_RELEASE" ] && [ -n "$NGINX_NAMESPACE" ]; then
  echo "Uninstalling Helm release: $NGINX_RELEASE in namespace: $NGINX_NAMESPACE"
  helm uninstall "$NGINX_RELEASE" --namespace "$NGINX_NAMESPACE"
  kubectl delete namespace "$NGINX_NAMESPACE" --ignore-not-found
else
  echo "No nginx-ingress Helm release found. Skipping."
fi

helm repo list | grep -q ingress-nginx && helm repo remove ingress-nginx

cd terraform
terraform destroy -auto-approve
