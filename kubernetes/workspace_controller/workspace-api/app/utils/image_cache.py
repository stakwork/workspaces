import json
import logging
from datetime import datetime
from kubernetes import client
from app.config import app_config
from app.utils.git_utils import generate_cache_key
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class ImageCacheService:
    """Service for managing cached workspace images"""
    
    def __init__(self):
        self.core_v1 = app_config.core_v1
        self.cache_namespace = "workspace-system"
        self.cache_configmap_name = "image-cache"
    
    def get_cached_wrapper_image(self, workspace_config):
        """
        Check if cached wrapper image exists for the given workspace configuration.
        
        Args:
            workspace_config (dict): The workspace configuration
        
        Returns:
            dict: Cache info with wrapper_image_tag if found, None otherwise
        """
        try:
            cache_key = generate_cache_key(workspace_config)
            
            # Get the cache ConfigMap
            try:
                cache_cm = self.core_v1.read_namespaced_config_map(
                    name=self.cache_configmap_name,
                    namespace=self.cache_namespace
                )
                cache_data = json.loads(cache_cm.data.get("cache", "{}"))
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    logger.info("Image cache ConfigMap doesn't exist yet")
                    return None
                raise
            
            # Check if cache entry exists
            if cache_key in cache_data:
                cache_entry = cache_data[cache_key]
                logger.info(f"Found cached wrapper image for key {cache_key}: {cache_entry}")
                
                # Verify wrapper image still exists in ECR
                if self._verify_wrapper_image_exists(cache_entry):
                    cache_entry['cache_hit'] = True
                    return cache_entry
                else:
                    logger.info(f"Cached wrapper image for key {cache_key} no longer exists, will rebuild")
                    # Remove stale cache entry
                    self._remove_cache_entry(cache_key)
                    
            return None
            
        except Exception as e:
            logger.error(f"Error checking image cache: {e}")
            return None
    
    def store_cached_wrapper_image(self, workspace_config, wrapper_image_tag, base_image_tag):
        """
        Store cached wrapper image information.
        
        Args:
            workspace_config (dict): The workspace configuration
            wrapper_image_tag (str): The wrapper image tag
            base_image_tag (str): The base image tag used to build the wrapper
        """
        try:
            cache_key = generate_cache_key(workspace_config)
            
            cache_entry = {
                "wrapper_image_tag": wrapper_image_tag,
                "base_image_used": base_image_tag,
                "created": datetime.now().isoformat(),
                "repositories": workspace_config['github_urls'],
                "branches": workspace_config['github_branches'],
                "image_config": {
                    "use_custom_image_url": workspace_config['use_custom_image_url'],
                    "custom_image": workspace_config.get('custom_image'),
                    "custom_image_url": workspace_config.get('custom_image_url'),
                    "use_dev_container": workspace_config['use_dev_container']
                }
            }
            
            # Get existing cache or create new one
            try:
                cache_cm = self.core_v1.read_namespaced_config_map(
                    name=self.cache_configmap_name,
                    namespace=self.cache_namespace
                )
                cache_data = json.loads(cache_cm.data.get("cache", "{}"))
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    cache_data = {}
                    # Create the ConfigMap
                    self._create_cache_configmap()
                else:
                    raise
            
            # Add new cache entry
            cache_data[cache_key] = cache_entry
            
            # Clean up old entries (keep last 50)
            if len(cache_data) > 50:
                sorted_entries = sorted(
                    cache_data.items(), 
                    key=lambda x: x[1].get('created', ''), 
                    reverse=True
                )
                cache_data = dict(sorted_entries[:50])
            
            # Update ConfigMap
            patch_body = {
                "data": {
                    "cache": json.dumps(cache_data, indent=2)
                }
            }
            
            self.core_v1.patch_namespaced_config_map(
                name=self.cache_configmap_name,
                namespace=self.cache_namespace,
                body=patch_body
            )
            
            logger.info(f"Stored cache entry for key {cache_key}")
            
        except Exception as e:
            logger.error(f"Error storing image cache: {e}")
    
    def _verify_wrapper_image_exists(self, cache_entry):
        """
        Verify that the cached wrapper image still exists in ECR.
        
        Args:
            cache_entry (dict): The cache entry to verify
        
        Returns:
            bool: True if wrapper image exists, False otherwise
        """
        try:
            ecr_client = boto3.client('ecr', region_name='us-east-1')
            
            wrapper_tag = cache_entry['wrapper_image_tag'].split(':')[-1]
            
            # Check if wrapper image exists
            try:
                ecr_client.describe_images(
                    repositoryName='workspace-images',
                    imageIds=[
                        {'imageTag': wrapper_tag}
                    ]
                )
                logger.info(f"Verified cached wrapper image exists: {wrapper_tag}")
                return True
                
            except ClientError as e:
                if e.response['Error']['Code'] == 'ImageNotFoundException':
                    logger.info(f"Cached wrapper image not found in ECR: {wrapper_tag}")
                    return False
                raise
                
        except Exception as e:
            logger.error(f"Error verifying cached wrapper image: {e}")
            return False
    
    def _remove_cache_entry(self, cache_key):
        """Remove a stale cache entry"""
        try:
            cache_cm = self.core_v1.read_namespaced_config_map(
                name=self.cache_configmap_name,
                namespace=self.cache_namespace
            )
            cache_data = json.loads(cache_cm.data.get("cache", "{}"))
            
            if cache_key in cache_data:
                del cache_data[cache_key]
                
                patch_body = {
                    "data": {
                        "cache": json.dumps(cache_data, indent=2)
                    }
                }
                
                self.core_v1.patch_namespaced_config_map(
                    name=self.cache_configmap_name,
                    namespace=self.cache_namespace,
                    body=patch_body
                )
                
                logger.info(f"Removed stale cache entry: {cache_key}")
                
        except Exception as e:
            logger.error(f"Error removing cache entry: {e}")
    
    def _create_cache_configmap(self):
        """Create the image cache ConfigMap if it doesn't exist"""
        try:
            cache_cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name=self.cache_configmap_name,
                    namespace=self.cache_namespace,
                    labels={"app": "workspace-image-cache"}
                ),
                data={
                    "cache": json.dumps({}, indent=2)
                }
            )
            
            self.core_v1.create_namespaced_config_map(
                namespace=self.cache_namespace,
                body=cache_cm
            )
            
            logger.info(f"Created image cache ConfigMap in {self.cache_namespace}")
            
        except client.exceptions.ApiException as e:
            if e.status == 409:  # Already exists
                logger.info("Image cache ConfigMap already exists")
            else:
                raise
    
    def list_cached_images(self):
        """
        List all cached images with their metadata.
        
        Returns:
            dict: Cache data with all entries
        """
        try:
            cache_cm = self.core_v1.read_namespaced_config_map(
                name=self.cache_configmap_name,
                namespace=self.cache_namespace
            )
            cache_data = json.loads(cache_cm.data.get("cache", "{}"))
            
            return cache_data
            
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return {}
            raise
        except Exception as e:
            logger.error(f"Error listing cached images: {e}")
            return {}
    
    def clear_cache(self):
        """Clear all cached image entries"""
        try:
            patch_body = {
                "data": {
                    "cache": json.dumps({}, indent=2)
                }
            }
            
            self.core_v1.patch_namespaced_config_map(
                name=self.cache_configmap_name,
                namespace=self.cache_namespace,
                body=patch_body
            )
            
            logger.info("Cleared image cache")
            return True
            
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return False


# Global service instance
image_cache_service = ImageCacheService()