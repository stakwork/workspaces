import logging
import uuid
import json
from threading import Thread
import time
from kubernetes import client
from typing import Optional
from datetime import datetime
import base64

from models.pool import Pool
from models.cleanup import CleanupStatus

logger = logging.getLogger(__name__)

class PoolService:
    """Service class for managing workspace pools"""
    
    def __init__(self, core_v1: client.CoreV1Api, apps_v1: client.AppsV1Api):
        self.pools = {}  # name -> Pool
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1
        self.running = True
        self.cleanup_status = CleanupStatus()
        self.monitor_thread = Thread(target=self._monitor_pools, daemon=True)
        self.monitor_thread.start()
    
    def create_pool(self, name: str, minimum_vms: int, repo_name: str, 
                   branch_name: str, github_pat: str) -> Optional[Pool]:
        """Create a new pool with the given configuration"""
        try:
            if name in self.pools:
                logger.error(f"Pool {name} already exists")
                return None
                
            pool = Pool(name, minimum_vms, repo_name, branch_name, github_pat)
            # Initialize pool attributes
            pool.workspace_ids = set()
            pool.workspace_count = 0
            pool.is_healthy = True
            pool.status_message = "Pool created"
            pool.last_check = datetime.now()
            
            self.pools[name] = pool
            logger.info(f"Created new pool: {name} with {minimum_vms} minimum VMs")
            
            # Create initial workspaces
            for _ in range(minimum_vms):
                workspace_id = str(uuid.uuid4())
                if self._create_workspace(workspace_id, repo_name, branch_name, github_pat, name):
                    pool.add_workspace(workspace_id)
                    logger.info(f"Created workspace {workspace_id} for pool {name}")
                else:
                    logger.error(f"Failed to create workspace for pool {name}")
            
            # Update pool status in ConfigMap
            self._update_pool_status(pool)
            return pool
            
        except Exception as e:
            logger.error(f"Error creating pool {name}: {e}")
            return None

    def delete_pool(self, name: str) -> bool:
        """Delete a pool and all its workspaces"""
        pool = self.pools.get(name)
        if not pool:
            logger.error(f"Pool {name} not found")
            return False
            
        try:
            self._cleanup_pool(pool)
            del self.pools[name]
            return True
        except Exception as e:
            logger.error(f"Error deleting pool {name}: {e}")
            return False
    
    def get_pool(self, name: str) -> Optional[Pool]:
        """Get a pool by name"""
        return self.pools.get(name)
    
    def list_pools(self):
        """List all pools"""
        try:
            pool_list = []
            for name, pool in self.pools.items():
                if not pool:
                    continue

                try:
                    pool_info = {
                        'name': name,
                        'minimum_vms': pool.minimum_vms,
                        'workspace_count': len(pool.workspace_ids),
                        'is_healthy': pool.is_healthy,
                        'status_message': pool.status_message,
                        'last_check': pool.last_check.isoformat() if pool.last_check else None,
                        'repo_name': pool.repo_name,
                        'branch_name': pool.branch_name
                    }
                    pool_list.append(pool_info)
                except Exception as e:
                    logger.error(f"Error processing pool {name}: {e}")

            return pool_list
        except Exception as e:
            logger.error(f"Error listing pools: {e}")
            return []

    def shutdown(self, timeout: int = 300):
        """Gracefully shutdown the pool service"""
        self.running = False
        if self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=min(60, timeout/5))
        
        # Cleanup remaining pools
        for pool in list(self.pools.values()):
            try:
                self._cleanup_pool(pool)
            except Exception as e:
                logger.error(f"Error cleaning up pool {pool.name} during shutdown: {e}")
    
    def _monitor_pools(self):
        """Monitor pools and maintain minimum VM counts"""
        while self.running:
            try:
                for pool_name, pool in list(self.pools.items()):
                    if not self.running:
                        break
                        
                    self._check_and_maintain_pool(pool)
                    
                if self.running:
                    time.sleep(60)
                    
            except Exception as e:
                logger.error(f"Error in pool monitoring: {e}")
                if self.running:
                    time.sleep(60)
        
        logger.info("Pool monitoring stopped")
    
    def _check_and_maintain_pool(self, pool: Pool):
        """Check pool health and maintain minimum VM count"""
        try:
            # Verify existing workspaces
            active_workspaces = set()
            for workspace_id in list(pool.workspace_ids):
                if self._check_workspace_exists(workspace_id):
                    active_workspaces.add(workspace_id)
                else:
                    pool.remove_workspace(workspace_id)
                    
            pool.workspace_ids = active_workspaces
            
            # Create new workspaces if needed
            current_count = len(pool.workspace_ids)
            if current_count < pool.minimum_vms:
                needed = pool.minimum_vms - current_count
                logger.info(f"Pool {pool.name} needs {needed} more workspaces")
                
                for _ in range(needed):
                    workspace_id = str(uuid.uuid4())
                    if self._create_workspace(workspace_id, pool.repo_name, pool.branch_name, pool.github_pat, pool.name):
                        pool.add_workspace(workspace_id)
                    else:
                        logger.error(f"Failed to create workspace for pool {pool.name}")
            
            pool.update_status(True, f"Pool has {len(pool.workspace_ids)} workspaces")
            
        except Exception as e:
            logger.error(f"Error maintaining pool {pool.name}: {e}")
            pool.update_status(False, f"Pool maintenance error: {str(e)}")
    
    def check_and_maintain_pools(self):
        """Check all pools and maintain minimum VMs"""
        try:
            # Change to access pool objects directly instead of using list_pools()
            for pool_name, pool in self.pools.items():
                try:
                    # Count active workspaces for this pool
                    workspace_count = self._count_pool_workspaces(pool_name)
                    
                    # Update pool status
                    pool.workspace_count = workspace_count
                    pool.last_check = datetime.now()
                    
                    # Check if we need to create more workspaces
                    if workspace_count < pool.minimum_vms:
                        needed_vms = pool.minimum_vms - workspace_count
                        logger.info(f"Pool {pool_name} needs {needed_vms} more VMs")
                        
                        # Create new workspaces
                        for _ in range(needed_vms):
                            try:
                                self.create_workspace_from_pool(pool)
                                pool.status_message = f"Creating new workspace to maintain minimum ({workspace_count}/{pool.minimum_vms})"
                            except Exception as e:
                                logger.error(f"Failed to create workspace for pool {pool.name}: {e}")
                                pool.status_message = f"Failed to create workspace: {str(e)}"
                                pool.is_healthy = False
                                continue
                    else:
                        pool.status_message = f"Pool is healthy ({workspace_count}/{pool.minimum_vms} VMs)"
                        pool.is_healthy = True
                        
                    self._update_pool_status(pool)
                    
                except Exception as e:
                    logger.error(f"Error checking pool {pool.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Error checking pools: {e}")

    def _count_pool_workspaces(self, pool_name):
        """Count number of active workspaces in a pool"""
        try:
            namespaces = self.core_v1.list_namespace(
                label_selector=f"pool={pool_name},app=workspace"
            )
            return len([ns for ns in namespaces.items if self._is_workspace_active(ns.metadata.name)])
        except Exception as e:
            logger.error(f"Error counting pool workspaces: {e}")
            return 0

    def _is_workspace_active(self, namespace):
        """Check if a workspace is active by checking its pods"""
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace,
                label_selector="app=code-server"
            )
            return any(pod.status.phase == "Running" for pod in pods.items)
        except Exception:
            return False

    def create_workspace_from_pool(self, pool):
        """Create a new workspace using pool configuration"""
        # Implementation will depend on your workspace creation logic
        # Use pool.repo_name, pool.branch_name, and pool.github_pat
        # Add label to link workspace to pool
        pass

    def _update_pool_status(self, pool):
        """Update pool status in ConfigMap"""
        try:
            status_data = {
                'workspace_count': getattr(pool, 'workspace_count', 0),
                'is_healthy': getattr(pool, 'is_healthy', False),
                'status_message': getattr(pool, 'status_message', ''),
                'last_check': datetime.now().isoformat()
            }
            
            try:
                cm = self.core_v1.read_namespaced_config_map(
                    name=f"pool-{pool.name}",
                    namespace="workspace-system"
                )
                cm.data['status'] = json.dumps(status_data)
                self.core_v1.replace_namespaced_config_map(
                    name=f"pool-{pool.name}",
                    namespace="workspace-system",
                    body=cm
                )
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    # Create ConfigMap if it doesn't exist
                    cm = client.V1ConfigMap(
                        metadata=client.V1ObjectMeta(
                            name=f"pool-{pool.name}",
                            namespace="workspace-system"
                        ),
                        data={'status': json.dumps(status_data)}
                    )
                    self.core_v1.create_namespaced_config_map(
                        namespace="workspace-system",
                        body=cm
                    )
                else:
                    raise
        except Exception as e:
            logger.error(f"Error updating pool status for {pool.name}: {e}")
    
    def _check_workspace_exists(self, workspace_id: str) -> bool:
        """Check if a workspace still exists in Kubernetes"""
        try:
            namespace = f"workspace-{workspace_id}"
            self.core_v1.read_namespace(namespace)
            return True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return False
            raise
    
    def _create_workspace(self, workspace_id: str, repo_name: str, branch_name: str, 
                         github_pat: str, pool_name: str) -> bool:
        """Create a new workspace in Kubernetes"""
        try:
            # Create namespace
            namespace = f"workspace-{workspace_id}"
            self.core_v1.create_namespace(
                client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=namespace,
                        labels={
                            "workspace-id": workspace_id,
                            "pool-name": pool_name
                        }
                    )
                )
            )
            
            # Create other resources (secrets, deployments, etc.)
            # This would typically call your existing workspace creation logic
            # For now, we'll just create a simple deployment
            
            deployment = client.V1Deployment(
                metadata=client.V1ObjectMeta(
                    name="workspace",
                    namespace=namespace
                ),
                spec=client.V1DeploymentSpec(
                    replicas=1,
                    selector=client.V1LabelSelector(
                        match_labels={"app": "workspace"}
                    ),
                    template=client.V1PodTemplateSpec(
                        metadata=client.V1ObjectMeta(
                            labels={"app": "workspace"}
                        ),
                        spec=client.V1PodSpec(
                            containers=[
                                client.V1Container(
                                    name="workspace",
                                    image="linuxserver/code-server:latest",
                                    env=[
                                        client.V1EnvVar(
                                            name="GITHUB_URL",
                                            value=repo_name
                                        ),
                                        client.V1EnvVar(
                                            name="GITHUB_BRANCH",
                                            value=branch_name
                                        )
                                    ]
                                )
                            ]
                        )
                    )
                )
            )
            
            self.apps_v1.create_namespaced_deployment(
                namespace=namespace,
                body=deployment
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating workspace: {e}")
            return False
    
    def _cleanup_pool(self, pool: Pool):
        """Clean up all resources associated with a pool"""
        for workspace_id in list(pool.workspace_ids):
            try:
                namespace = f"workspace-{workspace_id}"
                self.core_v1.delete_namespace(namespace)
                pool.remove_workspace(workspace_id)
            except Exception as e:
                logger.error(f"Error cleaning up workspace {workspace_id}: {e}")
                # Continue with other workspaces
    
    def _check_workspace_health(self, workspace_id: str) -> bool:
        """Check if a workspace is healthy"""
        try:
            ns_name = f"workspace-{workspace_id}"
            deployment = self.apps_v1.read_namespaced_deployment_status(
                name="workspace",
                namespace=ns_name
            )
            
            return (
                deployment.status.available_replicas == 1 and
                deployment.status.ready_replicas == 1
            )
            
        except Exception as e:
            logger.error(f"Error checking workspace {workspace_id} health: {e}")
            return False
    
    def update_pool(self, original_name, new_name, minimum_vms, repo_name, branch_name=None, github_pat=None):
        """Update an existing pool"""
        try:
            # Validate pool exists first
            pool = self.get_pool(original_name)
            if not pool:
                logger.error(f"Pool {original_name} not found")
                return False

            # Update pool object attributes
            pool.minimum_vms = minimum_vms
            pool.repo_name = repo_name
            pool.branch_name = branch_name
            if github_pat:
                pool.github_pat = github_pat
            
            # If name changed, update the key in self.pools
            if original_name != new_name:
                # Update pool name
                pool.name = new_name
                
                # Update all workspace namespace labels
                try:
                    # Using the same pool= label selector as used in _create_workspace and _count_pool_workspaces
                    namespaces = self.core_v1.list_namespace(
                        label_selector=f"pool={original_name}"
                    )
                    for ns in namespaces.items:
                        # Update namespace labels with new pool name
                        patch = {
                            "metadata": {
                                "labels": {
                                    "pool": new_name  # Match the label key used in _create_workspace
                                }
                            }
                        }
                        self.core_v1.patch_namespace(
                            name=ns.metadata.name,
                            body=patch
                        )
                except Exception as e:
                    logger.error(f"Error updating workspace labels: {e}")
                    return False
                
                # Update pools dictionary
                self.pools[new_name] = pool
                del self.pools[original_name]

            # Get ConfigMap name - match the format used in _update_pool_status
            config_map_name = f"pool-{original_name}"

            # Get existing ConfigMap
            try:
                config_map = self.core_v1.read_namespaced_config_map(
                    name=config_map_name,
                    namespace="workspace-system"
                )
            except Exception as e:
                logger.error(f"Error reading ConfigMap: {e}")
                return False

            # Update pool data for ConfigMap
            pool_data = {
                "name": new_name,
                "minimum_vms": minimum_vms,
                "repo_name": repo_name,
                "branch_name": branch_name,
                "last_check": datetime.now().isoformat(),
                "is_healthy": True,  # Reset health status after update
                "workspace_count": len(pool.workspace_ids),
                "status_message": "Pool updated successfully"
            }

            # If name changed, create new ConfigMap and delete old one
            if original_name != new_name:
                # Create new ConfigMap - match the format used in _update_pool_status
                new_config_map = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(
                        name=f"pool-{new_name}",
                        namespace="workspace-system"
                    ),
                    data={'status': json.dumps(pool_data)}  # Match the data key used in _update_pool_status
                )

                try:
                    self.core_v1.create_namespaced_config_map(
                        namespace="workspace-system",
                        body=new_config_map
                    )
                    # Delete old ConfigMap
                    self.core_v1.delete_namespaced_config_map(
                        name=config_map_name,
                        namespace="workspace-system"
                    )
                except Exception as e:
                    logger.error(f"Error updating ConfigMaps: {e}")
                    return False
            else:
                # Update existing ConfigMap
                config_map.data["config"] = json.dumps(pool_data)
                try:
                    self.core_v1.replace_namespaced_config_map(
                        name=config_map_name,
                        namespace="workspace-system",
                        body=config_map
                    )
                except Exception as e:
                    logger.error(f"Error replacing ConfigMap: {e}")
                    return False

            # Handle GitHub PAT update if provided
            if github_pat:
                old_secret_name = f"pool-{original_name}-github"
                new_secret_name = f"pool-{new_name}-github"
                secret_data = {"github_pat": base64.b64encode(github_pat.encode()).decode()}
                
                try:
                    # First try to update existing secret
                    try:
                        # Handle name change - create new secret and delete old one
                        if original_name != new_name:
                            # Create new secret
                            secret = client.V1Secret(
                                metadata=client.V1ObjectMeta(
                                    name=new_secret_name,
                                    namespace="workspace-system"
                                ),
                                data=secret_data
                            )
                            self.core_v1.create_namespaced_secret(
                                namespace="workspace-system",
                                body=secret
                            )
                            # Delete old secret
                            try:
                                self.core_v1.delete_namespaced_secret(
                                    name=old_secret_name,
                                    namespace="workspace-system"
                                )
                            except client.exceptions.ApiException as e:
                                if e.status != 404:  # Ignore if old secret doesn't exist
                                    raise
                        else:
                            # Just update existing secret
                            existing_secret = self.core_v1.read_namespaced_secret(
                                name=old_secret_name,
                                namespace="workspace-system"
                            )
                            existing_secret.data = secret_data
                            self.core_v1.replace_namespaced_secret(
                                name=old_secret_name,
                                namespace="workspace-system",
                                body=existing_secret
                            )
                    except client.exceptions.ApiException as e:
                        if e.status == 404:
                            # Create new secret if it doesn't exist
                            secret = client.V1Secret(
                                metadata=client.V1ObjectMeta(
                                    name=new_secret_name,
                                    namespace="workspace-system"
                                ),
                                data=secret_data
                            )
                            self.core_v1.create_namespaced_secret(
                                namespace="workspace-system",
                                body=secret
                            )
                        else:
                            raise
                except Exception as e:
                    logger.error(f"Error updating GitHub PAT secret: {e}")
                    return False

            # Update pool status
            self._update_pool_status(pool)

            return True

        except Exception as e:
            logger.error(f"Error updating pool: {e}")
            return False
    
    def mark_workspace_as_used(self, pool_name: str, workspace_id: str) -> bool:
        """Mark a workspace as being used in a pool"""
        try:
            pool = self.get_pool(pool_name)
            if not pool:
                logger.error(f"Pool {pool_name} not found")
                return False
                
            # Verify the workspace exists and is healthy
            if not self._check_workspace_exists(workspace_id):
                logger.error(f"Workspace {workspace_id} does not exist")
                return False
                
            if not self._check_workspace_health(workspace_id):
                logger.error(f"Workspace {workspace_id} is not healthy")
                return False
            
            try:
                pool.mark_workspace_as_used(workspace_id)
                self._update_pool_status(pool)
                logger.info(f"Marked workspace {workspace_id} as used in pool {pool_name}")
                return True
            except ValueError as e:
                logger.error(str(e))
                return False
                
        except Exception as e:
            logger.error(f"Error marking workspace as used: {e}")
            return False
    
    def get_available_workspaces(self, pool_name: str) -> list:
        """Get available workspaces in a pool"""
        try:
            pool = self.get_pool(pool_name)
            if not pool:
                logger.error(f"Pool {pool_name} not found")
                return []
            
            # Get unused workspace IDs
            available_ids = pool.get_available_workspaces()
            
            # Filter for only healthy workspaces
            healthy_workspaces = []
            for workspace_id in available_ids:
                # Skip if workspace doesn't exist or isn't healthy
                if not self._check_workspace_exists(workspace_id):
                    continue
                if not self._check_workspace_health(workspace_id):
                    continue
                    
                # Get workspace details
                try:
                    ns_name = f"workspace-{workspace_id}"
                    deployment = self.apps_v1.read_namespaced_deployment_status(
                        name="workspace",
                        namespace=ns_name
                    )
                    
                    healthy_workspaces.append({
                        'workspace_id': workspace_id,
                        'status': deployment.status.conditions[-1].message if deployment.status.conditions else None,
                        'ready_replicas': deployment.status.ready_replicas,
                        'namespace': ns_name,
                        'pool_name': pool_name
                    })
                except Exception as e:
                    logger.error(f"Error getting workspace {workspace_id} details: {e}")
                    continue
            
            return healthy_workspaces
            
        except Exception as e:
            logger.error(f"Error getting available workspaces: {e}")
            return []
            
    def release_workspace(self, pool_name: str, workspace_id: str) -> bool:
        """Release a used workspace back to the pool"""
        try:
            pool = self.get_pool(pool_name)
            if not pool:
                logger.error(f"Pool {pool_name} not found")
                return False
            
            pool.mark_workspace_as_unused(workspace_id)
            self._update_pool_status(pool)
            logger.info(f"Released workspace {workspace_id} in pool {pool_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error releasing workspace: {e}")
            return False
        
    def get_pool_workspaces(self, pool_name: str) -> list:
        """Get all workspaces in a pool (both available and in-use)"""
        try:
            pool = self.get_pool(pool_name)
            if not pool:
                logger.error(f"Pool {pool_name} not found")
                return None
            
            workspaces = []
            for workspace_id in pool.workspace_ids:
                if not self._check_workspace_exists(workspace_id):
                    continue
                    
                # Get workspace details
                try:
                    ns_name = f"workspace-{workspace_id}"
                    deployment = self.apps_v1.read_namespaced_deployment_status(
                        name="workspace",  # Changed from code-server to workspace to match _create_workspace
                        namespace=ns_name
                    )
                    
                    # Get workspace status
                    status = "unknown"
                    if deployment.status.conditions:
                        if deployment.status.available_replicas == 1:
                            status = "running"
                        elif deployment.status.conditions[-1].reason == "ProgressDeadlineExceeded":
                            status = "failed"
                        else:
                            status = "pending"
                    
                    workspaces.append({
                        'id': workspace_id,
                        'status': status,
                        'ready_replicas': deployment.status.ready_replicas or 0,
                        'namespace': ns_name,
                        'pool_name': pool_name,
                        'in_use': workspace_id in pool.used_workspace_ids,
                        'conditions': [
                            {'type': c.type, 'status': c.status, 'message': c.message}
                            for c in (deployment.status.conditions or [])
                        ]
                    })
                except Exception as e:
                    logger.error(f"Error getting workspace {workspace_id} details: {e}")
                    continue
            
            return workspaces
            
        except Exception as e:
            logger.error(f"Error getting pool workspaces: {e}")
            return None
