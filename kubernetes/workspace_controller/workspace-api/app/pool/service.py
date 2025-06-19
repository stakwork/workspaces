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

logger = logging.getLogger(__name__)


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
        self.monitoring_threads: Dict[str, threading.Thread] = {}
        self.stop_monitoring: Dict[str, threading.Event] = {}
        self.scaling_locks: Dict[str, threading.Lock] = {}
        self._load_existing_pools()
    
    def create_pool(self, pool_name: str, minimum_vms: int, repo_name: str, 
                   branch_name: str, github_pat: str) -> Dict:
        """Create a new pool"""
        try:
            # Validate inputs
            if pool_name in self.pools:
                raise ValueError(f"Pool '{pool_name}' already exists")
            
            if minimum_vms < 1:
                raise ValueError("minimum_vms must be at least 1")
            
            # Validate pool name for basic requirements
            if not pool_name or len(pool_name.strip()) == 0:
                raise ValueError("Pool name cannot be empty")
            
            if len(pool_name) > 253:
                raise ValueError("Pool name is too long (max 253 characters)")
            
            # Create pool configuration
            pool_config = PoolConfig(
                pool_name=pool_name,
                minimum_vms=minimum_vms,
                repo_name=repo_name,
                branch_name=branch_name,
                github_pat=github_pat
            )
            
            # Store pool configuration in Kubernetes
            self._store_pool_config(pool_config)
            
            # Add to local cache
            self.pools[pool_name] = pool_config
            
            # Create scaling lock for this pool
            self.scaling_locks[pool_name] = threading.Lock()
            
            # Start monitoring thread
            self._start_pool_monitoring(pool_name)
            
            # Create initial workspaces (with lock to prevent double creation)
            with self.scaling_locks[pool_name]:
                self._scale_pool(pool_name)
            
            logger.info(f"Created pool '{pool_name}' with {minimum_vms} minimum VMs")
            
            return {
                "success": True,
                "message": f"Pool '{pool_name}' created successfully",
                "pool": pool_config.to_dict()
            }
            
        except Exception as e:
            logger.error(f"Error creating pool '{pool_name}': {e}")
            raise Exception(f"Failed to create pool: {str(e)}")
    
    def list_pools(self) -> List[Dict]:
        """List all pools with their status"""
        pools_status = []
        
        for pool_name, pool_config in self.pools.items():
            try:
                status = self._get_pool_status(pool_name)
                pools_status.append(status.to_dict())
            except Exception as e:
                logger.error(f"Error getting status for pool '{pool_name}': {e}")
                pools_status.append({
                    'pool_name': pool_name,
                    'error': str(e),
                    'minimum_vms': pool_config.minimum_vms
                })
        
        return pools_status
    
    def get_pool(self, pool_name: str) -> Dict:
        """Get detailed information about a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        try:
            pool_config = self.pools[pool_name]
            status = self._get_pool_status(pool_name)
            
            return {
                "config": pool_config.to_dict(),
                "status": status.to_dict()
            }
        except Exception as e:
            logger.error(f"Error getting pool '{pool_name}': {e}")
            raise Exception(f"Failed to get pool: {str(e)}")
    
    def delete_pool(self, pool_name: str) -> Dict:
        """Delete a pool and all its workspaces"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        try:
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
            
            # Remove scaling lock
            if pool_name in self.scaling_locks:
                del self.scaling_locks[pool_name]
            
            logger.info(f"Deleted pool '{pool_name}'")
            
            return {
                "success": True,
                "message": f"Pool '{pool_name}' deleted successfully"
            }
            
        except Exception as e:
            logger.error(f"Error deleting pool '{pool_name}': {e}")
            raise Exception(f"Failed to delete pool: {str(e)}")
    
    def scale_pool(self, pool_name: str, new_minimum: int) -> Dict:
        """Update the minimum VMs for a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
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
                self._store_pool_config(pool_config)
                
                # Trigger scaling
                self._scale_pool(pool_name)
                
                logger.info(f"Scaled pool '{pool_name}' from {old_minimum} to {new_minimum} minimum VMs")
                
                return {
                    "success": True,
                    "message": f"Pool '{pool_name}' scaled to {new_minimum} minimum VMs",
                    "old_minimum": old_minimum,
                    "new_minimum": new_minimum
                }
            
        except Exception as e:
            logger.error(f"Error scaling pool '{pool_name}': {e}")
            raise Exception(f"Failed to scale pool: {str(e)}")
    
    def get_available_workspace(self, pool_name: str) -> Optional[Dict]:
        """Get an available workspace from the pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        try:
            workspaces = self._get_pool_workspaces(pool_name)
            
            # Find a running and unused workspace first
            for workspace in workspaces:
                if workspace.get('state') == 'running' and workspace.get('usage_status') == 'unused':
                    return workspace
            
            # No available workspace
            return None
            
        except Exception as e:
            logger.error(f"Error getting available workspace from pool '{pool_name}': {e}")
            return None
    
    def mark_workspace_as_used(self, pool_name: str, workspace_id: str, user_info: Optional[str] = None) -> Dict:
        """Mark a workspace as used"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
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
            
            return {
                "success": True,
                "message": f"Workspace '{workspace_id}' marked as used",
                "workspace_id": workspace_id,
                "pool_name": pool_name,
                "usage_status": "used",
                "user_info": user_info
            }
            
        except Exception as e:
            logger.error(f"Error marking workspace '{workspace_id}' as used: {e}")
            raise Exception(f"Failed to mark workspace as used: {str(e)}")
    
    def mark_workspace_as_unused(self, pool_name: str, workspace_id: str) -> Dict:
        """Mark a workspace as unused"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
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
            self._update_workspace_usage_status(namespace_name, 'unused')
            
            logger.info(f"Marked workspace '{workspace_id}' as unused in pool '{pool_name}'")
            
            return {
                "success": True,
                "message": f"Workspace '{workspace_id}' marked as unused",
                "workspace_id": workspace_id,
                "pool_name": pool_name,
                "usage_status": "unused"
            }
            
        except Exception as e:
            logger.error(f"Error marking workspace '{workspace_id}' as unused: {e}")
            raise Exception(f"Failed to mark workspace as unused: {str(e)}")
    
    def get_workspace_usage_status(self, pool_name: str, workspace_id: str) -> Dict:
        """Get the usage status of a workspace"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        try:
            # Find the workspace namespace
            namespace_name = f"workspace-{workspace_id}"
            
            # Get the workspace usage status
            usage_info = self._get_workspace_usage_status(namespace_name)
            
            return {
                "success": True,
                "workspace_id": workspace_id,
                "pool_name": pool_name,
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
                    pool_config = PoolConfig(
                        pool_name=pool_data['pool_name'],
                        minimum_vms=pool_data['minimum_vms'],
                        repo_name=pool_data['repo_name'],
                        branch_name=pool_data['branch_name'],
                        github_pat=pool_data['github_pat'],
                        created_at=datetime.fromisoformat(pool_data['created_at'])
                    )
                    
                    self.pools[pool_config.pool_name] = pool_config
                    self.scaling_locks[pool_config.pool_name] = threading.Lock()
                    self._start_pool_monitoring(pool_config.pool_name)
                    
                    logger.info(f"Loaded existing pool: {pool_config.pool_name}")
                    
                except Exception as e:
                    logger.error(f"Error loading pool from ConfigMap {cm.metadata.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Error loading existing pools: {e}")
    
    def _store_pool_config(self, pool_config: PoolConfig):
        """Store pool configuration in Kubernetes"""
        config_data = {
            'pool_name': pool_config.pool_name,
            'minimum_vms': pool_config.minimum_vms,
            'repo_name': pool_config.repo_name,
            'branch_name': pool_config.branch_name,
            'github_pat': pool_config.github_pat,  # In production, this should be stored in a Secret
            'created_at': pool_config.created_at.isoformat()
        }
        
        # Sanitize the pool name for Kubernetes resource naming
        sanitized_pool_name = sanitize_k8s_name(pool_config.pool_name)
        sanitized_pool_label = sanitize_k8s_label(pool_config.pool_name)
        
        config_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"pool-{sanitized_pool_name}",
                namespace="workspace-system",
                labels={
                    "app": "workspace-pool", 
                    "pool": sanitized_pool_label,
                    "original-pool-name": sanitized_pool_label  # Keep track of original name
                }
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
                        
                        # Get pod status
                        pods = self.core_v1.list_namespaced_pod(
                            ns.metadata.name,
                            label_selector="app=code-server"
                        )
                        
                        if pods.items:
                            pod = pods.items[0]
                            if pod.status.phase == "Running":
                                workspace_info["state"] = "running"
                            elif pod.status.phase in ["Pending"]:
                                workspace_info["state"] = "pending"
                            elif pod.status.phase in ["Failed", "Unknown"]:
                                workspace_info["state"] = "failed"
                            else:
                                workspace_info["state"] = pod.status.phase.lower()
                        else:
                            workspace_info["state"] = "creating"  # No pods yet, still being created
                        
                        # Get usage status
                        usage_info = self._get_workspace_usage_status(ns.metadata.name)
                        workspace_info["usage_status"] = usage_info.get('status', 'unused')
                        workspace_info["user_info"] = usage_info.get('user_info')
                        workspace_info["marked_at"] = usage_info.get('marked_at')
                        
                        workspaces.append(workspace_info)
                        
                except Exception as e:
                    logger.error(f"Error getting workspace info from namespace {ns.metadata.name}: {e}")
                    continue
            
            return workspaces
            
        except Exception as e:
            logger.error(f"Error getting workspaces for pool '{pool_name}': {e}")
            return []
    
    def _get_pool_status(self, pool_name: str) -> PoolStatus:
        """Get current status of a pool"""
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        pool_config = self.pools[pool_name]
        workspaces = self._get_pool_workspaces(pool_name)
        
        # Count workspaces by state and usage
        running_vms = len([w for w in workspaces if w.get('state') == 'running'])
        pending_vms = len([w for w in workspaces if w.get('state') in ['pending', 'creating']])
        failed_vms = len([w for w in workspaces if w.get('state') in ['failed', 'error']])
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
            needed_vms = max(0, status.minimum_vms - active_vms)
            
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
                            'image': 'linuxserver/code-server:latest',
                            'useDevContainer': True
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
        if not stop_event.wait(10):  # Wait 10 seconds before first check
            while not stop_event.is_set():
                try:
                    if pool_name in self.pools:
                        # Use lock to prevent concurrent scaling
                        scaling_lock = self.scaling_locks.get(pool_name)
                        if scaling_lock and scaling_lock.acquire(blocking=False):
                            try:
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
                
                # Wait for 60 seconds or until stop event
                stop_event.wait(60)  # Check every minute instead of every 30 seconds
        
        logger.info(f"Stopped monitoring pool '{pool_name}'")


# Global service instance
pool_service = PoolService()