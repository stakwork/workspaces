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
        k8s_resources.create_persistent_volume_claim(workspace_ids)
        k8s_resources.create_workspace_secret(workspace_ids, workspace_config.get('github_token'), workspace_config.get('github_username'))
        
        # Create initialization scripts
        k8s_resources.create_init_script_configmap(workspace_ids, workspace_config)
        k8s_resources.create_workspace_info_configmap(workspace_ids, workspace_config)

        # Copy required ConfigMaps and Secrets
        k8s_resources.copy_port_detector_configmap(workspace_ids)
        k8s_resources.copy_wildcard_certificate(workspace_ids)
        
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


# Global service instance
workspace_service = WorkspaceService()