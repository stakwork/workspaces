import logging
import base64
import json
from kubernetes import client

logger = logging.getLogger(__name__)

def init_kubernetes_clients():
    """Initialize and return Kubernetes API clients"""
    try:
        # Try loading in-cluster config first
        client.config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration")
    except client.config.ConfigException:
        # Fall back to local kubeconfig
        client.config.load_kube_config()
        logger.info("Loaded local Kubernetes configuration")
    
    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    return core_v1, apps_v1

def load_config_from_k8s(core_v1):
    """Load configuration from Kubernetes ConfigMap"""
    try:
        config_map = core_v1.read_namespaced_config_map(
            "workspace-config", 
            "workspace-system"
        )
        
        return {
            "domain": config_map.data.get("domain", "SUBDOMAIN_REPLACE_ME"),
            "parent_domain": config_map.data.get("parent-domain", "REPLACE_ME"),
            "workspace_domain": config_map.data.get("workspace-domain", "SUBDOMAIN_REPLACE_ME"),
            "aws_account_id": config_map.data.get("aws-account-id", "AWS_ACCOUNT_ID")
        }
    except Exception as e:
        logger.error(f"Error loading config from ConfigMap: {e}")
        return {
            "domain": "SUBDOMAIN_REPLACE_ME",
            "parent_domain": "REPLACE_ME",
            "workspace_domain": "SUBDOMAIN_REPLACE_ME",
            "aws_account_id": "AWS_ACCOUNT_ID_REPLACE_ME"
        }

def load_auth_config(core_v1):
    """Load authentication configuration from Kubernetes secrets"""
    try:
        # Get JWT secret from Kubernetes secret
        secret = core_v1.read_namespaced_secret(
            "workspace-auth-secret",
            "workspace-system"
        )
        jwt_secret = base64.b64decode(secret.data.get("jwt-secret")).decode('utf-8')
        
        # Get users from ConfigMap
        config_map = core_v1.read_namespaced_config_map(
            "workspace-users",
            "workspace-system"
        )
        users_config = json.loads(config_map.data.get("users.json", '{"users": []}'))
        
        return jwt_secret, users_config
        
    except Exception as e:
        logger.error(f"Error loading auth config: {e}")
        return "fallback-secret-key-change-this", {"users": []}
