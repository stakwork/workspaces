import logging
from flask import Blueprint, request, jsonify
from app.auth.decorators import token_required, admin_required
from app.pool.service import pool_service
from urllib.parse import unquote  # Add this import

logger = logging.getLogger(__name__)
pool_bp = Blueprint('pool', __name__)


@pool_bp.route('', methods=['GET'])
@token_required
def list_pools(current_user):
    """List all pools"""
    try:
        is_admin = current_user.get('role') == 'admin'
        
        if is_admin:
            # Admin can see all pools
            pools = pool_service.list_pools()
        else:
            # Regular user sees only their pools
            pools = pool_service.get_user_pools(current_user['username'])

        return jsonify({"pools": pools})
    except Exception as e:
        logger.error(f"Error in list_pools: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('', methods=['POST'])
@token_required
def create_pool(current_user):
    """Create a new pool"""
    try:
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        data = request.json
        is_admin = current_user.get('role') == 'admin'
        username = current_user['username']
        
        # Validate required fields
        required_fields = ['pool_name', 'minimum_vms', 'repo_name', 'branch_name', 'github_pat']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Validate data types
        if not isinstance(data['minimum_vms'], int) or data['minimum_vms'] < 1:
            return jsonify({"error": "minimum_vms must be a positive integer"}), 400
        
        # Validate environment variables format
        env_vars = data.get('env_vars', [])
        if env_vars and not isinstance(env_vars, list):
            return jsonify({"error": "env_vars must be a list"}), 400
        
        for env_var in env_vars:
            if not isinstance(env_var, dict) or 'name' not in env_var or 'value' not in env_var:
                return jsonify({"error": "Each env_var must have 'name' and 'value' fields"}), 400

        owner_username = data.get('owner_username', username)
        if owner_username != username and not is_admin:
            return jsonify({"error": "Only admins can create pools for other users"}), 403

        container_files = data.get('container_files', {})

        result = pool_service.create_pool(
            pool_name=data['pool_name'],
            minimum_vms=data['minimum_vms'],
            repo_name=data['repo_name'],
            branch_name=data['branch_name'],
            github_pat=data['github_pat'],
            github_username=data['github_username'],
            env_vars=env_vars,
            owner_username=owner_username,
            devcontainer_json=container_files.get('devcontainer.json'),
            dockerfile=container_files.get('Dockerfile'),
            docker_compose_yml=container_files.get('docker-compose.yml'),
            pm2_config_js=container_files.get('pm2.config.js'),
            cpu=container_files.get('poolCpu'),
            memory=container_files.get('poolMemory')
        )
        
        return jsonify(result), 201
        
    except ValueError as e:
        logger.warning(f"Validation error in create_pool: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in create_pool: {e}")
        return jsonify({"error": str(e)}), 500

@pool_bp.route('/<pool_name>', methods=['PUT'])
@token_required
def update_pool(current_user, pool_name):
    """Update pool configuration"""
    try:
        pool_name = unquote(pool_name)
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        data = request.json
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'

        
        # Validate environment variables format if provided
        if 'env_vars' in data:
            env_vars = data['env_vars']
            if env_vars and not isinstance(env_vars, list):
                return jsonify({"error": "env_vars must be a list"}), 400
            
            for env_var in env_vars:
                if not isinstance(env_var, dict) or 'name' not in env_var or 'value' not in env_var:
                    return jsonify({"error": "Each env_var must have 'name' and 'value' fields"}), 400
        
        if 'github_pat' in data:
            github_pat = data['github_pat']
            if isinstance(github_pat, dict):
                if 'value' not in github_pat:
                    return jsonify({"error": "github_pat object must have 'value' field"}), 400
            elif not isinstance(github_pat, str):
                return jsonify({"error": "github_pat must be a string or object with 'value' field"}), 400


        requesting_user = None if is_admin else username
        result = pool_service.update_pool(pool_name, data, requesting_user=requesting_user)
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Validation error in update_pool: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in update_pool: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>', methods=['GET'])
@token_required
def get_pool(current_user, pool_name):
    """Get details for a specific pool"""
    try:
        pool_name = unquote(pool_name)
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'

        requesting_user = None if is_admin else username
        pool_info = pool_service.get_pool(pool_name, requesting_user=requesting_user)

        return jsonify(pool_info)
    except ValueError as e:
        logger.warning(f"Pool not found: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in get_pool: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>', methods=['DELETE'])
@token_required
def delete_pool(current_user, pool_name):
    """Delete a pool"""
    try:
        pool_name = unquote(pool_name)

        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        # Admin can delete any pool, regular users only their own
        requesting_user = None if is_admin else username
        result = pool_service.delete_pool(pool_name, requesting_user=requesting_user)

        return jsonify(result)
    except ValueError as e:
        logger.warning(f"Pool not found: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in delete_pool: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>/scale', methods=['POST'])
@token_required
def scale_pool(current_user, pool_name):
    """Update the minimum VMs for a pool"""
    try:
        pool_name = unquote(pool_name)
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        data = request.json
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'

        
        if 'minimum_vms' not in data:
            return jsonify({"error": "Missing required field: minimum_vms"}), 400
        
        if not isinstance(data['minimum_vms'], int) or data['minimum_vms'] < 1:
            return jsonify({"error": "minimum_vms must be a positive integer"}), 400
        
        requesting_user = None if is_admin else username
        result = pool_service.scale_pool(pool_name, data['minimum_vms'], requesting_user=requesting_user)
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Validation error in scale_pool: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in scale_pool: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>/workspace', methods=['GET'])
@token_required
def get_available_workspace(current_user, pool_name):
    """Get an available workspace from the pool"""
    try:
        pool_name = unquote(pool_name)
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        # Admin can access any pool, regular users only their own
        requesting_user = None if is_admin else username
        workspace = pool_service.get_available_workspace(pool_name, requesting_user=requesting_user)
        
        if workspace:
            return jsonify({
                "success": True,
                "workspace": workspace
            })
        else:
            return jsonify({
                "success": False,
                "message": "No available workspace in pool",
                "workspace": None
            })
            
    except ValueError as e:
        logger.warning(f"Pool not found: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in get_available_workspace: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>/status', methods=['GET'])
@token_required
def get_pool_status(current_user, pool_name):
    """Get detailed status for a pool"""
    try:
        pool_name = unquote(pool_name)
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        # Admin can access any pool, regular users only their own
        requesting_user = None if is_admin else username
        pool_info = pool_service.get_pool(pool_name, requesting_user=requesting_user)
        return jsonify(pool_info['status'])
    except ValueError as e:
        logger.warning(f"Pool not found: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in get_pool_status: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>/workspaces', methods=['GET'])
@token_required
def list_pool_workspaces(current_user, pool_name):
    """List all workspaces in a pool"""
    try:
        pool_name = unquote(pool_name)
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        # Admin can access any pool, regular users only their own
        requesting_user = None if is_admin else username
        result = pool_service.get_pool_workspaces(pool_name, requesting_user=requesting_user)
        return jsonify({
            "pool_name": pool_name,
            "workspaces": result['workspaces']
        })
    except ValueError as e:
        logger.warning(f"Pool not found: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in list_pool_workspaces: {e}")
        return jsonify({"error": str(e)}), 500

@pool_bp.route('/<pool_name>/workspaces/<workspace_id>/mark-used', methods=['POST'])
@token_required
def mark_workspace_used(current_user, pool_name, workspace_id):
    """Mark a workspace as used"""
    try:
        pool_name = unquote(pool_name)
        data = request.json or {}
        
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        data = request.json or {}
        user_info = data.get('user_info', username)
        
        # Admin can access any pool, regular users only their own
        requesting_user = None if is_admin else username
        result = pool_service.mark_workspace_as_used(
            pool_name, workspace_id, requesting_user=requesting_user, user_info=user_info
        )
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Validation error in mark_workspace_used: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in mark_workspace_used: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>/workspaces/<workspace_id>/mark-unused', methods=['POST'])
@token_required
def mark_workspace_unused(current_user, pool_name, workspace_id):
    """Mark a workspace as unused"""
    try:
        pool_name = unquote(pool_name)
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        # Admin can access any pool, regular users only their own
        requesting_user = None if is_admin else username
        result = pool_service.mark_workspace_as_unused(
            pool_name, workspace_id, requesting_user=requesting_user
        )
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Validation error in mark_workspace_unused: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in mark_workspace_unused: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>/workspaces/<workspace_id>/usage', methods=['GET'])
@token_required
def get_workspace_usage(current_user, pool_name, workspace_id):
    """Get workspace usage status"""
    try:
        pool_name = unquote(pool_name)
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        # Admin can access any pool, regular users only their own
        requesting_user = None if is_admin else username
        result = pool_service.get_workspace_usage_status(
            pool_name, workspace_id, requesting_user=requesting_user
        )
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Validation error in get_workspace_usage: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in get_workspace_usage: {e}")
        return jsonify({"error": str(e)}), 500

@pool_bp.route('/<pool_name>/workspaces/<workspace_id>', methods=['DELETE'])
@token_required
def delete_pool_workspace(current_user, pool_name, workspace_id):
    """Delete a workspace from a pool"""
    try:
        pool_name = unquote(pool_name)
        
        username = current_user['username']
        is_admin = current_user.get('role') == 'admin'
        
        # Admin can access any pool, regular users only their own
        requesting_user = None if is_admin else username
        result = pool_service.delete_workspace_from_pool(
            pool_name, workspace_id, requesting_user=requesting_user
        )
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Validation error in delete_pool_workspace: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in delete_pool_workspace: {e}")
        return jsonify({"error": str(e)}), 500
    
@pool_bp.route('/admin/all', methods=['GET'])
@token_required
@admin_required
def list_all_pools_admin(current_user):
    """List all pools in the system (admin only)"""
    try:
        pools = pool_service.list_pools()
        return jsonify({
            "pools": pools,
            "total_count": len(pools),
            "admin_view": True
        })
    except Exception as e:
        logger.error(f"Error in list_all_pools_admin: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/admin/users/<username>/pools', methods=['GET'])
@token_required
@admin_required
def get_user_pools_admin(current_user, username):
    """Get all pools owned by a specific user (admin only)"""
    try:
        pools = pool_service.get_user_pools(username)
        return jsonify({
            "username": username,
            "pools": pools,
            "pool_count": len(pools),
            "admin_view": True
        })
    except Exception as e:
        logger.error(f"Error in get_user_pools_admin: {e}")
        return jsonify({"error": str(e)}), 500
