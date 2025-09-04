import json
import logging
import threading
import time
import re
from typing import List, Dict, Optional
from datetime import datetime
from kubernetes import client
from app.config import app_config
from app.workspace.service import workspace_service
from app.pool.models import PoolConfig, PoolStatus
from app.user.service import user_service
import requests
from urllib3.exceptions import InsecureRequestWarning
import urllib3

logger = logging.getLogger(__name__)

urllib3.disable_warnings(InsecureRequestWarning)

def sanitize_k8s_name(name: str) -> str:
    """
    Sanitize a name to be valid for Kubernetes resources.
    
    Kubernetes names must:
    - consist of lower case alphanumeric characters, '-' or '.'
    - start and end with an alphanumeric character
    - be no more than 253 characters
    """
    # Convert to lowercase
    sanitized = name.lower()
    
    # Replace invalid characters with hyphens
    sanitized = re.sub(r'[^a-z0-9.-]', '-', sanitized)
    
    # Ensure it starts and ends with alphanumeric
    sanitized = re.sub(r'^[^a-z0-9]+', '', sanitized)
    sanitized = re.sub(r'[^a-z0-9]+$', '', sanitized)
    
    # Replace multiple consecutive hyphens with single hyphen
    sanitized = re.sub(r'-+', '-', sanitized)
    
    # Truncate if too long (leave room for prefixes like "pool-")
    if len(sanitized) > 240:
        sanitized = sanitized[:240]
    
    # Ensure it's not empty
    if not sanitized:
        sanitized = "default"
    
    return sanitized


def sanitize_k8s_label(value: str) -> str:
    """
    Sanitize a value to be valid for Kubernetes labels.
    
    Kubernetes labels must:
    - consist of alphanumeric characters, '-', '_' or '.'
    - start and end with an alphanumeric character
    - be no more than 63 characters
    """
    # Replace invalid characters with hyphens
    sanitized = re.sub(r'[^A-Za-z0-9._-]', '-', value)
    
    # Ensure it starts and ends with alphanumeric
    sanitized = re.sub(r'^[^A-Za-z0-9]+', '', sanitized)
    sanitized = re.sub(r'[^A-Za-z0-9]+$', '', sanitized)
    
    # Replace multiple consecutive hyphens with single hyphen
    sanitized = re.sub(r'-+', '-', sanitized)
    
    # Truncate if too long
    if len(sanitized) > 63:
        sanitized = sanitized[:63]
    
    # Ensure it's not empty
    if not sanitized:
        sanitized = "default"
    
    return sanitized


class PoolService:
    """Service for managing workspace pools"""
    
    def __init__(self):
        self.core_v1 = app_config.core_v1
        self.pools: Dict[str, PoolConfig] = {}
        self.pool_owners: Dict[str, str] = {}  # pool_name -> username
        self.monitoring_threads: Dict[str, threading.Thread] = {}
        self.stop_monitoring: Dict[str, threading.Event] = {}
        self.scaling_locks: Dict[str, threading.Lock] = {}
        self._load_existing_pools()
    
    def create_pool(self, pool_name: str, minimum_vms: int, repo_name: str, 
                   branch_name: str, github_pat: str, github_username: str, 
                   env_vars: List[Dict] = None, owner_username: str = None,
                   devcontainer_json: str = None, dockerfile: str = None,
                   docker_compose_yml: str = None, pm2_config_js: str = None,
                   cpu: str = None, memory: str = None) -> Dict:
        """Create a new pool"""
        try:
            # Validate inputs
            if pool_name in self.pools:
                raise ValueError(f"Pool '{pool_name}' already exists")
            
            if not owner_username:
                raise ValueError("Owner username is required")
            
            if minimum_vms < 1:
                raise ValueError("minimum_vms must be at least 1")
            
            # Validate pool name for basic requirements
            if not pool_name or len(pool_name.strip()) == 0:
                raise ValueError("Pool name cannot be empty")
            
            if len(pool_name) > 253:
                raise ValueError("Pool name is too long (max 253 characters)")
            
            if env_vars:
                for env_var in env_vars:
                    if not env_var.get('name') or not isinstance(env_var.get('name'), str):
                        raise ValueError("Environment variable name must be a non-empty string")
                    if env_var.get('value') is None:
                        raise ValueError("Environment variable value cannot be None")
            
            actual_github_pat = github_pat
            if isinstance(github_pat, dict):
                actual_github_pat = github_pat.get('value', '')

            # Create pool configuration
            pool_config = PoolConfig(
                pool_name=pool_name,
                minimum_vms=minimum_vms,
                repo_name=repo_name,
                branch_name=branch_name,
                github_pat=actual_github_pat,
                github_username=github_username,
                env_vars=env_vars or [],
                devcontainer_json=devcontainer_json,
                dockerfile=dockerfile,
                docker_compose_yml=docker_compose_yml,
                pm2_config_js=pm2_config_js,
                cpu=cpu or "2",
                memory=memory or "8Gi"
            )
            
            # Store pool configuration in Kubernetes
            self._store_pool_config(pool_config, owner_username)
            
            # Add to local cache
            self.pools[pool_name] = pool_config
            self.pool_owners[pool_name] = owner_username
            
            # Create scaling lock for this pool
            self.scaling_locks[pool_name] = threading.Lock()
            
            # Start monitoring thread
            self._start_pool_monitoring(pool_name)
            
            # Create initial workspaces (with lock to prevent double creation)
            with self.scaling_locks[pool_name]:
                self._scale_pool(pool_name)
            
            user_service.add_pool_to_user(owner_username, pool_name)

            logger.info(f"Created pool '{pool_name}' with {minimum_vms} minimum VMs for user '{owner_username}'")
            
            return {
                "success": True,
                "message": f"Pool '{pool_name}' created successfully",
                "pool": pool_config.to_dict(mask_sensitive=True),
                "owner": owner_username
            }
            
        except Exception as e:
            logger.error(f"Error creating pool '{pool_name}': {e}")
            raise Exception(f"Failed to create pool: {str(e)}")
    
    def get_user_pools(self, username: str) -> List[Dict]:
        """Get all pools owned by a specific user"""
        user_pools = []
        
        for pool_name, pool_config in self.pools.items():
            if self.pool_owners.get(pool_name) == username:
                try:
                    status = self._get_pool_status(pool_name)
                    status_dict = status.to_dict()
                    # Add masked config info to status
                    status_dict['config'] = pool_config.to_dict(mask_sensitive=True)
                    status_dict['owner'] = username
                    user_pools.append(status_dict)
                except Exception as e:
                    logger.error(f"Error getting status for pool '{pool_name}': {e}")
                    user_pools.append({
                        'pool_name': pool_name,
                        'error': str(e),
                        'minimum_vms': pool_config.minimum_vms,
                        'config': pool_config.to_dict(mask_sensitive=True),
                        'owner': username
                    })
        
        return user_pools
    
    def check_pool_ownership(self, pool_name: str, username: str) -> bool:
        """Check if a user owns a specific pool"""
        return self.pool_owners.get(pool_name) == username
    
    def update_pool(self, pool_name: str, update_data: Dict, requesting_user: str = None) -> Dict:
        """Update pool configuration"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        # Check ownership
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")

        try:
            must_update = False
            pool_config = self.pools[pool_name]

            if 'devcontainer_json' in update_data:
                pool_config.devcontainer_json = update_data['devcontainer_json']
                must_update = True

            if 'dockerfile' in update_data:
                pool_config.dockerfile = update_data['dockerfile']
                must_update = True

            if 'docker_compose_yml' in update_data:
                pool_config.docker_compose_yml = update_data['docker_compose_yml']
                must_update = True

            if 'pm2_config_js' in update_data:
                pool_config.pm2_config_js = update_data['pm2_config_js']
                must_update = True

            if 'poolCpu' in update_data:
                pool_config.cpu = update_data['poolCpu']
                must_update = True

            if 'poolMemory' in update_data:
                pool_config.memory = update_data['poolMemory']
                must_update = True
            
            # Update allowed fields
            if 'branch_name' in update_data:
                pool_config.branch_name = update_data['branch_name']
                must_update = True
            
            # Update allowed fields
            if 'minimum_vms' in update_data:
                if not isinstance(update_data['minimum_vms'], int) or update_data['minimum_vms'] < 1:
                    raise ValueError("minimum_vms must be a positive integer")
                pool_config.minimum_vms = update_data['minimum_vms']
            
            if 'github_username' in update_data:
                pool_config.github_username = update_data['github_username']
                must_update = True

            if 'github_pat' in update_data:
                pat_data = update_data['github_pat']
                
                if isinstance(pat_data, dict):
                    # New format with masking support
                    pat_value = pat_data.get('value', '')
                    is_masked = pat_data.get('masked', False)
                    
                    if is_masked and pool_config.github_pat:
                        # Check if the masked value matches what we would generate
                        expected_masked = pool_config._mask_value(pool_config.github_pat)
                        
                        if pat_value == expected_masked:
                            # Value unchanged, keep existing
                            pass  # Don't update github_pat
                        else:
                            must_update = True
                            # Value was modified, use new value
                            pool_config.github_pat = pat_value
                    else:
                        must_update = True
                        # New PAT or unmasked value
                        pool_config.github_pat = pat_value
                elif isinstance(pat_data, str):
                    must_update = True
                    # Legacy string format
                    pool_config.github_pat = pat_data
            
            # Handle environment variables with masking support
            if 'env_vars' in update_data:
                must_update = True
                new_env_vars = []
                existing_env_vars = {env['name']: env['value'] for env in pool_config.env_vars}
                
                for env_var in update_data['env_vars']:
                    if not isinstance(env_var, dict) or 'name' not in env_var:
                        continue
                        
                    env_name = env_var['name']
                    env_value = env_var.get('value', '')
                    is_masked = env_var.get('masked', False)
                    
                    # If the value is marked as masked and unchanged, keep the existing value
                    if is_masked and env_name in existing_env_vars:
                        # Check if the masked value matches what we would generate
                        existing_value = existing_env_vars[env_name]
                        expected_masked = pool_config._mask_env_value(existing_value)
                        
                        if env_value == expected_masked:
                            # Value unchanged, keep existing
                            new_env_vars.append({
                                'name': env_name,
                                'value': existing_value
                            })
                        else:
                            # Value was modified, use new value
                            new_env_vars.append({
                                'name': env_name,
                                'value': env_value
                            })
                    else:
                        # New variable or unmasked value
                        new_env_vars.append({
                            'name': env_name,
                            'value': env_value
                        })
                
                pool_config.env_vars = new_env_vars
            
            # Update stored configuration
            owner_username = self.pool_owners.get(pool_name)
            self._store_pool_config(pool_config, owner_username)

            deleted_count = 0
            flagged_count = 0
            
            # Only delete/flag workspaces if configuration that affects workspace content changed
            if must_update:
                deleted_count, flagged_count = self._handle_workspace_recreation(pool_name)
            
            message_parts = [f"Pool '{pool_name}' updated successfully"]
            if must_update:
                if deleted_count > 0:
                    message_parts.append(f"{deleted_count} unused workspaces will be recreated")
                if flagged_count > 0:
                    message_parts.append(f"{flagged_count} used workspaces flagged for recreation when unused")
            else:
                message_parts.append("No workspace recreation needed")
            
            logger.info(f"Updated pool '{pool_name}' configuration. Config changed: {must_update}, Deleted: {deleted_count}, Flagged: {flagged_count}")
            
            return {
                "success": True,
                "message": ". ".join(message_parts),
                "pool": pool_config.to_dict(mask_sensitive=True),
                "owner": owner_username,
                "config_changed": must_update,
                "deleted_workspaces": deleted_count,
                "flagged_workspaces": flagged_count
            }
            
            
        except Exception as e:
            logger.error(f"Error updating pool '{pool_name}': {e}")
            raise Exception(f"Failed to update pool: {str(e)}")
        
    def _handle_workspace_recreation(self, pool_name: str) -> tuple[int, int]:
        """Handle workspace recreation after pool config change"""
        deleted_count = 0
        flagged_count = 0
        
        try:
            workspaces = self._get_pool_workspaces(pool_name)
            
            for workspace in workspaces:
                usage_status = workspace.get('usage_status')
                workspace_id = workspace.get('id')
                
                if not workspace_id:
                    continue
                    
                if usage_status == 'unused':
                    # Delete unused workspaces immediately
                    try:
                        logger.info(f"Deleting unused workspace {workspace_id} from pool {pool_name} for recreation")
                        workspace_service.delete_workspace(workspace_id)
                        deleted_count += 1
                    except Exception as e:
                        logger.error(f"Failed to delete unused workspace {workspace_id}: {e}")
                elif usage_status == 'used':
                    # Flag used workspaces for deletion when they become unused
                    try:
                        namespace_name = f"workspace-{workspace_id}"
                        self._flag_workspace_for_recreation(namespace_name)
                        flagged_count += 1
                        logger.info(f"Flagged used workspace {workspace_id} from pool {pool_name} for recreation when unused")
                    except Exception as e:
                        logger.error(f"Failed to flag workspace {workspace_id} for recreation: {e}")
        
        except Exception as e:
            logger.error(f"Error handling workspace recreation in pool {pool_name}: {e}")
        
        return deleted_count, flagged_count

    def _flag_workspace_for_recreation(self, namespace_name: str):
        """Flag a workspace for recreation when it becomes unused"""
        try:
            flag_data = {
                'flagged_for_recreation': True,
                'flagged_at': datetime.now().isoformat(),
                'reason': 'pool_config_changed'
            }
            
            config_map = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name="workspace-recreation-flag",
                    namespace=namespace_name,
                    labels={"app": "workspace-recreation-flag"}
                ),
                data={
                    "flag.json": json.dumps(flag_data)
                }
            )
            
            try:
                # Try to update first
                self.core_v1.patch_namespaced_config_map(
                    name="workspace-recreation-flag",
                    namespace=namespace_name,
                    body=config_map
                )
            except client.rest.ApiException as e:
                if e.status == 404:
                    # Create if it doesn't exist
                    self.core_v1.create_namespaced_config_map(
                        namespace=namespace_name,
                        body=config_map
                    )
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Error flagging workspace for recreation: {e}")
            raise

    def _is_workspace_flagged_for_recreation(self, namespace_name: str) -> bool:
        """Check if a workspace is flagged for recreation"""
        try:
            config_map = self.core_v1.read_namespaced_config_map(
                name="workspace-recreation-flag",
                namespace=namespace_name
            )
            
            flag_data = json.loads(config_map.data.get("flag.json", "{}"))
            return flag_data.get('flagged_for_recreation', False)
            
        except client.rest.ApiException as e:
            if e.status == 404:
                # No flag ConfigMap means not flagged
                return False
            raise
        except Exception as e:
            logger.error(f"Error checking workspace recreation flag: {e}")
            return False

    def _remove_recreation_flag(self, namespace_name: str):
        """Remove the recreation flag from a workspace"""
        try:
            self.core_v1.delete_namespaced_config_map(
                name="workspace-recreation-flag",
                namespace=namespace_name
            )
        except client.rest.ApiException as e:
            if e.status != 404:  # Ignore if already deleted
                raise
        except Exception as e:
            logger.error(f"Error removing recreation flag: {e}")

        
    def _delete_unused_workspaces(self, pool_name: str) -> int:
        """Delete all unused workspaces in a pool so they get recreated"""
        deleted_count = 0
        
        try:
            workspaces = self._get_pool_workspaces(pool_name)
            
            for workspace in workspaces:
                usage_status = workspace.get('usage_status')
                workspace_id = workspace.get('id')
                
                # Only delete unused workspaces
                if usage_status == 'unused' and workspace_id:
                    try:
                        logger.info(f"Deleting unused workspace {workspace_id} from pool {pool_name} for recreation")
                        workspace_service.delete_workspace(workspace_id)
                        deleted_count += 1
                    except Exception as e:
                        logger.error(f"Failed to delete unused workspace {workspace_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error deleting unused workspaces in pool {pool_name}: {e}")
        
        return deleted_count

    
    def list_pools(self) -> List[Dict]:
        """List all pools with their status"""
        pools_status = []
        
        for pool_name, pool_config in self.pools.items():
            try:
                status = self._get_pool_status(pool_name)
                status_dict = status.to_dict()
                # Add masked config info to status
                status_dict['config'] = pool_config.to_dict(mask_sensitive=True)
                status_dict['owner'] = self.pool_owners.get(pool_name, 'admin')

                pools_status.append(status_dict)
            except Exception as e:
                logger.error(f"Error getting status for pool '{pool_name}': {e}")
                pools_status.append({
                    'pool_name': pool_name,
                    'error': str(e),
                    'minimum_vms': pool_config.minimum_vms,
                    'config': pool_config.to_dict(mask_sensitive=True)
                })
        
        return pools_status
    
    def get_pool(self, pool_name: str, requesting_user: str = None) -> Dict:
        """Get detailed information about a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")

        try:
            pool_config = self.pools[pool_name]
            status = self._get_pool_status(pool_name)
            owner_username = self.pool_owners.get(pool_name)
            
            return {
                "config": pool_config.to_dict(mask_sensitive=True),
                "status": status.to_dict(),
                "owner": owner_username
            }
        except Exception as e:
            logger.error(f"Error getting pool '{pool_name}': {e}")
            raise Exception(f"Failed to get pool: {str(e)}")
    
    def delete_pool(self, pool_name: str, requesting_user: str = None) -> Dict:
        """Delete a pool and all its workspaces"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        # Check ownership
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")
        
        try:
            owner_username = self.pool_owners.get(pool_name)

            # Stop monitoring
            self._stop_pool_monitoring(pool_name)
            
            # Delete all workspaces in the pool
            workspaces = self._get_pool_workspaces(pool_name)
            for workspace in workspaces:
                try:
                    workspace_service.delete_workspace(workspace['id'])
                    logger.info(f"Deleted workspace {workspace['id']} from pool {pool_name}")
                except Exception as e:
                    logger.error(f"Error deleting workspace {workspace['id']}: {e}")
            
            # Remove pool configuration from Kubernetes
            self._delete_pool_config(pool_name)
            
            # Remove from local cache
            del self.pools[pool_name]
            if pool_name in self.pool_owners:
                del self.pool_owners[pool_name]

            # Remove scaling lock
            if pool_name in self.scaling_locks:
                del self.scaling_locks[pool_name]

            if owner_username:
                user_service.remove_pool_from_user(owner_username, pool_name)

            logger.info(f"Deleted pool '{pool_name}'")
            
            return {
                "success": True,
                "message": f"Pool '{pool_name}' deleted successfully",
                "owner": owner_username
            }
            
        except Exception as e:
            logger.error(f"Error deleting pool '{pool_name}': {e}")
            raise Exception(f"Failed to delete pool: {str(e)}")
    
    def scale_pool(self, pool_name: str, new_minimum: int, requesting_user: str = None) -> Dict:
        """Update the minimum VMs for a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        # Check ownership
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")


        if new_minimum < 1:
            raise ValueError("minimum_vms must be at least 1")
        
        try:
            # Use lock to prevent concurrent scaling
            with self.scaling_locks.get(pool_name, threading.Lock()):
                # Update pool configuration
                pool_config = self.pools[pool_name]
                old_minimum = pool_config.minimum_vms
                pool_config.minimum_vms = new_minimum
                
                # Update stored configuration
                owner_username = self.pool_owners.get(pool_name)
                self._store_pool_config(pool_config, owner_username)
                
                # Trigger scaling
                self._scale_pool(pool_name)
                
                logger.info(f"Scaled pool '{pool_name}' from {old_minimum} to {new_minimum} minimum VMs")
                
                return {
                    "success": True,
                    "message": f"Pool '{pool_name}' scaled to {new_minimum} minimum VMs",
                    "old_minimum": old_minimum,
                    "new_minimum": new_minimum,
                    "owner": owner_username
                }
            
        except Exception as e:
            logger.error(f"Error scaling pool '{pool_name}': {e}")
            raise Exception(f"Failed to scale pool: {str(e)}")
    
    def get_available_workspace(self, pool_name: str, requesting_user: str = None) -> Optional[Dict]:
        """Get an available workspace from the pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        # Check ownership
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")
        
        try:
            workspaces = self._get_pool_workspaces(pool_name)
            
            # Find a running, healthy, and unused workspace
            for workspace in workspaces:
                state = workspace.get('state')
                usage_status = workspace.get('usage_status')
                
                # Only consider truly healthy workspaces
                if (state == 'running' and 
                    usage_status == 'unused' and
                    state not in ['crashing', 'unstable', 'failed']):
                    
                    # Optional: Perform additional health check
                    workspace_id = workspace.get('id')
                    if workspace_id:
                        namespace_name = f"workspace-{workspace_id}"
                        
                        # Get the actual pod for health check
                        pods = self.core_v1.list_namespaced_pod(
                            namespace_name,
                            label_selector="app=code-server"
                        )
                        
                        if pods.items and self._is_workspace_healthy(namespace_name, pods.items[0]):
                            return workspace
            
            # No healthy available workspace
            return None
            
        except Exception as e:
            logger.error(f"Error getting available workspace from pool '{pool_name}': {e}")
            return None

    
    def mark_workspace_as_used(self, pool_name: str, workspace_id: str, requesting_user: str = None, user_info: Optional[str] = None) -> Dict:
        """Mark a workspace as used"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        # Check ownership
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")
        
        try:
            # Find the workspace namespace
            namespace_name = f"workspace-{workspace_id}"
            
            # Verify the workspace belongs to this pool
            try:
                namespace = self.core_v1.read_namespace(namespace_name)
                pool_label = sanitize_k8s_label(pool_name)
                if namespace.metadata.labels.get('pool') != pool_label:
                    raise ValueError(f"Workspace '{workspace_id}' does not belong to pool '{pool_name}'")
            except client.rest.ApiException as e:
                if e.status == 404:
                    raise ValueError(f"Workspace '{workspace_id}' not found")
                raise
            
            # Update the workspace usage status
            self._update_workspace_usage_status(namespace_name, 'used', user_info)
            
            logger.info(f"Marked workspace '{workspace_id}' as used in pool '{pool_name}'")

            workspace_info = workspace_service.get_workspace(workspace_id)
            
            return {
                "success": True,
                "message": f"Workspace '{workspace_id}' marked as used",
                "workspace_id": workspace_id,
                "pool_name": pool_name,
                "pool_owner": self.pool_owners.get(pool_name),
                "usage_status": "used",
                "user_info": user_info,
                "url": workspace_info['url']
            }
            
        except Exception as e:
            logger.error(f"Error marking workspace '{workspace_id}' as used: {e}")
            raise Exception(f"Failed to mark workspace as used: {str(e)}")
    
    def mark_workspace_as_unused(self, pool_name: str, workspace_id: str, requesting_user: str = None) -> Dict:
        """Mark a workspace as unused"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")

        try:
            # Find the workspace namespace
            namespace_name = f"workspace-{workspace_id}"
            
            # Verify the workspace belongs to this pool
            try:
                namespace = self.core_v1.read_namespace(namespace_name)
                pool_label = sanitize_k8s_label(pool_name)
                if namespace.metadata.labels.get('pool') != pool_label:
                    raise ValueError(f"Workspace '{workspace_id}' does not belong to pool '{pool_name}'")
            except client.rest.ApiException as e:
                if e.status == 404:
                    raise ValueError(f"Workspace '{workspace_id}' not found")
                raise
            
            # Update the workspace usage status
            flagged_for_recreation = self._is_workspace_flagged_for_recreation(namespace_name)
            
            if flagged_for_recreation:
                # Delete the workspace instead of just marking it unused
                logger.info(f"Deleting workspace '{workspace_id}' as it was flagged for recreation due to pool config change")
                
                try:
                    workspace_service.delete_workspace(workspace_id)
                    logger.info(f"Successfully deleted flagged workspace '{workspace_id}' from pool '{pool_name}'")
                    
                    return {
                        "success": True,
                        "message": f"Workspace '{workspace_id}' deleted for recreation due to pool configuration changes",
                        "workspace_id": workspace_id,
                        "pool_name": pool_name,
                        "pool_owner": self.pool_owners.get(pool_name),
                        "action": "deleted_for_recreation"
                    }
                except Exception as e:
                    logger.error(f"Failed to delete flagged workspace '{workspace_id}': {e}")
                    # Fall back to marking as unused if deletion fails
                    flagged_for_recreation = False
            
            if not flagged_for_recreation:
                # Update the workspace usage status normally
                self._update_workspace_usage_status(namespace_name, 'unused')
                
                logger.info(f"Marked workspace '{workspace_id}' as unused in pool '{pool_name}'")
                
                return {
                    "success": True,
                    "message": f"Workspace '{workspace_id}' marked as unused",
                    "workspace_id": workspace_id,
                    "pool_name": pool_name,
                    "pool_owner": self.pool_owners.get(pool_name),
                    "usage_status": "unused",
                    "action": "marked_unused"
                }
            
        except Exception as e:
            logger.error(f"Error marking workspace '{workspace_id}' as unused: {e}")
            raise Exception(f"Failed to mark workspace as unused: {str(e)}")
    
    def get_workspace_usage_status(self, pool_name: str, workspace_id: str, requesting_user: str = None) -> Dict:
        """Get the usage status of a workspace"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        # Check ownership
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")
        
        try:
            # Find the workspace namespace
            namespace_name = f"workspace-{workspace_id}"
            
            # Get the workspace usage status
            usage_info = self._get_workspace_usage_status(namespace_name)
            
            return {
                "success": True,
                "workspace_id": workspace_id,
                "pool_name": pool_name,
                "pool_owner": self.pool_owners.get(pool_name),
                "usage_status": usage_info.get('status', 'unused'),
                "user_info": usage_info.get('user_info'),
                "marked_at": usage_info.get('marked_at')
            }
            
        except Exception as e:
            logger.error(f"Error getting workspace usage status: {e}")
            raise Exception(f"Failed to get workspace usage status: {str(e)}")
    
    def _update_workspace_usage_status(self, namespace_name: str, status: str, user_info: Optional[str] = None):
        """Update workspace usage status via ConfigMap"""
        try:
            usage_data = {
                'status': status,
                'marked_at': datetime.now().isoformat()
            }
            
            if user_info:
                usage_data['user_info'] = user_info
            
            config_map = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name="workspace-usage",
                    namespace=namespace_name,
                    labels={"app": "workspace-usage"}
                ),
                data={
                    "usage.json": json.dumps(usage_data)
                }
            )
            
            try:
                # Try to update first
                self.core_v1.patch_namespaced_config_map(
                    name="workspace-usage",
                    namespace=namespace_name,
                    body=config_map
                )
            except client.rest.ApiException as e:
                if e.status == 404:
                    # Create if it doesn't exist
                    self.core_v1.create_namespaced_config_map(
                        namespace=namespace_name,
                        body=config_map
                    )
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Error updating workspace usage status: {e}")
            raise
    
    def _get_workspace_usage_status(self, namespace_name: str) -> Dict:
        """Get workspace usage status from ConfigMap"""
        try:
            config_map = self.core_v1.read_namespaced_config_map(
                name="workspace-usage",
                namespace=namespace_name
            )
            
            usage_data = json.loads(config_map.data.get("usage.json", "{}"))
            return usage_data
            
        except client.rest.ApiException as e:
            if e.status == 404:
                # No usage status ConfigMap means unused
                return {'status': 'unused'}
            raise
        except Exception as e:
            logger.error(f"Error getting workspace usage status: {e}")
            return {'status': 'unused'}
    
    def _load_existing_pools(self):
        """Load existing pools from Kubernetes"""
        try:
            # Get all pool ConfigMaps
            config_maps = self.core_v1.list_namespaced_config_map(
                namespace="workspace-system",
                label_selector="app=workspace-pool"
            )
            
            for cm in config_maps.items:
                try:
                    pool_data = json.loads(cm.data.get("pool.json", "{}"))
                    if 'github_username' not in pool_data:
                        pool_data['github_username'] = None
                    if 'env_vars' not in pool_data:
                        pool_data['env_vars'] = []
                    if 'devcontainer_json' not in pool_data:
                        pool_data['devcontainer_json'] = None
                    if 'dockerfile' not in pool_data:
                        pool_data['dockerfile'] = None
                    if 'docker_compose_yml' not in pool_data:
                        pool_data['docker_compose_yml'] = None
                    if 'pm2_config_js' not in pool_data:
                        pool_data['pm2_config_js'] = None
                    if 'cpu' not in pool_data:
                        pool_data['cpu'] = "2"
                    if 'memory' not in pool_data:
                        pool_data['memory'] = "8Gi"

                    pool_config = PoolConfig(
                        pool_name=pool_data['pool_name'],
                        minimum_vms=pool_data['minimum_vms'],
                        repo_name=pool_data['repo_name'],
                        branch_name=pool_data['branch_name'],
                        github_pat=pool_data['github_pat'],
                        github_username=pool_data['github_username'],
                        env_vars=pool_data['env_vars'],
                        devcontainer_json=pool_data['devcontainer_json'],
                        dockerfile=pool_data['dockerfile'],
                        docker_compose_yml=pool_data['docker_compose_yml'],
                        pm2_config_js=pool_data['pm2_config_js'],
                        cpu=pool_data['cpu'],
                        memory=pool_data['memory'],
                        created_at=datetime.fromisoformat(pool_data['created_at'])
                    )

                    owner_username = cm.metadata.labels.get('owner', 'unknown')
                    
                    self.pools[pool_config.pool_name] = pool_config
                    self.pool_owners[pool_config.pool_name] = owner_username
                    self.scaling_locks[pool_config.pool_name] = threading.Lock()
                    self._start_pool_monitoring(pool_config.pool_name)
                    
                    logger.info(f"Loaded existing pool: {pool_config.pool_name} (owner: {owner_username})")
                    
                except Exception as e:
                    logger.error(f"Error loading pool from ConfigMap {cm.metadata.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Error loading existing pools: {e}")
    
    def _store_pool_config(self, pool_config: PoolConfig, owner_username: str = None):
        """Store pool configuration in Kubernetes"""
        config_data = {
            'pool_name': pool_config.pool_name,
            'minimum_vms': pool_config.minimum_vms,
            'repo_name': pool_config.repo_name,
            'branch_name': pool_config.branch_name,
            'github_pat': pool_config.github_pat,  # In production, this should be stored in a Secret
            'github_username': pool_config.github_username,
            'env_vars': pool_config.env_vars,
            'devcontainer_json': pool_config.devcontainer_json,
            'dockerfile': pool_config.dockerfile,
            'docker_compose_yml': pool_config.docker_compose_yml,
            'pm2_config_js': pool_config.pm2_config_js,
            'cpu': pool_config.cpu,
            'memory': pool_config.memory,
            'created_at': pool_config.created_at.isoformat()
        }
        
        # Sanitize the pool name for Kubernetes resource naming
        sanitized_pool_name = sanitize_k8s_name(pool_config.pool_name)
        sanitized_pool_label = sanitize_k8s_label(pool_config.pool_name)
        
        labels = {
            "app": "workspace-pool", 
            "pool": sanitized_pool_label,
            "original-pool-name": sanitized_pool_label  # Keep track of original name
        }

        if owner_username:
            labels["owner"] = sanitize_k8s_label(owner_username)

        config_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"pool-{sanitized_pool_name}",
                namespace="workspace-system",
                labels=labels
            ),
            data={
                "pool.json": json.dumps(config_data)
            }
        )
        
        try:
            # Try to update first
            self.core_v1.patch_namespaced_config_map(
                name=f"pool-{sanitized_pool_name}",
                namespace="workspace-system",
                body=config_map
            )
        except client.rest.ApiException as e:
            if e.status == 404:
                # Create if it doesn't exist
                self.core_v1.create_namespaced_config_map(
                    namespace="workspace-system",
                    body=config_map
                )
            else:
                raise
    
    def _delete_pool_config(self, pool_name: str):
        """Delete pool configuration from Kubernetes"""
        try:
            sanitized_pool_name = sanitize_k8s_name(pool_name)
            self.core_v1.delete_namespaced_config_map(
                name=f"pool-{sanitized_pool_name}",
                namespace="workspace-system"
            )
        except client.rest.ApiException as e:
            if e.status != 404:  # Ignore if already deleted
                raise
    
    def _get_pool_workspaces(self, pool_name: str) -> List[Dict]:
        """Get all workspaces belonging to a pool"""
        try:
            # Use sanitized pool name for label selector
            sanitized_pool_label = sanitize_k8s_label(pool_name)
            
            # Get all namespaces with the pool label
            namespaces = self.core_v1.list_namespace(
                label_selector=f"app=workspace,pool={sanitized_pool_label}"
            )
            
            workspaces = []
            for ns in namespaces.items:
                try:
                    # Get workspace info
                    config_maps = self.core_v1.list_namespaced_config_map(
                        ns.metadata.name,
                        label_selector="app=workspace-info"
                    )
                    
                    if config_maps.items:
                        workspace_info = json.loads(config_maps.items[0].data.get("info", "{}"))
                        
                        # Get pod status with crash detection
                        pods = self.core_v1.list_namespaced_pod(
                            ns.metadata.name,
                            label_selector="app=code-server"
                        )
                        
                        if pods.items:
                            pod = pods.items[0]
                            workspace_info["state"] = self._determine_pod_state(pod)
                        else:
                            workspace_info["state"] = "creating"
                        
                        # Get usage status
                        usage_info = self._get_workspace_usage_status(ns.metadata.name)
                        workspace_info["usage_status"] = usage_info.get('status', 'unused')
                        workspace_info["user_info"] = usage_info.get('user_info')
                        workspace_info["marked_at"] = usage_info.get('marked_at')
                        
                        # Check if flagged for recreation
                        workspace_info["flagged_for_recreation"] = self._is_workspace_flagged_for_recreation(ns.metadata.name)
                        
                        workspaces.append(workspace_info)
                        
                except Exception as e:
                    logger.error(f"Error getting workspace info from namespace {ns.metadata.name}: {e}")
                    continue
            
            return workspaces
            
        except Exception as e:
            logger.error(f"Error getting workspaces for pool '{pool_name}': {e}")
            return []

    def _determine_pod_state(self, pod) -> str:
        """Determine the actual state of a pod, including crash detection and health checks"""
        
        # Check if pod is explicitly failed
        if pod.status.phase in ["Failed", "Unknown"]:
            return "failed"
        
        # Check for crash loop or repeated failures
        if pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                # Check restart count - high restart count indicates crashing
                if container_status.restart_count >= 3:  # Configurable threshold
                    logger.warning(f"Pod {pod.metadata.name} has high restart count: {container_status.restart_count}")
                    return "crashing"
                
                # Check current container state
                if container_status.state:
                    # Container is waiting due to crash loop back off
                    if (container_status.state.waiting and 
                        container_status.state.waiting.reason in ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"]):
                        return "crashing"
                    
                    # Container terminated due to error
                    if (container_status.state.terminated and 
                        container_status.state.terminated.exit_code != 0):
                        return "failed"
                
                # Check last termination state for crash patterns
                if container_status.last_state and container_status.last_state.terminated:
                    last_terminated = container_status.last_state.terminated
                    if last_terminated.exit_code != 0:
                        # Recent crash - check how recent
                        if last_terminated.finished_at:
                            from datetime import datetime, timezone
                            import dateutil.parser
                            
                            finish_time = dateutil.parser.parse(last_terminated.finished_at)
                            now = datetime.now(timezone.utc)
                            time_since_crash = (now - finish_time).total_seconds()
                            
                            # If crashed within last 5 minutes, consider it unstable
                            if time_since_crash < 300:  # 5 minutes
                                logger.warning(f"Pod {pod.metadata.name} crashed recently: {last_terminated.reason}")
                                return "unstable"
        
        # Standard phase checking
        if pod.status.phase == "Running":
            # Additional check: ensure containers are actually ready
            if pod.status.container_statuses:
                all_ready = all(cs.ready for cs in pod.status.container_statuses)
                if not all_ready:
                    return "starting"
            
            # Perform HTTP health check before considering it truly "running"
            namespace_name = pod.metadata.namespace
            if not self._check_http_health(namespace_name, pod):
                logger.info(f"Pod {pod.metadata.name} in {namespace_name} is running but failed health check")
                return "starting"  # Pod is running but services aren't ready yet
            
            return "running"
        elif pod.status.phase == "Pending":
            return "pending"
        else:
            return pod.status.phase.lower()
        
    def _is_workspace_healthy(self, namespace_name: str, pod) -> bool:
        """Perform comprehensive health check on a workspace"""
        
        # Basic pod health
        if pod.status.phase != "Running":
            return False
        
        # Container health
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                # Check if containers are ready
                if not cs.ready:
                    return False
                
                # Check restart count threshold
                if cs.restart_count >= 3:
                    logger.warning(f"Workspace {namespace_name} has high restart count: {cs.restart_count}")
                    return False
                
                # Check for recent crashes
                if cs.last_state and cs.last_state.terminated:
                    last_terminated = cs.last_state.terminated
                    if last_terminated.exit_code != 0:
                        from datetime import datetime, timezone
                        import dateutil.parser
                        
                        if last_terminated.finished_at:
                            finish_time = dateutil.parser.parse(last_terminated.finished_at)
                            now = datetime.now(timezone.utc)
                            time_since_crash = (now - finish_time).total_seconds()
                            
                            # If crashed within last 10 minutes, not healthy
                            if time_since_crash < 600:
                                return False
        
        # Optional: HTTP health check if your code-server exposes health endpoint
        if not self._check_http_health(namespace_name, pod):
            logger.info(f"HTTP health check failed for {namespace_name}")
            return False
        
        logger.info(f"Workspace {namespace_name} passed all health checks")
        return True

    def _check_http_health(self, namespace_name: str, pod) -> bool:
        """Perform HTTP health check on the code-server by calling /jlist endpoint"""
        try:
            pod_ip = pod.status.pod_ip

            # Using cluster-internal DNS: service-name.namespace.svc.cluster.local
            health_url = f"http://{pod_ip}:15552/jlist"
            
            # Make the HTTP request with a reasonable timeout
            logger.info(f"Checking health for {namespace_name} at {health_url}")
            
            response = requests.get(
                health_url,
                timeout=10,  # 10 second timeout
                verify=False  # Skip SSL verification for internal cluster communication
            )
            
            if response.status_code != 200:
                logger.info(f"Health check failed for {namespace_name}: HTTP {response.status_code}")
                return False
            
            # Parse the JSON response
            try:
                processes = response.json()
            except ValueError as e:
                logger.error(f"Invalid JSON response from health check for {namespace_name}: {e}")
                return False
            
            # Check that we have a list of processes
            if not isinstance(processes, list):
                logger.error(f"Health check response is not a list for {namespace_name}")
                return False
            
            # Check that all processes are online
            online_processes = 0
            total_processes = len(processes)
            
            for process in processes:
                if not isinstance(process, dict):
                    continue
                    
                status = process.get('status', '')
                process_name = process.get('name', 'unknown')
                
                if status == 'online':
                    online_processes += 1
                    logger.info(f"Process {process_name} is online in {namespace_name}")
                else:
                    logger.info(f"Process {process_name} is {status} in {namespace_name}")
            
            # All processes must be online for the workspace to be healthy
            if online_processes == total_processes and total_processes > 0:
                logger.info(f"Health check passed for {namespace_name}: {online_processes}/{total_processes} processes online")
                return True
            else:
                logger.info(f"Health check failed for {namespace_name}: {online_processes}/{total_processes} processes online")
                return False
                
        except requests.exceptions.Timeout:
            logger.error(f"Health check timed out for {namespace_name}")
            return False
        except requests.exceptions.ConnectionError:
            logger.error(f"Health check connection failed for {namespace_name} (service may not be ready)")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Health check request failed for {namespace_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in health check for {namespace_name}: {e}")
            return False


    
    def _get_pool_status(self, pool_name: str) -> PoolStatus:
        """Get current status of a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        pool_config = self.pools[pool_name]
        workspaces = self._get_pool_workspaces(pool_name)
        
        # Count workspaces by state and usage
        running_vms = len([w for w in workspaces if w.get('state') == 'running'])
        pending_vms = len([w for w in workspaces if w.get('state') in ['pending', 'creating', 'starting']])
        failed_vms = len([w for w in workspaces if w.get('state') in ['failed', 'error', 'crashing']])
        used_vms = len([w for w in workspaces if w.get('usage_status') == 'used' and w.get('state') == 'running'])
        unused_vms = len([w for w in workspaces if w.get('usage_status') != 'used' and w.get('state') == 'running'])
        
        logger.debug(f"Pool {pool_name} status: total={len(workspaces)}, running={running_vms}, pending={pending_vms}, failed={failed_vms}, used={used_vms}, unused={unused_vms}, minimum={pool_config.minimum_vms}")
        
        return PoolStatus(
            pool_name=pool_name,
            minimum_vms=pool_config.minimum_vms,
            current_vms=len(workspaces),
            running_vms=running_vms,
            pending_vms=pending_vms,
            failed_vms=failed_vms,
            used_vms=used_vms,
            unused_vms=unused_vms,
            workspaces=workspaces
        )
    
    def _scale_pool(self, pool_name: str):
        """Scale a pool to meet minimum VM requirements"""
        try:
            status = self._get_pool_status(pool_name)
            
            # Calculate how many VMs we need to create
            # We only count running and pending VMs towards the minimum
            active_vms = status.running_vms + status.pending_vms
            needed_vms = max(0, status.minimum_vms - active_vms - status.failed_vms)
            
            logger.info(f"Pool '{pool_name}' scaling check: minimum={status.minimum_vms}, active={active_vms} (running={status.running_vms}, pending={status.pending_vms}), needed={needed_vms}")
            
            if needed_vms > 0:
                pool_config = self.pools[pool_name]
                
                logger.info(f"Pool '{pool_name}' needs {needed_vms} more VMs")
                
                # Create the needed workspaces
                created_count = 0
                for i in range(needed_vms):
                    try:
                        # Create workspace with pool-specific configuration
                        workspace_request = {
                            'githubUrls': [pool_config.repo_name],
                            'githubBranches': [pool_config.branch_name],
                            'githubToken': pool_config.github_pat,
                            'githubUsername': pool_config.github_username,
                            'image': 'linuxserver/code-server:latest',
                            'useDevContainer': True,
                            'env_vars': pool_config.env_vars,
                            'cpu': pool_config.cpu,
                            'memory': pool_config.memory,
                            'container_files': {
                                'devcontainer_json': pool_config.devcontainer_json,
                                'docker_compose_yml': pool_config.docker_compose_yml,
                                'dockerfile': pool_config.dockerfile,
                                'pm2_config_js': pool_config.pm2_config_js
                            }
                        }
                        
                        result = workspace_service.create_workspace(workspace_request)
                        
                        if result.get('success'):
                            # Label the namespace with pool information
                            workspace_id = result['workspace']['id']
                            namespace_name = f"workspace-{workspace_id}"
                            
                            # Add pool label to namespace
                            self._label_namespace_with_pool(namespace_name, pool_name)
                            
                            # Mark as unused initially
                            self._update_workspace_usage_status(namespace_name, 'unused')
                            
                            created_count += 1
                            logger.info(f"Created workspace {workspace_id} for pool '{pool_name}' ({created_count}/{needed_vms})")
                        else:
                            logger.error(f"Failed to create workspace for pool '{pool_name}': {result}")
                            
                    except Exception as e:
                        logger.error(f"Error creating workspace for pool '{pool_name}': {e}")
                
                logger.info(f"Pool '{pool_name}' scaling completed: created {created_count}/{needed_vms} workspaces")
                        
            else:
                logger.debug(f"Pool '{pool_name}' does not need scaling")
                
        except Exception as e:
            logger.error(f"Error scaling pool '{pool_name}': {e}")
    
    def _label_namespace_with_pool(self, namespace_name: str, pool_name: str):
        """Add pool label to a workspace namespace"""
        try:
            # Sanitize the pool name for use as a label
            sanitized_pool_label = sanitize_k8s_label(pool_name)
            
            # Patch the namespace to add pool label
            self.core_v1.patch_namespace(
                name=namespace_name,
                body={
                    "metadata": {
                        "labels": {
                            "pool": sanitized_pool_label
                        }
                    }
                }
            )
            logger.debug(f"Labeled namespace {namespace_name} with pool {sanitized_pool_label}")
        except Exception as e:
            logger.error(f"Error labeling namespace {namespace_name} with pool {pool_name}: {e}")
    
    def _start_pool_monitoring(self, pool_name: str):
        """Start monitoring thread for a pool"""
        if pool_name in self.monitoring_threads:
            return  # Already monitoring
        
        stop_event = threading.Event()
        self.stop_monitoring[pool_name] = stop_event
        
        monitor_thread = threading.Thread(
            target=self._monitor_pool,
            args=(pool_name, stop_event),
            daemon=True,
            name=f"pool-monitor-{pool_name}"
        )
        
        self.monitoring_threads[pool_name] = monitor_thread
        monitor_thread.start()
        
        logger.info(f"Started monitoring thread for pool '{pool_name}'")
    
    def _stop_pool_monitoring(self, pool_name: str):
        """Stop monitoring thread for a pool"""
        if pool_name in self.stop_monitoring:
            self.stop_monitoring[pool_name].set()
            
        if pool_name in self.monitoring_threads:
            self.monitoring_threads[pool_name].join(timeout=5)
            del self.monitoring_threads[pool_name]
            
        if pool_name in self.stop_monitoring:
            del self.stop_monitoring[pool_name]
            
        logger.info(f"Stopped monitoring thread for pool '{pool_name}'")
    
    def _monitor_pool(self, pool_name: str, stop_event: threading.Event):
        """Monitor a pool and scale as needed"""
        logger.info(f"Started monitoring pool '{pool_name}'")
        
        # Wait a bit before first check to allow initial creation to complete
        if not stop_event.wait(10):
            while not stop_event.is_set():
                try:
                    if pool_name in self.pools:
                        scaling_lock = self.scaling_locks.get(pool_name)
                        if scaling_lock and scaling_lock.acquire(blocking=False):
                            try:
                                # Clean up unhealthy workspaces first
                                # self._cleanup_unhealthy_workspaces(pool_name)
                                
                                # Then scale the pool
                                self._scale_pool(pool_name)
                            finally:
                                scaling_lock.release()
                        else:
                            logger.debug(f"Pool '{pool_name}' scaling already in progress, skipping check")
                    else:
                        logger.warning(f"Pool '{pool_name}' no longer exists, stopping monitoring")
                        break
                        
                except Exception as e:
                    logger.error(f"Error in pool monitoring for '{pool_name}': {e}")
                
                stop_event.wait(60)
        
        logger.info(f"Stopped monitoring pool '{pool_name}'")

    def _cleanup_unhealthy_workspaces(self, pool_name: str):
        """Remove workspaces that are consistently unhealthy"""
        try:
            workspaces = self._get_pool_workspaces(pool_name)
            
            for workspace in workspaces:
                state = workspace.get('state')
                usage_status = workspace.get('usage_status')
                workspace_id = workspace.get('id')
                
                # Only clean up unused workspaces that are in bad states
                if (usage_status == 'unused' and 
                    state in ['crashing', 'failed'] and 
                    workspace_id):
                    
                    logger.warning(f"Cleaning up unhealthy workspace {workspace_id} in pool {pool_name} (state: {state})")
                    
                    try:
                        workspace_service.delete_workspace(workspace_id)
                        logger.info(f"Deleted unhealthy workspace {workspace_id}")
                    except Exception as e:
                        logger.error(f"Failed to delete unhealthy workspace {workspace_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error cleaning up unhealthy workspaces in pool {pool_name}: {e}")

    def get_pool_workspaces(self, pool_name: str, requesting_user: str = None) -> Dict:
        """Get all workspaces in a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")

        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")

        try:
            workspaces = self._get_pool_workspaces(pool_name)
            
            return {
                "success": True,
                "pool_name": pool_name,
                "pool_owner": self.pool_owners.get(pool_name),
                "workspaces": workspaces,
                "total_count": len(workspaces)
            }
            
        except Exception as e:
            logger.error(f"Error getting workspaces for pool '{pool_name}': {e}")
            raise Exception(f"Failed to get pool workspaces: {str(e)}")

    def delete_workspace_from_pool(self, pool_name: str, workspace_id: str, requesting_user: str = None) -> Dict:
        """Delete a specific workspace from a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        if requesting_user and not self.check_pool_ownership(pool_name, requesting_user):
            raise ValueError(f"Access denied: User '{requesting_user}' does not own pool '{pool_name}'")

        try:
            # Find the workspace namespace
            namespace_name = f"workspace-{workspace_id}"
            
            # Verify the workspace belongs to this pool
            try:
                namespace = self.core_v1.read_namespace(namespace_name)
                pool_label = sanitize_k8s_label(pool_name)
                if namespace.metadata.labels.get('pool') != pool_label:
                    raise ValueError(f"Workspace '{workspace_id}' does not belong to pool '{pool_name}'")
            except client.rest.ApiException as e:
                if e.status == 404:
                    raise ValueError(f"Workspace '{workspace_id}' not found")
                raise
            
            # Delete the workspace using the workspace service
            result = workspace_service.delete_workspace(workspace_id)
            
            if not result.get('success', False):
                raise Exception(f"Failed to delete workspace: {result.get('error', 'Unknown error')}")
            
            logger.info(f"Deleted workspace '{workspace_id}' from pool '{pool_name}'")
            
            # Trigger pool scaling to maintain minimum VMs (if needed)
            # This will be done asynchronously by the monitoring thread, but we can also trigger it immediately
            try:
                scaling_lock = self.scaling_locks.get(pool_name)
                if scaling_lock and scaling_lock.acquire(blocking=False):
                    try:
                        # Schedule scaling to happen soon (after a short delay to allow cleanup)
                        threading.Timer(5.0, self._scale_pool, args=[pool_name]).start()
                    finally:
                        scaling_lock.release()
            except Exception as e:
                logger.warning(f"Could not trigger immediate scaling for pool '{pool_name}': {e}")
            
            return {
                "success": True,
                "message": f"Workspace '{workspace_id}' deleted from pool '{pool_name}'",
                "workspace_id": workspace_id,
                "pool_name": pool_name,
                "pool_owner": self.pool_owners.get(pool_name)
            }
            
        except ValueError as ve:
            # Re-raise validation errors as-is
            raise ve
        except Exception as e:
            logger.error(f"Error deleting workspace '{workspace_id}' from pool '{pool_name}': {e}")
            raise Exception(f"Failed to delete workspace from pool: {str(e)}")


# Global service instance
pool_service = PoolService()