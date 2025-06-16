from flask import Blueprint, jsonify, request
from urllib.parse import unquote
import sys, os
import logging
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auth import token_required

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pool_routes = Blueprint('pool_routes', __name__)
service = None

def configure_routes(pool_service):
    """Configure pool routes with the provided service"""
    global service
    if not pool_service:
        raise ValueError("Pool service cannot be None")
    service = pool_service

    @pool_routes.route('/api/pools', methods=['GET'])
    @token_required
    def list_pools(current_user):
        try:
            logger.info("Getting list of pools")
            check_service()
            pools = service.list_pools()
            
            return jsonify({
                "status": "success",
                "pools": pools if pools else [],
                "message": "Pools retrieved successfully"
            })
            
        except Exception as e:
            logger.error(f"Error in list_pools: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to retrieve pools",
                "pools": []
            }), 500

    @pool_routes.route('/api/pools/<pool_name>/workspaces', methods=['GET'])
    @token_required
    def get_pool_workspaces(current_user, pool_name):
        """Get all workspaces in a pool (available and in-use)"""
        try:
            logger.info(f"Getting workspaces for pool: {pool_name}")
            check_service()
            decoded_name = unquote(pool_name)
            pool = service.get_pool(decoded_name)
            
            if not pool:
                return jsonify({
                    "status": "error",
                    "message": f"Pool '{decoded_name}' not found",
                    "workspaces": []
                }), 404
                
            workspaces = service.get_pool_workspaces(decoded_name)
            # The workspaces are already dictionaries, no need to call to_dict()
            return jsonify({
                "status": "success",
                "workspaces": workspaces if workspaces else [],
                "message": "Workspaces retrieved successfully"
            })
            
        except Exception as e:
            logger.error(f"Error in get_pool_workspaces: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to retrieve workspaces",
                "workspaces": []
            }), 500

    @pool_routes.route('/api/pools', methods=['POST'])
    @token_required
    def create_pool(current_user):
        try:
            check_service()
            pool_data = request.get_json()
            required_fields = ['name', 'minimum_vms', 'repo_name']
            missing_fields = [field for field in required_fields if field not in pool_data]

            if missing_fields:
                return jsonify({"error": f"Missing required fields: {', '.join(missing_fields)}"}), 400

            branch_name = pool_data.get('branch_name', 'main')
            pool = service.create_pool(
                name=pool_data['name'],
                minimum_vms=pool_data['minimum_vms'],
                repo_name=pool_data['repo_name'],
                branch_name=branch_name,
                github_pat=pool_data.get('github_pat')
            )
            return jsonify(pool.to_dict()), 201
        except Exception as e:
            logger.error(f"Error in create_pool: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to create pool"
            }), 500

    @pool_routes.route('/api/pools/<pool_name>', methods=['DELETE'])
    @token_required
    def delete_pool(current_user, pool_name):
        try:
            check_service()
            decoded_name = unquote(pool_name)
            result = service.delete_pool(decoded_name)
            if result:
                return jsonify({"success": True, "message": "Pool deleted successfully", "status": "success"})
            return jsonify({"error": "Failed to delete pool", "status": "error"}), 400
        except Exception as e:
            logger.error(f"Error in delete_pool: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to delete pool"
            }), 500

    @pool_routes.route('/api/pools/<name>/available', methods=['GET'])
    @token_required
    def get_available_workspaces(current_user, name):
        try:
            check_service()
            decoded_name = unquote(name)
            available = service.get_available_workspaces(decoded_name)
            return jsonify({"workspaces": available if available else [], "status": "success"})
        except Exception as e:
            logger.error(f"Error in get_available_workspaces: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to retrieve available workspaces"
            }), 500
    
    @pool_routes.route('/api/pools/<name>/workspaces/<workspace_id>/use', methods=['POST'])
    @token_required
    def mark_workspace_as_used(current_user, name, workspace_id):
        try:
            check_service()
            decoded_name = unquote(name)
            result = service.mark_workspace_as_used(decoded_name, workspace_id)
            if result:
                return jsonify({"success": True, "message": "Workspace marked as used", "status": "success"})
            return jsonify({"error": "Failed to mark workspace as used", "status": "error"}), 400
        except Exception as e:
            logger.error(f"Error in mark_workspace_as_used: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to mark workspace as used"
            }), 500
    
    @pool_routes.route('/api/pools/<name>/workspaces/<workspace_id>/release', methods=['POST'])
    @token_required
    def release_workspace(current_user, name, workspace_id):
        try:
            check_service()
            decoded_name = unquote(name)
            result = service.release_workspace(decoded_name, workspace_id)
            if result:
                return jsonify({"success": True, "message": "Workspace released back to pool", "status": "success"})
            return jsonify({"error": "Failed to release workspace", "status": "error"}), 400
        except Exception as e:
            logger.error(f"Error in release_workspace: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to release workspace"
            }), 500
    
    @pool_routes.route('/api/pools/<pool_name>', methods=['PUT'])
    @token_required
    def update_pool(current_user, pool_name):
        try:
            check_service()
            decoded_name = unquote(pool_name)
            data = request.get_json()
            success = service.update_pool(
                original_name=decoded_name,
                new_name=data.get('name', decoded_name),
                minimum_vms=data.get('minimum_vms'),
                repo_name=data.get('repo_name'),
                branch_name=data.get('branch_name'),
                github_pat=data.get('github_pat')
            )
            if success:
                return jsonify({"success": True, "message": f"Pool '{decoded_name}' updated successfully", "status": "success"})
            return jsonify({"error": "Failed to update pool", "status": "error"}), 400
        except Exception as e:
            logger.error(f"Error in update_pool: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "error": str(e),
                "message": "Failed to update pool"
            }), 500

    def check_service():
        if not service:
            logger.error("Pool service not configured")
            raise RuntimeError("Pool service not configured")
        
    def check_service():
        """Verify that the pool service is configured"""
        global service
        if not service:
            logger.error("Pool service not configured")
            raise RuntimeError("Pool service not configured. Please ensure service is properly initialized.")

    return pool_routes
