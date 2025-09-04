import json
import logging
from datetime import datetime
from app.config import app_config
from app.utils.generators import generate_workspace_identifiers, extract_workspace_config
from app.workspace import k8s_resources

logger = logging.getLogger(__name__)


class WorkspaceService:
    """Service class for workspace operations"""
    
    def __init__(self):
        self.core_v1 = app_config.core_v1
        self.apps_v1 = app_config.apps_v1
        self.networking_v1 = app_config.networking_v1
        self.batch_v1 = app_config.batch_v1
    
    def list_workspaces(self):
        """List all workspaces"""
        workspaces = []
        
        try:
            # Get all namespaces with the workspace label
            namespaces = self.core_v1.list_namespace(label_selector="app=workspace")
            
            for ns in namespaces.items:
                try:
                    # Get workspace info from config map
                    config_maps = self.core_v1.list_namespaced_config_map(
                        ns.metadata.name, 
                        label_selector="app=workspace-info"
                    )
                    if not config_maps.items:
                        continue
                        
                    workspace_info = json.loads(config_maps.items[0].data.get("info", "{}"))
                    
                    # Don't expose password
                    if "password" in workspace_info:
                        workspace_info["password"] = "********"
                        
                    # Get pods to determine state
                    pods = self.core_v1.list_namespaced_pod(
                        ns.metadata.name, 
                        label_selector="app=code-server"
                    )
                    if pods.items:
                        if pods.items[0].status.phase == "Running":
                            workspace_info["state"] = "running"
                        else:
                            workspace_info["state"] = pods.items[0].status.phase.lower()
                    else:
                        workspace_info["state"] = "unknown"
                        
                    workspaces.append(workspace_info)
                except Exception as e:
                    logger.error(f"Error getting workspace info from namespace {ns.metadata.name}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error listing workspaces: {e}")
            raise Exception(f"Failed to list workspaces: {str(e)}")
            
        return workspaces
    
    def create_workspace(self, request_data):
        """Create a new workspace"""
        try:
            # Extract and validate request data
            workspace_config = extract_workspace_config(request_data)
            
            # Generate workspace identifiers
            workspace_ids = generate_workspace_identifiers(app_config.WORKSPACE_DOMAIN)
            
            # Create all Kubernetes resources
            self._create_workspace_resources(workspace_ids, workspace_config)
            
            # Get workspace info for response
            workspace_info = self._get_workspace_info(workspace_ids, workspace_config)
            
            return {
                "success": True,
                "message": "Workspace creation initiated",
                "workspace": workspace_info
            }
            
        except Exception as e:
            logger.error(f"Error creating workspace: {e}")
            # Try to clean up if something went wrong
            try:
                if 'workspace_ids' in locals():
                    self.core_v1.delete_namespace(workspace_ids['namespace_name'])
            except:
                pass
            raise Exception(f"Failed to create workspace: {str(e)}")
    
    def get_workspace(self, workspace_id, include_password=False):
        """Get details for a specific workspace"""
        try:
            # Find the namespace for this workspace
            namespaces = self.core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
            
            if not namespaces.items:
                raise Exception("Workspace not found")
                
            namespace_name = namespaces.items[0].metadata.name
            
            # Get workspace info from config map
            config_maps = self.core_v1.list_namespaced_config_map(
                namespace_name, 
                label_selector="app=workspace-info"
            )
            if not config_maps.items:
                raise Exception("Workspace info not found")
                
            workspace_info = json.loads(config_maps.items[0].data.get("info", "{}"))
            
            # Don't expose password unless explicitly requested
            if "password" in workspace_info and not include_password:
                workspace_info["password"] = "********"
            
            # Get pods to determine state
            pods = self.core_v1.list_namespaced_pod(
                namespace_name, 
                label_selector="app=code-server"
            )
            if pods.items:
                if pods.items[0].status.phase == "Running":
                    workspace_info["state"] = "running"
                else:
                    workspace_info["state"] = pods.items[0].status.phase.lower()
            else:
                workspace_info["state"] = "unknown"
            
            return workspace_info
        except Exception as e:
            logger.error(f"Error getting workspace: {e}")
            raise Exception(f"Failed to get workspace: {str(e)}")
    
    def delete_workspace(self, workspace_id):
        """Delete a workspace"""
        try:
            # Find the namespace for this workspace
            namespaces = self.core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
            
            if not namespaces.items:
                raise Exception("Workspace not found")
                
            namespace_name = namespaces.items[0].metadata.name
            
            # Delete the namespace (this will delete all resources in it)
            self.core_v1.delete_namespace(namespace_name)
            
            return {
                "success": True,
                "message": f"Workspace {workspace_id} deleted"
            }
        except Exception as e:
            logger.error(f"Error deleting workspace: {e}")
            raise Exception(f"Failed to delete workspace: {str(e)}")
    
    def stop_workspace(self, workspace_id):
        """Stop a workspace by scaling it to 0 replicas"""
        try:
            # Find the namespace for this workspace
            namespaces = self.core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
            
            if not namespaces.items:
                raise Exception("Workspace not found")
                
            namespace_name = namespaces.items[0].metadata.name
            
            # Scale the deployment to 0
            self.apps_v1.patch_namespaced_deployment_scale(
                name="code-server",
                namespace=namespace_name,
                body={"spec": {"replicas": 0}}
            )
            
            return {
                "success": True,
                "message": f"Workspace {workspace_id} stopped"
            }
        except Exception as e:
            logger.error(f"Error stopping workspace: {e}")
            raise Exception(f"Failed to stop workspace: {str(e)}")
    
    def start_workspace(self, workspace_id):
        """Start a workspace by scaling it to 1 replica"""
        try:
            # Find the namespace for this workspace
            namespaces = self.core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
            
            if not namespaces.items:
                raise Exception("Workspace not found")
                
            namespace_name = namespaces.items[0].metadata.name
            
            # Scale the deployment to 1
            self.apps_v1.patch_namespaced_deployment_scale(
                name="code-server",
                namespace=namespace_name,
                body={"spec": {"replicas": 1}}
            )
            
            return {
                "success": True,
                "message": f"Workspace {workspace_id} started"
            }
        except Exception as e:
            logger.error(f"Error starting workspace: {e}")
            raise Exception(f"Failed to start workspace: {str(e)}")
    
    def _create_workspace_resources(self, workspace_ids, workspace_config):
        """Create all Kubernetes resources for the workspace"""
        # Create the namespace
        k8s_resources.create_namespace(workspace_ids)
        
        # Create storage and credentials
        # k8s_resources.create_persistent_volume_claim(workspace_ids)  # Using EmptyDir instead
        k8s_resources.create_workspace_secret(workspace_ids, workspace_config)
        
        # Create initialization scripts
        k8s_resources.create_init_script_configmap(workspace_ids, workspace_config)
        k8s_resources.create_workspace_info_configmap(workspace_ids, workspace_config)

        # Copy required ConfigMaps and Secrets
        k8s_resources.copy_port_detector_configmap(workspace_ids)
        k8s_resources.copy_wildcard_certificate(workspace_ids)
        k8s_resources.copy_dockerhub_secret(workspace_ids)
        
        # Create Kubernetes resources
        k8s_resources.create_deployment(workspace_ids, workspace_config)
        k8s_resources.create_service(workspace_ids)
        k8s_resources.create_ingress(workspace_ids)

        k8s_resources.create_warmer_job(workspace_ids)
    
    def _get_workspace_info(self, workspace_ids, workspace_config):
        """Create the workspace information dictionary"""
        workspace_info = {
            "id": workspace_ids['workspace_id'],
            "repositories": workspace_config['github_urls'],
            "branches": workspace_config['github_branches'],
            "primaryRepo": workspace_config['primary_repo_url'],
            "repoName": workspace_config['repo_name'],
            "subdomain": workspace_ids['subdomain'],
            "fqdn": workspace_ids['fqdn'],
            "url": f"https://{workspace_ids['fqdn']}",
            "password": workspace_ids['password'],
            "created": datetime.now().isoformat()
        }

        if workspace_config['use_custom_image_url']:
            workspace_info["imageUrl"] = workspace_config['custom_image_url']
            workspace_info["customImage"] = True
        else:
            workspace_info["image"] = workspace_config['custom_image']
            workspace_info["customImage"] = False
            workspace_info["useDevContainer"] = workspace_config['use_dev_container']
            
        return workspace_info
    
    def get_cluster_capacity(self):
        """Get cluster capacity with comprehensive scheduling constraints"""
        try:
            from kubernetes import client
            
            # Get node information
            nodes = self.core_v1.list_node()
            
            total_allocatable_cpu = 0
            total_allocatable_memory = 0
            node_details = []
            
            for node in nodes.items:
                # Skip if node is not ready or has taints that prevent scheduling
                is_ready = any(condition.type == "Ready" and condition.status == "True" 
                            for condition in node.status.conditions)
                
                if not is_ready:
                    continue
                
                # Check for taints that prevent scheduling
                has_no_schedule_taint = False
                if node.spec.taints:
                    for taint in node.spec.taints:
                        if taint.effect in ["NoSchedule", "NoExecute"]:
                            has_no_schedule_taint = True
                            break
                
                if has_no_schedule_taint:
                    logger.info(f"Node {node.metadata.name} has NoSchedule/NoExecute taint, skipping")
                    continue
                    
                # Get allocatable resources
                if node.status.allocatable:
                    cpu_str = node.status.allocatable.get('cpu', '0')
                    memory_str = node.status.allocatable.get('memory', '0')
                    
                    if cpu_str.endswith('m'):
                        cpu_cores = float(cpu_str[:-1]) / 1000
                    else:
                        cpu_cores = float(cpu_str)
                        
                    memory_bytes = self._parse_memory(memory_str)
                    
                    total_allocatable_cpu += cpu_cores
                    total_allocatable_memory += memory_bytes
                    
                    node_details.append({
                        'name': node.metadata.name,
                        'allocatable_cpu': cpu_cores,
                        'allocatable_memory': memory_bytes
                    })
            
            # Get actual usage from metrics API
            try:
                custom_api = client.CustomObjectsApi()
                
                # Get node metrics
                node_metrics = custom_api.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="nodes"
                )
                
                # Map usage to nodes
                node_usage = {}
                for node_metric in node_metrics.get('items', []):
                    node_name = node_metric.get('metadata', {}).get('name', '')
                    usage = node_metric.get('usage', {})
                    
                    cpu_usage = usage.get('cpu', '0')
                    memory_usage = usage.get('memory', '0')
                    
                    # Parse CPU usage
                    if cpu_usage.endswith('n'):
                        cpu_cores = float(cpu_usage[:-1]) / 1_000_000_000
                    elif cpu_usage.endswith('m'):
                        cpu_cores = float(cpu_usage[:-1]) / 1000
                    else:
                        cpu_cores = float(cpu_usage)
                    
                    memory_bytes = self._parse_memory(memory_usage)
                    
                    node_usage[node_name] = {
                        'cpu': cpu_cores,
                        'memory': memory_bytes
                    }
                
            except Exception as metrics_error:
                logger.error(f"Failed to get metrics: {metrics_error}")
                raise Exception(f"Metrics API error: {metrics_error}")
            
            # Calculate per-node available capacity and see if any node can fit a new workspace
            workspace_cpu_requirement = 2.0  # 2 CPU cores
            workspace_memory_requirement = 8 * 1024 * 1024 * 1024  # 8GB
            
            nodes_that_can_fit_workspace = 0
            total_used_cpu = 0
            total_used_memory = 0
            
            logger.info("Per-node capacity analysis:")
            
            for node_detail in node_details:
                node_name = node_detail['name']
                allocatable_cpu = node_detail['allocatable_cpu']
                allocatable_memory = node_detail['allocatable_memory']
                
                # Get usage for this node
                usage = node_usage.get(node_name, {'cpu': 0, 'memory': 0})
                used_cpu = usage['cpu']
                used_memory = usage['memory']
                
                total_used_cpu += used_cpu
                total_used_memory += used_memory
                
                # Calculate available on this specific node
                # Use a more aggressive buffer per node (20% instead of 10%)
                node_buffer_cpu = allocatable_cpu * 0.2
                node_buffer_memory = allocatable_memory * 0.2
                
                available_cpu = allocatable_cpu - used_cpu - node_buffer_cpu
                available_memory = allocatable_memory - used_memory - node_buffer_memory
                
                can_fit_workspace = (available_cpu >= workspace_cpu_requirement and 
                                available_memory >= workspace_memory_requirement)
                
                if can_fit_workspace:
                    nodes_that_can_fit_workspace += 1
                
                logger.info(f"  {node_name}:")
                logger.info(f"    Allocatable: {allocatable_cpu:.1f} CPU, {allocatable_memory/(1024**3):.1f}GB")
                logger.info(f"    Used: {used_cpu:.1f} CPU, {used_memory/(1024**3):.1f}GB")
                logger.info(f"    Available: {available_cpu:.1f} CPU, {available_memory/(1024**3):.1f}GB")
                logger.info(f"    Can fit workspace: {can_fit_workspace}")
            
            # Also check for resource quotas and limit ranges that might block scheduling
            resource_constraints = []
            
            try:
                # Check if there are any resource quotas that might be limiting
                all_namespaces = self.core_v1.list_namespace()
                for ns in all_namespaces.items:
                    try:
                        quotas = self.core_v1.list_namespaced_resource_quota(ns.metadata.name)
                        if quotas.items:
                            resource_constraints.append(f"ResourceQuotas in {ns.metadata.name}")
                            
                        limit_ranges = self.core_v1.list_namespaced_limit_range(ns.metadata.name)
                        if limit_ranges.items:
                            resource_constraints.append(f"LimitRanges in {ns.metadata.name}")
                    except:
                        pass
            except Exception as e:
                logger.warning(f"Could not check resource constraints: {e}")
            
            # Check for pod disruption budgets
            try:
                policy_v1 = client.PolicyV1Api()
                pdbs = policy_v1.list_pod_disruption_budget_for_all_namespaces()
                if pdbs.items:
                    resource_constraints.append(f"PodDisruptionBudgets ({len(pdbs.items)} found)")
            except:
                pass
            
            # Count current workspaces
            current_workspaces = 0
            try:
                workspace_namespaces = self.core_v1.list_namespace(label_selector="app=workspace")
                for ns in workspace_namespaces.items:
                    try:
                        pods = self.core_v1.list_namespaced_pod(
                            ns.metadata.name, 
                            label_selector="app=code-server"
                        )
                        for pod in pods.items:
                            if pod.status.phase == "Running":
                                current_workspaces += 1
                    except Exception as e:
                        logger.warning(f"Error counting workspaces in {ns.metadata.name}: {e}")
            except Exception as e:
                logger.warning(f"Error listing workspace namespaces: {e}")
            
            # The real capacity is limited by how many nodes can actually fit a workspace
            # Not just the total cluster resources
            additional_capacity = nodes_that_can_fit_workspace
            
            # Conservative total available calculation
            conservative_available_cpu = total_allocatable_cpu - total_used_cpu - (total_allocatable_cpu * 0.2)
            conservative_available_memory = total_allocatable_memory - total_used_memory - (total_allocatable_memory * 0.2)
            
            result = {
                "cluster_resources": {
                    "total_cpu_cores": round(total_allocatable_cpu, 2),
                    "total_memory_gb": round(total_allocatable_memory / (1024**3), 2),
                    "used_cpu_cores": round(total_used_cpu, 2),
                    "used_memory_gb": round(total_used_memory / (1024**3), 2),
                    "available_cpu_cores": round(conservative_available_cpu, 2),
                    "available_memory_gb": round(conservative_available_memory / (1024**3), 2)
                },
                "workspace_capacity": {
                    "current_workspaces": current_workspaces,
                    "max_additional_workspaces": additional_capacity,
                    "limited_by": "node_capacity",
                    "nodes_that_can_fit_workspace": nodes_that_can_fit_workspace
                },
                "per_workspace_requirements": {
                    "cpu_cores": 2,
                    "memory_gb": 8
                },
                "scheduling_constraints": resource_constraints,
                "node_count": len(node_details)
            }
            
            logger.info(f"Final capacity assessment: {additional_capacity} additional workspaces possible")
            logger.info(f"Scheduling constraints found: {resource_constraints}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting cluster capacity: {e}")
            raise Exception(f"Failed to get cluster capacity: {str(e)}")    

    def _parse_memory(self, memory_str):
        """Parse Kubernetes memory string (e.g., '7901Mi', '8Gi') to bytes"""
        if not memory_str:
            return 0
            
        memory_str = memory_str.strip()
        
        # Handle different units
        if memory_str.endswith('Ki'):
            return int(memory_str[:-2]) * 1024
        elif memory_str.endswith('Mi'):
            return int(memory_str[:-2]) * 1024 * 1024
        elif memory_str.endswith('Gi'):
            return int(memory_str[:-2]) * 1024 * 1024 * 1024
        elif memory_str.endswith('Ti'):
            return int(memory_str[:-2]) * 1024 * 1024 * 1024 * 1024
        elif memory_str.endswith('m'):
            return int(memory_str[:-1]) / 1000  # millibytes
        else:
            # Assume bytes
            return int(memory_str)


# Global service instance
workspace_service = WorkspaceService()