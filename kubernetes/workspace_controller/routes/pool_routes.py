from flask import Blueprint, jsonify, request
from urllib.parse import unquote

pool_routes = Blueprint('pool_routes', __name__)
service = None

def configure_routes(pool_service):
    """Configure pool routes with the provided service"""
    global service
    service = pool_service

    @pool_routes.route('/api/pools/<pool_name>/workspaces', methods=['GET'])
    def get_pool_workspaces(pool_name):
        """Get all workspaces in a pool (available and in-use)"""
        try:
            decoded_name = unquote(pool_name)
            pool = service.get_pool(decoded_name)
            if not pool:
                return jsonify({"error": f"Pool '{decoded_name}' not found"}), 404
                
            workspaces = service.get_pool_workspaces(decoded_name)
            return jsonify({"workspaces": workspaces})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @pool_routes.route('/api/pools', methods=['GET'])
    def list_pools():
        try:
            pools = service.list_pools()
            return jsonify({"pools": pools})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @pool_routes.route('/api/pools', methods=['POST'])
    def create_pool():
        try:
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
            return jsonify(pool.to_dict())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @pool_routes.route('/api/pools/<pool_name>', methods=['DELETE'])
    def delete_pool(pool_name):
        try:
            decoded_name = unquote(pool_name)
            result = service.delete_pool(decoded_name)
            if result:
                return jsonify({"success": True, "message": "Pool deleted successfully"})
            return jsonify({"error": "Failed to delete pool"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @pool_routes.route('/api/pools/<name>/available', methods=['GET'])
    def get_available_workspaces(name):
        try:
            decoded_name = unquote(name)
            available = service.get_available_workspaces(decoded_name)
            return jsonify({"workspaces": available})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @pool_routes.route('/api/pools/<name>/workspaces/<workspace_id>/use', methods=['POST'])
    def mark_workspace_as_used(name, workspace_id):
        try:
            decoded_name = unquote(name)
            result = service.mark_workspace_as_used(decoded_name, workspace_id)
            if result:
                return jsonify({"success": True, "message": "Workspace marked as used"})
            return jsonify({"error": "Failed to mark workspace as used"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @pool_routes.route('/api/pools/<name>/workspaces/<workspace_id>/release', methods=['POST'])
    def release_workspace(name, workspace_id):
        try:
            decoded_name = unquote(name)
            result = service.release_workspace(decoded_name, workspace_id)
            if result:
                return jsonify({"success": True, "message": "Workspace released back to pool"})
            return jsonify({"error": "Failed to release workspace"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @pool_routes.route('/api/pools/<pool_name>', methods=['PUT'])
    def update_pool(pool_name):
        try:
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
                return jsonify({"success": True, "message": f"Pool '{decoded_name}' updated successfully"})
            return jsonify({"error": "Failed to update pool"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return pool_routes
