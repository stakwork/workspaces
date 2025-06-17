import logging
import uuid
import json
from threading import Thread
import time
import string
import random
from kubernetes import client
from typing import Optional
from datetime import datetime
import base64

from models.pool import Pool
from models.cleanup import CleanupStatus
from utils.workspace_init import generate_init_script
from .workspace_initializer import WorkspaceInitializer

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

        # Read workspace configuration from ConfigMap
        try:
            config_map = self.core_v1.read_namespaced_config_map("workspace-config", "workspace-system")
            self.workspace_domain = config_map.data.get("workspace-domain", "SUBDOMAIN_REPLACE_ME")
            self.aws_account_id = config_map.data.get("aws-account-id", "AWS_ACCOUNT_ID_REPLACE_ME")
            logger.info(f"Using workspace domain: {self.workspace_domain}")
        except Exception as e:
            logger.error(f"Error reading workspace config: {e}")
            self.workspace_domain = "SUBDOMAIN_REPLACE_ME"
            self.aws_account_id = "AWS_ACCOUNT_ID_REPLACE_ME"

        self.workspace_initializer = WorkspaceInitializer(core_v1, apps_v1, self.aws_account_id, self.workspace_domain)

    def _generate_random_subdomain(self, length=8):
        """Generate a random subdomain name"""
        letters = string.ascii_lowercase + string.digits
        return ''.join(random.choice(letters) for i in range(length))

    def _random_password(self, length=12):
        """Generate a random password"""
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for i in range(length))

    def create_pool(self, name: str, minimum_vms: int, repo_name: str, 
                   branch_name: str, github_pat: str) -> Optional[Pool]:
        """Create a new pool with the given configuration"""
        try:
            # Validate pool name format
            if not name or not name.strip() or not name.replace("-", "").isalnum():
                logger.error(f"Invalid pool name: {name}")
                raise ValueError("Pool name must be alphanumeric (hyphens allowed)")

            # Check if pool already exists - case insensitive
            existing_pool_name = next((p for p in self.pools.keys() if p.lower() == name.lower()), None)
            if existing_pool_name:
                logger.error(f"Pool {name} already exists (as {existing_pool_name})")
                raise ValueError(f"Pool already exists with name: {existing_pool_name}")

            # Validate minimum VMs
            if minimum_vms < 1:
                logger.error(f"Invalid minimum VMs: {minimum_vms}")
                raise ValueError("Minimum VMs must be at least 1")

            # Validate kubernetes API connectivity first
            try:
                self.core_v1.list_namespace(limit=1)
            except client.exceptions.ApiException as e:
                if e.status == 503:
                    logger.error("Kubernetes API is currently unavailable")
                    raise RuntimeError("Kubernetes API is currently unavailable") from e
                raise RuntimeError(f"Kubernetes API error: {e}") from e
                
            pool = Pool(name, minimum_vms, repo_name, branch_name, github_pat)
            # Initialize pool attributes
            pool.workspace_ids = set()
            pool.workspace_count = 0
            pool.is_healthy = True
            pool.status_message = "Pool created"
            pool.last_check = datetime.now()
            
            self.pools[name] = pool
            logger.info(f"Created new pool: {name} with {minimum_vms} minimum VMs")

            # Create ConfigMap to store pool configuration
            try:
                config_map = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(
                        name=f"pool-{name}",
                        namespace="workspace-system"
                    ),
                    data={
                        'config': json.dumps({
                            'name': name,
                            'minimum_vms': minimum_vms,
                            'repo_name': repo_name,
                            'branch_name': branch_name,
                            'created_at': datetime.now().isoformat()
                        })
                    }
                )
                self.core_v1.create_namespaced_config_map(
                    namespace="workspace-system",
                    body=config_map
                )
            except client.exceptions.ApiException as e:
                logger.error(f"Failed to create pool ConfigMap: {e}")
                del self.pools[name]
                raise RuntimeError(f"Failed to create pool configuration: {e}") from e
            
            # Validate repository name and branch
            if not repo_name or not repo_name.strip():
                raise ValueError("Repository name is required")

            # Create initial workspaces with better error handling
            failed_workspaces = []
            created_workspaces = []
            for i in range(minimum_vms):
                workspace_id = str(uuid.uuid4())
                try:
                    logger.info(f"Creating workspace {i+1}/{minimum_vms} for pool {name}")
                    if self._create_workspace(workspace_id, repo_name, branch_name, github_pat, name):
                        pool.add_workspace(workspace_id)
                        created_workspaces.append(workspace_id)
                        logger.info(f"Created workspace {workspace_id} for pool {name}")
                    else:
                        failed_workspaces.append(workspace_id)
                        logger.error(f"Failed to create workspace {workspace_id} for pool {name}")
                except Exception as e:
                    logger.error(f"Failed to create workspace {workspace_id}: {str(e)}")
                    failed_workspaces.append(workspace_id)
                    # Clean up any partially created resources for this workspace
                    try:
                        self._cleanup_workspace(workspace_id)
                    except Exception as cleanup_error:
                        logger.error(f"Failed to cleanup failed workspace {workspace_id}: {cleanup_error}")

            if failed_workspaces:
                # Update pool status to reflect partial creation
                pool.update_status(
                    is_healthy=False, 
                    message=f"Pool created with {len(failed_workspaces)} failed workspaces"
                )
            else:
                pool.update_status(True, "Pool created successfully")

            # Update pool status in ConfigMap
            self._update_pool_status(pool)
            return pool
            
        except Exception as e:
            logger.error(f"Error creating pool {name}: {e}")
            if name in self.pools:
                del self.pools[name]
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

    def create_workspace_from_pool(self, pool: Pool) -> str:
        """Create a new workspace using pool configuration.
        
        Args:
            pool: The Pool object containing the configuration
            
        Returns:
            The workspace ID if creation was successful, None otherwise
        """
        try:
            workspace_id = str(uuid.uuid4())
            
            # Create workspace using pool configuration
            if self._create_workspace(
                workspace_id=workspace_id,
                repo_name=pool.repo_name,
                branch_name=pool.branch_name,
                github_pat=pool.github_pat,
                pool_name=pool.name
            ):
                # Add workspace to pool tracking
                pool.add_workspace(workspace_id)
                logger.info(f"Created workspace {workspace_id} for pool {pool.name}")
                return workspace_id
            else:
                logger.error(f"Failed to create workspace for pool {pool.name}")
                return None
                
        except Exception as e:
            logger.error(f"Error creating workspace for pool {pool.name}: {e}")
            return None

    def _update_pool_status(self, pool):
        """Update pool status in ConfigMap"""
        try:
            # Prepare status data with explicit type conversion to avoid JSON serialization issues
            status_data = {
                'workspace_count': int(getattr(pool, 'workspace_count', 0)),
                'is_healthy': bool(getattr(pool, 'is_healthy', False)),
                'status_message': str(getattr(pool, 'status_message', '')),
                'last_check': datetime.now().isoformat(),
                'workspace_ids': list(pool.workspace_ids),
                'used_workspace_ids': list(pool.used_workspace_ids),
                'minimum_vms': int(pool.minimum_vms)
            }
            
            # Validate JSON serialization before updating ConfigMap
            try:
                status_json = json.dumps(status_data)
            except (TypeError, ValueError) as e:
                logger.error(f"Failed to serialize pool status: {e}")
                # Fallback to basic status
                status_data = {
                    'workspace_count': 0,
                    'is_healthy': False,
                    'status_message': f'Status update failed: {str(e)}',
                    'last_check': datetime.now().isoformat()
                }
                status_json = json.dumps(status_data)
            
            try:
                cm = self.core_v1.read_namespaced_config_map(
                    name=f"pool-{pool.name}",
                    namespace="workspace-system"
                )
                cm.data['status'] = status_json
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
        """Create a new workspace in Kubernetes with enhanced initialization tracking"""
        try:
            # Generate build timestamp
            build_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

            # Use PoolWorkspaceInitializer to set up environment workspaces
            self.workspace_initializer.initialize_workspace(
                workspace_id, repo_name, branch_name, github_pat, pool_name, build_timestamp=build_timestamp
            )

            # Create ConfigMap for feature installation script
            feature_script_cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name="feature-install",
                    namespace=f"workspace-{workspace_id}",
                    labels={"app": "workspace"}
                ),
                data={
                    "install-features.sh": ""
                }
            )

            # Generate workspace identifiers
            subdomain = self._generate_random_subdomain()
            namespace = f"workspace-{workspace_id}"
            
            # Create initialization status ConfigMap
            init_status_cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name="workspace-init-status",
                    namespace=namespace
                ),
                data={
                    'status': json.dumps({
                        'phase': 'creating',
                        'start_time': datetime.now().isoformat(),
                        'pool_name': pool_name,
                        'workspace_id': workspace_id,
                        'last_status': 'Initializing workspace'
                    })
                }
            )
            fqdn = f"{subdomain}.{self.workspace_domain}"
            password = self._random_password()
            
            # Create namespace
            self.core_v1.create_namespace(
                client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=namespace,
                        labels={
                            "workspace-id": workspace_id,
                            "pool": pool_name,
                            "app": "workspace",
                            "initialization": "in-progress"
                        }
                    )
                )
            )
            
            # Create feature installation script ConfigMap
            self.core_v1.create_namespaced_config_map(namespace, feature_script_cm)

            # Create initialization status ConfigMap
            self.core_v1.create_namespaced_config_map(
                namespace=namespace,
                body=init_status_cm
            )
            
            # Normalize GitHub repository URL
            github_url = repo_name
            if not github_url.startswith(("http://", "https://")):
                if github_url.startswith("github.com/"):
                    github_url = f"https://{github_url}"
                else:
                    github_url = f"https://github.com/{github_url}"

            # Create workspace config with normalized URL and branch
            workspace_config = {
                'github_urls': [github_url],
                'github_branches': [branch_name if branch_name else "main"],  # Use main as default if no branch specified
                'github_pat': github_pat,
                'use_custom_image_url': False
            }

            # Generate workspace IDs for init script
            workspace_ids = {
                'namespace_name': namespace,
                'build_timestamp': datetime.now().strftime('%Y%m%d%H%M%S'),
                'workspace_id': workspace_id
            }
            
            # Generate the initialization script
            init_script = generate_init_script(workspace_ids, workspace_config)

            # Create PersistentVolumeClaim for workspace storage
            pvc = client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(
                    name="registry-storage",
                    namespace=namespace,
                    labels={"app": "workspace"}
                ),
                spec=client.V1PersistentVolumeClaimSpec(
                    access_modes=["ReadWriteMany"],
                    resources=client.V1ResourceRequirements(
                        requests={"storage": "10Gi"}
                    ),
                    storage_class_name="efs-sc"
                )
            )
            self.core_v1.create_namespaced_persistent_volume_claim(
                namespace=namespace,
                body=pvc
            )

            # Create workspace initialization script ConfigMap            
            init_config_map = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name="workspace-init",
                    namespace=namespace,
                    labels={"app": "workspace"}
                ),
                data={"init.sh": init_script}
            )
            self.core_v1.create_namespaced_config_map(namespace, init_config_map)
            
            # Create workspace secret
            secret = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name="workspace-secret",
                    namespace=namespace,
                    labels={"app": "workspace"}
                ),
                string_data={
                    "password": password
                }
            )
            if github_pat:
                secret.string_data["github_token"] = github_pat
            self.core_v1.create_namespaced_secret(namespace, secret)
            
            # Copy wildcard certificate
            try:
                wildcard_cert = self.core_v1.read_namespaced_secret(
                    name="workspace-domain-wildcard-tls", 
                    namespace="workspace-system"
                )
                
                wildcard_cert_new = client.V1Secret(
                    metadata=client.V1ObjectMeta(
                        name="workspace-domain-wildcard-tls",
                        namespace=namespace,
                        labels={"app": "workspace"}
                    ),
                    data=wildcard_cert.data,
                    type=wildcard_cert.type
                )
                self.core_v1.create_namespaced_secret(namespace, wildcard_cert_new)
            except Exception as e:
                logger.error(f"Error copying wildcard certificate: {e}")
            
            # Create the workspace deployment
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
                            init_containers=[
                                # Feature installation container
                                client.V1Container(
                                    name="feature-installer",
                                    image="buildpack-deps:22.04-scm",
                                    command=["/bin/bash", "/scripts/features/install-features.sh"],
                                    env=[
                                        client.V1EnvVar(name="HOME", value="/root"),
                                        client.V1EnvVar(name="USER", value="root")
                                    ],
                                    volume_mounts=[
                                        client.V1VolumeMount(
                                            name="registry-storage",
                                            mount_path="/workspaces",
                                            sub_path="workspaces"
                                        ),
                                        client.V1VolumeMount(
                                            name="feature-script",
                                            mount_path="/scripts/features",
                                            read_only=True
                                        ),
                                        client.V1VolumeMount(
                                            name="registry-storage",
                                            mount_path="/root",
                                            sub_path="home"
                                        )
                                    ]
                                ),
                                # Workspace initialization container
                                client.V1Container(
                                    name="init-workspace",
                                    image="buildpack-deps:22.04-scm",
                                    command=["/bin/bash", "/scripts/init.sh"],
                                    env=[
                                        client.V1EnvVar(name="HOME", value="/root"),
                                        client.V1EnvVar(name="USER", value="root"),
                                        client.V1EnvVar(
                                            name="GITHUB_TOKEN",
                                            value_from=client.V1EnvVarSource(
                                                secret_key_ref=client.V1SecretKeySelector(
                                                    name="workspace-secret",
                                                    key="github_token",
                                                    optional=True
                                                )
                                            )
                                        )
                                    ],
                                    volume_mounts=[
                                        client.V1VolumeMount(
                                            name="registry-storage",
                                            mount_path="/workspaces",
                                            sub_path="workspaces"
                                        ),
                                        client.V1VolumeMount(
                                            name="init-script",
                                            mount_path="/scripts"
                                        ),
                                        client.V1VolumeMount(
                                            name="feature-script",
                                            mount_path="/scripts/features",
                                            read_only=True
                                        ),
                                        client.V1VolumeMount(
                                            name="registry-storage",
                                            mount_path="/root",
                                            sub_path="home"
                                        )
                                    ]
                                )
                            ],
                            containers=[
                                client.V1Container(
                                    name="code-server",
                                    image="linuxserver/code-server:latest",
                                    env=[
                                        client.V1EnvVar(name="PUID", value="1000"),
                                        client.V1EnvVar(name="PGID", value="1000"),
                                        client.V1EnvVar(name="TZ", value="UTC"),
                                        client.V1EnvVar(
                                            name="PASSWORD",
                                            value_from=client.V1EnvVarSource(
                                                secret_key_ref=client.V1SecretKeySelector(
                                                    name="workspace-secret",
                                                    key="password"
                                                )
                                            )
                                        ),
                                        client.V1EnvVar(name="DOCKER_HOST", value="unix:///var/run/docker.sock"),
                                        client.V1EnvVar(name="GITHUB_TOKEN",
                                            value_from=client.V1EnvVarSource(
                                                secret_key_ref=client.V1SecretKeySelector(
                                                    name="workspace-secret",
                                                    key="github_token",
                                                    optional=True
                                                )
                                            )
                                        )
                                    ],
                                    volume_mounts=[
                                        client.V1VolumeMount(
                                            name="registry-storage",
                                            mount_path="/config",
                                            sub_path="config"
                                        ),
                                        client.V1VolumeMount(
                                            name="registry-storage", 
                                            mount_path="/workspaces",
                                            sub_path="workspaces"
                                        ),
                                        client.V1VolumeMount(
                                            name="docker-lib",
                                            mount_path="/var/lib/docker"
                                        ),
                                        client.V1VolumeMount(
                                            name="docker-sock",
                                            mount_path="/var/run"
                                        ),
                                        client.V1VolumeMount(
                                            name="feature-script",
                                            mount_path="/scripts/features",
                                            read_only=True
                                        )
                                    ],
                                    lifecycle=client.V1Lifecycle(
                                        post_start=client.V1LifecycleHandler(
                                            _exec=client.V1ExecAction(
                                                command=self._create_post_start_command()
                                            )
                                        ),
                                        pre_stop=client.V1LifecycleHandler(
                                            _exec=client.V1ExecAction(
                                                command=["/bin/sh", "-c", "echo 'stopping' > /workspaces/.pool-init-status"]
                                            )
                                        )
                                    ),
                                    security_context=client.V1SecurityContext(
                                        privileged=True,
                                        capabilities=client.V1Capabilities(
                                            add=["SYS_ADMIN", "NET_ADMIN"]
                                        )
                                    )
                                )
                            ],
                            volumes=[
                                client.V1Volume(
                                    name="registry-storage",
                                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                        claim_name="registry-storage"
                                    )
                                ),
                                client.V1Volume(
                                    name="init-script",
                                    config_map=client.V1ConfigMapVolumeSource(
                                        name="workspace-init",
                                        default_mode=0o755
                                    )
                                ),
                                client.V1Volume(
                                    name="docker-lib",
                                    empty_dir={}
                                ),
                                client.V1Volume(
                                    name="docker-sock",
                                    empty_dir={}
                                ),
                                client.V1Volume(
                                    name="feature-script",
                                    config_map=client.V1ConfigMapVolumeSource(
                                        name="feature-install",
                                        default_mode=0o755
                                    )
                                )
                            ]
                        )
                    )
                )
            )
            self.apps_v1.create_namespaced_deployment(namespace, deployment)

            # Create service
            service = client.V1Service(
                metadata=client.V1ObjectMeta(
                    name="code-server",
                    namespace=namespace
                ),
                spec=client.V1ServiceSpec(
                    ports=[
                        client.V1ServicePort(
                            port=8443,
                            target_port=8443,
                            name="code-server"
                        )
                    ],
                    selector={"app": "workspace"}
                )
            )
            self.core_v1.create_namespaced_service(namespace, service)
            
            # Create ingress
            ingress = client.V1Ingress(
                metadata=client.V1ObjectMeta(
                    name="code-server",
                    namespace=namespace,
                    annotations={
                        "kubernetes.io/ingress.class": "nginx",
                        "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
                        "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600"
                    }
                ),
                spec=client.V1IngressSpec(
                    tls=[
                        client.V1IngressTLS(
                            hosts=[f"*.{self.workspace_domain}"],
                            secret_name="workspace-domain-wildcard-tls"
                        )
                    ],
                    rules=[
                        client.V1IngressRule(
                            host=fqdn,
                            http=client.V1HTTPIngressRuleValue(
                                paths=[
                                    client.V1HTTPIngressPath(
                                        path="/",
                                        path_type="Prefix",
                                        backend=client.V1IngressBackend(
                                            service=client.V1IngressServiceBackend(
                                                name="code-server",
                                                port=client.V1ServiceBackendPort(
                                                    number=8443
                                                )
                                            )
                                        )
                                    )
                                ]
                            )
                        )
                    ]
                )
            )
            
            networking_v1 = client.NetworkingV1Api()
            networking_v1.create_namespaced_ingress(namespace, ingress)
            
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
    
    def _check_workspace_health(self, workspace_id: str) -> tuple[bool, str]:
        """Check if a workspace is healthy and return status details"""
        try:
            ns_name = f"workspace-{workspace_id}"
            
            # Check initialization status
            try:
                init_status = self.core_v1.read_namespaced_config_map(
                    name="workspace-init-status",
                    namespace=ns_name
                )
                status_data = json.loads(init_status.data.get('status', '{}'))
                if status_data.get('phase') == 'failed':
                    return False, f"Initialization failed: {status_data.get('last_status', 'Unknown error')}"
            except client.exceptions.ApiException as e:
                if e.status != 404:  # Ignore if ConfigMap doesn't exist
                    logger.warning(f"Error reading init status for {workspace_id}: {e}")
            
            # Check deployment status
            deployment = self.apps_v1.read_namespaced_deployment_status(
                name="workspace",
                namespace=ns_name
            )
            
            # Check init container status
            pod_list = self.core_v1.list_namespaced_pod(
                namespace=ns_name,
                label_selector="app=workspace"
            )
            
            if not pod_list.items:
                return False, "No pods found for workspace"
                
            pod = pod_list.items[0]  # Get the first pod
            init_status = "pending"
            
            if pod.status.init_container_statuses:
                init_container = pod.status.init_container_statuses[0]
                if init_container.state.waiting:
                    return False, f"Init container waiting: {init_container.state.waiting.reason}"
                elif init_container.state.terminated:
                    if init_container.state.terminated.exit_code != 0:
                        return False, f"Init container failed: {init_container.state.terminated.reason}"
                    init_status = "completed"
            
            # Check main container and deployment status
            is_healthy = (
                deployment.status.available_replicas == 1 and
                deployment.status.ready_replicas == 1 and
                init_status == "completed"
            )
            
            if not is_healthy:
                if deployment.status.conditions:
                    return False, deployment.status.conditions[-1].message or "Deployment not ready"
                return False, "Deployment not ready"
                
            return True, "Workspace is healthy"
            
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
                    namespaces = self.core_v1.list_namespace(
                        label_selector=f"pool={original_name}"
                    )
                    for ns in namespaces.items:
                        patch = {
                            "metadata": {
                                "labels": {
                                    "pool": new_name
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

                # Restore workspace_ids and used_workspace_ids from ConfigMap
                status_data = json.loads(config_map.data.get('status', '{}'))
                pool.workspace_ids = set(status_data.get('workspace_ids', []))
                pool.used_workspace_ids = set(status_data.get('used_workspace_ids', []))
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
                "is_healthy": True,
                "workspace_count": len(pool.workspace_ids),
                "status_message": "Pool updated successfully"
            }

            if original_name != new_name:
                new_config_map = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(
                        name=f"pool-{new_name}",
                        namespace="workspace-system"
                    ),
                    data={'status': json.dumps(pool_data)}
                )

                try:
                    self.core_v1.create_namespaced_config_map(
                        namespace="workspace-system",
                        body=new_config_map
                    )
                    self.core_v1.delete_namespaced_config_map(
                        name=config_map_name,
                        namespace="workspace-system"
                    )
                except Exception as e:
                    logger.error(f"Error updating ConfigMaps: {e}")
                    return False
            else:
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
                    if original_name != new_name:
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
                        try:
                            self.core_v1.delete_namespaced_secret(
                                name=old_secret_name,
                                namespace="workspace-system"
                            )
                        except client.exceptions.ApiException as e:
                            if e.status != 404:
                                raise
                    else:
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
                except Exception as e:
                    logger.error(f"Error updating GitHub PAT secret: {e}")
                    return False

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
                    
                    # Get workspace info from ConfigMap
                    try:
                        info_config_map = self.core_v1.read_namespaced_config_map(
                            name="workspace-info",
                            namespace=ns_name
                        )
                        workspace_info = json.loads(info_config_map.data["info"])
                    except Exception as e:
                        logger.warning(f"Could not read workspace info for {workspace_id}: {e}")
                        workspace_info = {}
                    
                    deployment = self.apps_v1.read_namespaced_deployment_status(
                        name="workspace",
                        namespace=ns_name
                    )
                    
                    healthy_workspaces.append({
                        'workspace_id': workspace_id,
                        'status': deployment.status.conditions[-1].message if deployment.status.conditions else None,
                        'ready_replicas': deployment.status.ready_replicas,
                        'namespace': ns_name,
                        'pool_name': pool_name,
                        'subdomain': workspace_info.get('subdomain'),
                        'fqdn': workspace_info.get('fqdn'),
                        'password': workspace_info.get('password')
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
                return []
            
            workspaces = []
            for workspace_id in pool.workspace_ids:
                if not self._check_workspace_exists(workspace_id):
                    continue
                    
                # Get workspace details
                try:
                    ns_name = f"workspace-{workspace_id}"
                    
                    # Get workspace info from ConfigMap
                    workspace_info = {}
                    try:
                        info_config_map = self.core_v1.read_namespaced_config_map(
                            name="workspace-info",
                            namespace=ns_name
                        )
                        workspace_info = json.loads(info_config_map.data.get("info", "{}"))
                    except Exception as e:
                        logger.warning(f"Could not read workspace info for {workspace_id}: {e}")
                    
                    deployment = self.apps_v1.read_namespaced_deployment_status(
                        name="workspace",
                        namespace=ns_name
                    )
                    
                    # Get workspace status
                    status = "unknown"
                    if deployment.status.available_replicas == 1:
                        status = "running"
                    elif deployment.status.conditions and deployment.status.conditions[-1].reason == "ProgressDeadlineExceeded":
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
                        ],
                        'subdomain': workspace_info.get('subdomain'),
                        'fqdn': workspace_info.get('fqdn'),
                        'password': workspace_info.get('password')
                    })
                except Exception as e:
                    logger.error(f"Error getting workspace {workspace_id} details: {e}")
                    # Return basic info even if full details can't be retrieved
                    workspaces.append({
                        'id': workspace_id,
                        'status': 'error',
                        'namespace': f"workspace-{workspace_id}",
                        'pool_name': pool_name,
                        'in_use': workspace_id in pool.used_workspace_ids,
                        'error': str(e)
                    })
            
            return workspaces
            
        except Exception as e:
            logger.error(f"Error getting pool workspaces: {e}")
            return []
    
    def _create_post_start_command(self) -> list:
        """Create the post-start command for workspace container initialization.

        Returns a command array to be used as a post-start lifecycle hook that:
        1. Waits for workspace initialization
        2. Installs VS Code extensions
        3. Sets up environment variables
        4. Installs and configures devcontainer features
        5. Validates the initialization

        The command includes retry logic and proper progress tracking.
        """
        commands = [
            "#!/bin/bash",
            "set -e",  # Exit on error
            "set -x",  # Print commands for debugging
            
            # Create status file
            "STATUS_FILE=/workspaces/.pool-init-status",
            "echo 'starting' > $STATUS_FILE",
            
            # Function to update status
            "update_status() {",
            "    local status=$1",
            "    echo \"$status\" > $STATUS_FILE",
            "    echo \"[$(date)] $status\"",
            "    # Update initialization status ConfigMap",
            "    if [ -n \"$status\" ]; then",
            "        kubectl patch configmap workspace-init-status -n $NAMESPACE -p \"{\\\"data\\\":{\\\"status\\\":\\\"{\\\"phase\\\":\\\"$status\\\",\\\"last_update\\\":\\\"$(date -Iseconds)\\\",\\\"last_status\\\":\\\"$status\\\"}\\\"}}\" || true",
            "    fi",
            "}",

            # Function for retrying commands
            "retry() {",
            "    local n=1",
            "    local max=5",
            "    local delay=15",
            "    while true; do",
            "        echo \"Attempt $n/$max: $@\"",
            "        \"$@\" && break || {",
            "            if [[ $n -lt $max ]]; then",
            "                ((n++))",
            "                echo \"Command failed. Attempt $n/$max:\"",
            "                sleep $delay;",
            "            else",
            "                echo \"The command has failed after $n attempts.\"",
            "                return 1",
            "            fi",
            "        }",
            "    done",
            "}",

            # Wait for workspace initialization with timeout
            "update_status 'waiting_for_init'",
            "TIMEOUT=300  # 5 minutes timeout",
            "COUNTER=0",
            "while [ ! -f /workspaces/.pool-workspace-initialized ]; do",
            "    if [ $COUNTER -ge $TIMEOUT ]; then",
            "        update_status 'init_timeout'",
            "        echo 'Workspace initialization timed out after 5 minutes'",
            "        exit 1",
            "    fi",
            "    echo 'Waiting for workspace initialization...'",
            "    sleep 2",
            "    COUNTER=$((COUNTER + 2))",
            "done",
            
            # Create logs directory
            "mkdir -p /workspaces/logs",
            
            # Install VS Code extensions if any were found
            "if [ -f /workspaces/.extensions-list ]; then",
            "    update_status 'installing_extensions'",
            "    # First wait for code-server to be ready",
            "    TIMEOUT=60",
            "    while ! pgrep -f code-server > /dev/null; do",
            "        if [ $TIMEOUT -le 0 ]; then",
            "            update_status 'code_server_timeout'",
            "            echo 'Timeout waiting for code-server to start'",
            "            exit 1",
            "        fi",
            "        echo 'Waiting for code-server to start...'",
            "        sleep 2",
            "        TIMEOUT=$((TIMEOUT - 2))",
            "done",
            "    # Additional wait to ensure code-server is fully initialized",
            "    sleep 10",
            "    # Install extensions with retry logic",
            "    while IFS= read -r extension; do",
            "        if [ ! -z \"$extension\" ]; then",
            "            echo \"Installing extension: $extension\"",
            "            retry code-server --install-extension \"$extension\" >> /workspaces/logs/extension_install.log 2>&1 || {",
            "                echo \"Warning: Failed to install extension $extension after retries\"",
            "            }",
            "        fi",
            "    done < /workspaces/.extensions-list",
            "fi",
            
            # Set environment variables if any were found
            "if [ -f /workspaces/.container-env ]; then",
            "    update_status 'setting_env_vars'",
            "    echo 'Setting environment variables...'",
            "    mkdir -p ~/.config/code-server",
            "    echo '# Environment variables set by workspace initialization' > ~/.config/code-server/env",
            "    while IFS= read -r env_var; do",
            "        if [ ! -z \"$env_var\" ]; then",
            "            echo \"export $env_var\" >> ~/.config/code-server/env",
            "            echo \"Added environment variable: $env_var\"",
            "        fi",
            "    done < /workspaces/.container-env",
            "    chmod 600 ~/.config/code-server/env",  # Secure the env file
            # Install and configure devcontainer features if any were found
            "if [ -f /workspaces/.devcontainer-features ]; then",
            "    update_status 'installing_features'",
            "    echo 'Installing devcontainer features...'",
            "    apt-get update && apt-get install -y jq || {",
            "        echo 'Failed to install jq, cannot process features'",
            "        update_status 'feature_install_failed'",
            "        exit 1",
            "    }",
            "    FEATURES=$(cat /workspaces/.devcontainer-features)",

            # Docker in Docker setup with health check
            "    if echo \"$FEATURES\" | jq -e '.docker-in-docker != null' > /dev/null; then",
            "        echo 'Setting up Docker in Docker...'",
            "        curl -fsSL https://get.docker.com -o get-docker.sh",
            "        sh get-docker.sh >> /workspaces/logs/docker_install.log 2>&1 || {",
            "            echo 'Failed to install Docker'",
            "            update_status 'docker_install_failed'",
            "            exit 1",
            "        }",
            "        # Start Docker daemon with logging",
            "        mkdir -p /var/log",
            "        dockerd >> /var/log/dockerd.log 2>&1 &",
            "        # Wait for Docker to be ready",
            "        TIMEOUT=30",
            "        until docker info >/dev/null 2>&1; do",
            "            if [ $TIMEOUT -le 0 ]; then",
            "                echo 'Docker daemon failed to start'",
            "                update_status 'docker_start_failed'",
            "                exit 1",
            "            fi",
            "            echo 'Waiting for Docker daemon to start...'",
            "            sleep 1",
            "            TIMEOUT=$((TIMEOUT - 1))",
            "        done",
            "        echo 'Docker daemon started successfully'",
            "        # Verify Docker works by running hello-world",
            "        docker run --rm hello-world > /workspaces/logs/docker_test.log 2>&1 || {",
            "            echo 'Docker test failed'",
            "            update_status 'docker_test_failed'",
            "            exit 1",
            "        }",
            "    fi",

            # Git feature installation with validation
            "    if echo \"$FEATURES\" | jq -e '.git != null' > /dev/null; then",
            "        echo 'Installing additional Git tools...'",
            "        apt-get install -y git-lfs >> /workspaces/logs/git_install.log 2>&1 || {",
            "            echo 'Failed to install git-lfs'",
            "            update_status 'git_install_failed'",
            "            exit 1",
            "        }",
            "        git lfs install >> /workspaces/logs/git_lfs.log 2>&1",
            "        # Verify git-lfs installation",
            "        if ! git lfs version > /dev/null 2>&1; then",
            "            echo 'git-lfs verification failed'",
            "            update_status 'git_lfs_verify_failed'",
            "            exit 1",
            "        fi",
            "    fi",
            "fi",
            
            # Final validation
            "echo 'Validating initialization...'",
            "VALIDATION_FAILED=0",
            
            # Check code-server is running",
            "if ! pgrep -f code-server > /dev/null; then",
            "    echo 'ERROR: code-server is not running'",
            "    VALIDATION_FAILED=1",
            "fi",

            # Check extension installation logs if we installed any
            "if [ -f /workspaces/.extensions-list ] && [ -f /workspaces/logs/extension_install.log ]; then",
            "    if grep -i 'error' /workspaces/logs/extension_install.log > /dev/null; then",
            "        echo 'Warning: Some extensions may have failed to install'",
            "    fi",
            "fi",

            # Check Docker if it was installed
            "if [ -f /var/log/dockerd.log ]; then",
            "    if ! docker info > /dev/null 2>&1; then",
            "        echo 'ERROR: Docker is not running properly'",
            "        VALIDATION_FAILED=1",
            "    fi",
            "fi",

            # Final status update
            "if [ $VALIDATION_FAILED -eq 0 ]; then",
            "    update_status 'complete'",
            "    echo 'Post-start initialization completed successfully'",
            "    touch ~/.config/code-server/.post-start-complete",
            "else",
            "    update_status 'validation_failed'",
            "    echo 'Post-start initialization validation failed'",
            "    exit 1",
            "fi",
        ]
        
        return ["/bin/bash", "-c", " && ".join(commands)]

    def _cleanup_workspace(self, workspace_id: str):
        """Clean up a failed workspace's resources"""
        try:
            namespace = f"workspace-{workspace_id}"
            
            # Delete namespace which will cascade delete all resources in it
            try:
                self.core_v1.delete_namespace(namespace)
                logger.info(f"Cleaned up namespace {namespace}")
            except client.exceptions.ApiException as e:
                if e.status != 404:  # Ignore if namespace doesn't exist
                    raise
            
            # Additional cleanup if needed (e.g., external resources)
            logger.info(f"Completed cleanup for workspace {workspace_id}")
            
        except Exception as e:
            logger.error(f"Error during workspace cleanup: {e}")
            raise
            
    def _create_feature_installation_script(self):
        """Generate a script for installing devcontainer features in pool VMs"""
        return """#!/bin/bash
    echo "No features file found, skipping feature installation"
    exit 0
fi

# Helper function to handle errors
handle_error() {
    echo "Error occurred during feature installation: $1"
    return 1  # Changed to return instead of exit to allow continuation
}

# Helper function to check if a feature exists
feature_exists() {
    jq -e ".$1" "$FEATURES_FILE" > /dev/null 2>&1
}

# Helper function to get feature version/options
get_feature_option() {
    local feature=$1
    local option=$2
    local default=$3
    jq -r ".$feature.$option // \"$default\"" "$FEATURES_FILE" 2>/dev/null || echo "$default"
}

# Ensure PATH includes common binary locations
export PATH="/usr/local/cargo/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Install common dependencies first
apt-get update && apt-get install -y curl wget git build-essential || handle_error "Failed to install base dependencies"

# Rust installation - moved earlier in the process
if feature_exists "rust"; then
    echo "Installing Rust..."
    # Install Rust using rustup
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y || handle_error "Failed to install Rust"
    
    # Source cargo environment
    source "$HOME/.cargo/env"
    
    # Add cargo to PATH permanently
    echo 'source "$HOME/.cargo/env"' >> ~/.bashrc
    echo 'export PATH="$HOME/.cargo/bin:$PATH"' >> ~/.profile
    
    # Install common Rust tools
    rustup component add rust-analyzer || echo "Warning: Failed to install rust-analyzer"
    rustup component add rustfmt || echo "Warning: Failed to install rustfmt"
    rustup component add clippy || echo "Warning: Failed to install clippy"
    
    # Verify Rust installation
    if ! cargo --version > /dev/null 2>&1; then
        handle_error "Rust installation verification failed"
    fi
fi

# Node.js installation
if feature_exists "node"; then
    echo "Installing Node.js..."
    VERSION=$(get_feature_option "node" "version" "lts")
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - || handle_error "Failed to setup Node.js repo"
    apt-get install -y nodejs || handle_error "Failed to install Node.js"
    npm install -g yarn || echo "Warning: Failed to install yarn"
fi

# Python installation
if feature_exists "python"; then
    echo "Installing Python..."
    VERSION=$(get_feature_option "python" "version" "3.10")
    apt-get install -y python3 python3-pip python3-venv || handle_error "Failed to install Python"
    if [ "$(get_feature_option "python" "installJupyter" "false")" = "true" ]; then
        pip3 install jupyter notebook || echo "Warning: Failed to install Jupyter"
    fi
fi

# Go installation
if feature_exists "go"; then
    echo "Installing Go..."
    VERSION=$(get_feature_option "go" "version" "latest")
    if [ "$VERSION" = "latest" ]; then
        VERSION=$(curl -s https://go.dev/VERSION?m=text | head -n1)
    fi
    curl -sSL "https://golang.org/dl/$VERSION.linux-amd64.tar.gz" -o go.tar.gz
    tar -C /usr/local -xzf go.tar.gz
    rm go.tar.gz
    echo 'export PATH=$PATH:/usr/local/go/bin' > /etc/profile.d/go.sh
fi

# Java installation
if feature_exists "java"; then
    echo "Installing Java..."
    VERSION=$(get_feature_option "java" "version" "17")
    apt-get install -y openjdk-${VERSION}-jdk || handle_error "Failed to install Java"
fi

# Rust installation
if feature_exists "rust"; then
    echo "Installing Rust..."
    # Install Rust using rustup
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y || handle_error "Failed to install Rust"
    
    # Source cargo environment
    source "$HOME/.cargo/env"
    
    # Add cargo to PATH permanently
    echo 'source "$HOME/.cargo/env"' >> ~/.bashrc
    echo 'export PATH="$HOME/.cargo/bin:$PATH"' >> ~/.profile
    
    # Install common Rust tools
    rustup component add rust-analyzer || echo "Warning: Failed to install rust-analyzer"
    rustup component add rustfmt || echo "Warning: Failed to install rustfmt"
    rustup component add clippy || echo "Warning: Failed to install clippy"
    
    # Verify Rust installation
    if ! cargo --version > /dev/null 2>&1; then
        handle_error "Rust installation verification failed"
    fi
fi

# Docker feature (if not already installed by post-start script)
if feature_exists "docker" || feature_exists "docker-in-docker"; then
    if ! command -v docker &> /dev/null; then
        echo "Installing Docker..."
        curl -fsSL https://get.docker.com | sh || handle_error "Failed to install Docker"
        usermod -aG docker $USER || echo "Warning: Failed to add user to docker group"
    fi
fi

# Azure CLI
if feature_exists "azure-cli"; then
    echo "Installing Azure CLI..."
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash || handle_error "Failed to install Azure CLI"
fi

# AWS CLI
if feature_exists "aws-cli"; then
    echo "Installing AWS CLI..."
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
    unzip -q awscliv2.zip
    ./aws/install
    rm -rf aws awscliv2.zip
fi

# Kubernetes tools
if feature_exists "kubernetes-tools"; then
    echo "Installing Kubernetes tools..."
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    chmod +x kubectl
    mv kubectl /usr/local/bin/
    
    # Install Helm
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash || echo "Warning: Failed to install Helm"
fi

echo "Feature installation completed successfully"
"""

    