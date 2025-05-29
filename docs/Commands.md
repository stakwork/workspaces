kubectl delete namespaces $WORKSPACE
kubectl delete pod $POD -n $WORKSPACE

kubectl get namespaces | grep workspace

WORKSPACE=workspace-37d57a06

kubectl get pods -n $WORKSPACE

POD=code-server-7689b98f8-rv9hp

#kubectl delete pod -n $WORKSPACE 

# Log for init-workspace code-server
kubectl logs -n $WORKSPACE $POD -c code-server

# Log for init-workspace container
kubectl logs -n $WORKSPACE $POD -c init-workspace

# Status of Pod
kubectl get pods -n $WORKSPACE $POD -o wide

# ALL events
kubectl get events -n $WORKSPACE --sort-by='.lastTimestamp'

# Login to container
kubectl exec -it $POD -n $WORKSPACE -c code-server -- /bin/bash
kubectl exec -it $POD -n $WORKSPACE -c init-workspace -- /bin/bash


# GET ALL Pods
kubectl get pods --all-namespaces | grep -i terminating

# FORCE Delete Pod
kubectl delete pod code-server-d78dd465-5bht9 -n workspace-ca1e7d8a --grace-period=0 --force

kubectl delete pod -n workspace-system -l app=workspace-controller

# Edit Deployment
kubectl edit deployment code-server -n $WORKSPACE

# Tail Logs
kubectl logs -f -n workspace-system deployment/workspace-controller -c port-detector

kubectl exec -it workspace-controller-7dbb9bcf64-rvmnx -n workspace-system -c port-detector -- /bin/bash

kubectl get configmap -n workspace-system workspace-info -o jsonpath='{.data.info}'

#### MORE....

kubectl get deployment code-server -n workspace-493ab75a -o yaml | grep -A 2 hostNetwork

kubectl get pods -n workspace-f6490baf -l app=code-server -o wide

kubectl get namespace workspace-493ab75a -o yaml | grep pod-security

kubectl get events -n workspace-493ab75a | grep -i warn
kubectl get events -n workspace-493ab75a | grep -i error
kubectl get events -n workspace-493ab75a | grep -i deny


# Execute into the container and check listening ports
kubectl exec -it -n workspace-f6490baf code-server-786ddf4546-r44vj -c code-server -- netstat -tulpn


kubectl get endpoints -n workspace-f6490baf code-server -o yaml



kubectl logs -n workspace-5c1473ff code-server-786ddf4546-r44vj -c port-detector