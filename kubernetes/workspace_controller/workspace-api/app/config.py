import os
import json
import base64
import logging
from kubernetes import client, config

logger = logging.getLogger(__name__)

class Config:
    def __init__(self):
        self.JWT_SECRET_KEY = None
        self.USERS_CONFIG = None
        self.DOMAIN = None
        self.PARENT_DOMAIN = None
        self.WORKSPACE_DOMAIN = None
        self.AWS_ACCOUNT_ID = None
        self.core_v1 = None
        self.apps_v1 = None
        self.networking_v1 = None
        
        self._init_kubernetes()
        self._load_config()
        self._load_auth_config()
    
    def _init_kubernetes(self):
        """Initialize Kubernetes clients"""
        try:
            # Load in-cluster config
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.config_exception.ConfigException:
            # Load kubeconfig for local development
            config.load_kube_config()
            logger.info("Loaded kubeconfig for local development")

        # Initialize Kubernetes clients
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.networking_v1 = client.NetworkingV1Api()
    
    def _load_config(self):
        """Load configuration from ConfigMap"""
        try:
            config_map = self.core_v1.read_namespaced_config_map("workspace-config", "workspace-system")
            self.DOMAIN = config_map.data.get("domain", "SUBDOMAIN_REPLACE_ME")
            self.PARENT_DOMAIN = config_map.data.get("parent-domain", "REPLACE_ME")
            self.WORKSPACE_DOMAIN = config_map.data.get("workspace-domain", "SUBDOMAIN_REPLACE_ME")
            self.AWS_ACCOUNT_ID = config_map.data.get("aws-account-id", "AWS_ACCOUNT_ID")
            logger.info(f"Using domain: {self.DOMAIN}, parent domain: {self.PARENT_DOMAIN}, workspace domain: {self.WORKSPACE_DOMAIN}")
        except Exception as e:
            logger.error(f"Error reading config map: {e}")
            self.DOMAIN = "SUBDOMAIN_REPLACE_ME"
            self.PARENT_DOMAIN = "REPLACE_ME"
            self.WORKSPACE_DOMAIN = "SUBDOMAIN_REPLACE_ME"
            self.AWS_ACCOUNT_ID = "AWS_ACCOUNT_ID_REPLACE_ME"
    
    def _load_auth_config(self):
        """Load authentication configuration"""
        try:
            # Get JWT secret from Kubernetes secret
            secret = self.core_v1.read_namespaced_secret("workspace-auth-secret", "workspace-system")
            self.JWT_SECRET_KEY = base64.b64decode(secret.data.get("jwt-secret")).decode('utf-8')
            logger.info("Loaded JWT secret from Kubernetes")
        except Exception as e:
            logger.error(f"Error loading JWT secret: {e}")
            self.JWT_SECRET_KEY = "fallback-secret-key-change-this"
        
        try:
            # Get users from ConfigMap
            config_map = self.core_v1.read_namespaced_config_map("workspace-users", "workspace-system")
            self.USERS_CONFIG = json.loads(config_map.data.get("users.json", '{"users": []}'))
            logger.info(f"Loaded {len(self.USERS_CONFIG.get('users', []))} users")
        except Exception as e:
            logger.error(f"Error loading users config: {e}")

# Global config instance
app_config = Config()