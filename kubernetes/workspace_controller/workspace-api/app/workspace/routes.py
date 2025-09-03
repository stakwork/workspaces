import logging
from flask import Blueprint, request, jsonify
from app.auth.decorators import token_required
from app.workspace.service import workspace_service
from app.utils.image_cache import image_cache_service

logger = logging.getLogger(__name__)
workspace_bp = Blueprint('workspace', __name__)


@workspace_bp.route('', methods=['GET'])
@token_required
def list_workspaces(current_user):
    """List all workspaces"""
    try:
        workspaces = workspace_service.list_workspaces()
        return jsonify({"workspaces": workspaces})
    except Exception as e:
        logger.error(f"Error in list_workspaces: {e}")
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('', methods=['POST'])
@token_required
def create_workspace(current_user):
    """Create a new workspace"""
    try:
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        result = workspace_service.create_workspace(request.json)
        return jsonify(result)
        
    except ValueError as e:
        # Handle validation errors
        logger.warning(f"Validation error in create_workspace: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in create_workspace: {e}")
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/<workspace_id>', methods=['GET'])
@token_required
def get_workspace(current_user, workspace_id):
    """Get details for a specific workspace"""
    try:
        include_password = request.args.get("includePassword") == "true"
        workspace_info = workspace_service.get_workspace(workspace_id, include_password)
        return jsonify(workspace_info)
    except Exception as e:
        logger.error(f"Error in get_workspace: {e}")
        if "not found" in str(e).lower():
            return jsonify({"error": str(e)}), 404
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/<workspace_id>/delete', methods=['DELETE'])
@token_required
def delete_workspace(current_user, workspace_id):
    """Delete a workspace"""
    try:
        result = workspace_service.delete_workspace(workspace_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in delete_workspace: {e}")
        if "not found" in str(e).lower():
            return jsonify({"error": str(e)}), 404
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/<workspace_id>/stop', methods=['POST'])
@token_required
def stop_workspace(current_user, workspace_id):
    """Stop a workspace by scaling it to 0 replicas"""
    try:
        result = workspace_service.stop_workspace(workspace_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in stop_workspace: {e}")
        if "not found" in str(e).lower():
            return jsonify({"error": str(e)}), 404
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/<workspace_id>/start', methods=['POST'])
@token_required
def start_workspace(current_user, workspace_id):
    """Start a workspace by scaling it to 1 replica"""
    try:
        result = workspace_service.start_workspace(workspace_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in start_workspace: {e}")
        if "not found" in str(e).lower():
            return jsonify({"error": str(e)}), 404
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/<workspace_id>/logs', methods=['GET'])
@token_required
def get_workspace_logs(current_user, workspace_id):
    """Get logs for a workspace"""
    try:
        # Find the namespace for this workspace
        from app.config import app_config
        namespaces = app_config.core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
        
        if not namespaces.items:
            return jsonify({"error": "Workspace not found"}), 404
            
        namespace_name = namespaces.items[0].metadata.name
        
        # Get logs from the code-server pod
        pods = app_config.core_v1.list_namespaced_pod(
            namespace_name, 
            label_selector="app=code-server"
        )
        
        if not pods.items:
            return jsonify({"error": "No pods found for workspace"}), 404
        
        pod_name = pods.items[0].metadata.name
        
        # Get logs with optional parameters
        lines = request.args.get('lines', 100, type=int)
        follow = request.args.get('follow', 'false').lower() == 'true'
        container = request.args.get('container', None)
        
        try:
            logs = app_config.core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace_name,
                container=container,
                tail_lines=lines,
                follow=follow
            )
            
            return jsonify({
                "success": True,
                "logs": logs,
                "pod": pod_name,
                "namespace": namespace_name
            })
        except Exception as e:
            logger.error(f"Error getting pod logs: {e}")
            return jsonify({"error": f"Failed to get logs: {str(e)}"}), 500
            
    except Exception as e:
        logger.error(f"Error in get_workspace_logs: {e}")
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/<workspace_id>/status', methods=['GET'])
@token_required
def get_workspace_status(current_user, workspace_id):
    """Get detailed status for a workspace"""
    try:
        # Find the namespace for this workspace
        from app.config import app_config
        namespaces = app_config.core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
        
        if not namespaces.items:
            return jsonify({"error": "Workspace not found"}), 404
            
        namespace_name = namespaces.items[0].metadata.name
        
        # Get deployment status
        deployments = app_config.apps_v1.list_namespaced_deployment(
            namespace_name, 
            label_selector="app=workspace"
        )
        
        deployment_status = None
        if deployments.items:
            dep = deployments.items[0]
            deployment_status = {
                "name": dep.metadata.name,
                "replicas": dep.spec.replicas,
                "ready_replicas": dep.status.ready_replicas or 0,
                "available_replicas": dep.status.available_replicas or 0,
                "conditions": []
            }
            
            if dep.status.conditions:
                deployment_status["conditions"] = [
                    {
                        "type": condition.type,
                        "status": condition.status,
                        "reason": condition.reason,
                        "message": condition.message
                    }
                    for condition in dep.status.conditions
                ]
        
        # Get pod status
        pods = app_config.core_v1.list_namespaced_pod(
            namespace_name, 
            label_selector="app=code-server"
        )
        
        pod_statuses = []
        for pod in pods.items:
            container_statuses = []
            if pod.status.container_statuses:
                container_statuses = [
                    {
                        "name": container.name,
                        "ready": container.ready,
                        "restart_count": container.restart_count,
                        "state": str(container.state)
                    }
                    for container in pod.status.container_statuses
                ]
            
            pod_statuses.append({
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "conditions": [
                    {
                        "type": condition.type,
                        "status": condition.status,
                        "reason": condition.reason
                    }
                    for condition in (pod.status.conditions or [])
                ],
                "containers": container_statuses
            })
        
        # Get service status
        services = app_config.core_v1.list_namespaced_service(
            namespace_name,
            label_selector="app=workspace"
        )
        
        service_status = None
        if services.items:
            svc = services.items[0]
            service_status = {
                "name": svc.metadata.name,
                "type": svc.spec.type,
                "ports": [
                    {
                        "port": port.port,
                        "target_port": port.target_port,
                        "protocol": port.protocol
                    }
                    for port in (svc.spec.ports or [])
                ]
            }
        
        return jsonify({
            "success": True,
            "workspace_id": workspace_id,
            "namespace": namespace_name,
            "deployment": deployment_status,
            "pods": pod_statuses,
            "service": service_status
        })
        
    except Exception as e:
        logger.error(f"Error in get_workspace_status: {e}")
        return jsonify({"error": str(e)}), 500

@workspace_bp.route('/capacity', methods=['GET'])
@token_required
def get_cluster_capacity(current_user):
    """Get cluster capacity and workspace limits"""
    try:
        capacity_info = workspace_service.get_cluster_capacity()
        return jsonify(capacity_info)
    except Exception as e:
        logger.error(f"Error in get_cluster_capacity: {e}")
        return jsonify({"error": str(e)}), 500

@workspace_bp.route('/<workspace_id>/restart', methods=['POST'])
@token_required
def restart_workspace(current_user, workspace_id):
    """Restart a workspace by recreating its pods"""
    try:
        # Find the namespace for this workspace
        from app.config import app_config
        namespaces = app_config.core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
        
        if not namespaces.items:
            return jsonify({"error": "Workspace not found"}), 404
            
        namespace_name = namespaces.items[0].metadata.name
        
        # Restart by updating the deployment with a new annotation
        import time
        restart_annotation = f"kubectl.kubernetes.io/restartedAt-{int(time.time())}"
        
        # Patch the deployment to trigger a restart
        app_config.apps_v1.patch_namespaced_deployment(
            name="code-server",
            namespace=namespace_name,
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                restart_annotation: str(int(time.time()))
                            }
                        }
                    }
                }
            }
        )
        
        return jsonify({
            "success": True,
            "message": f"Workspace {workspace_id} restart initiated"
        })
        
    except Exception as e:
        logger.error(f"Error in restart_workspace: {e}")
        if "not found" in str(e).lower():
            return jsonify({"error": str(e)}), 404
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/cache', methods=['GET'])
@token_required
def get_image_cache(current_user):
    """Get all cached image information"""
    try:
        cache_data = image_cache_service.list_cached_images()
        
        # Add statistics
        total_entries = len(cache_data)
        cache_stats = {
            "total_cached_images": total_entries,
            "cache_entries": cache_data
        }
        
        return jsonify(cache_stats)
        
    except Exception as e:
        logger.error(f"Error in get_image_cache: {e}")
        return jsonify({"error": str(e)}), 500


@workspace_bp.route('/cache', methods=['DELETE'])
@token_required
def clear_image_cache(current_user):
    """Clear all cached images"""
    try:
        success = image_cache_service.clear_cache()
        
        if success:
            return jsonify({
                "success": True,
                "message": "Image cache cleared successfully"
            })
        else:
            return jsonify({
                "success": False,
                "message": "Failed to clear image cache"
            }), 500
            
    except Exception as e:
        logger.error(f"Error in clear_image_cache: {e}")
        return jsonify({"error": str(e)}), 500