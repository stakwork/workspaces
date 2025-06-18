import logging
from flask import Blueprint, request, jsonify
from app.auth.decorators import token_required
from app.pool.service import pool_service

logger = logging.getLogger(__name__)
pool_bp = Blueprint('pool', __name__)


@pool_bp.route('', methods=['GET'])
@token_required
def list_pools(current_user):
    """List all pools"""
    try:
        pools = pool_service.list_pools()
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
        
        # Validate required fields
        required_fields = ['pool_name', 'minimum_vms', 'repo_name', 'branch_name', 'github_pat']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Validate data types
        if not isinstance(data['minimum_vms'], int) or data['minimum_vms'] < 1:
            return jsonify({"error": "minimum_vms must be a positive integer"}), 400
        
        result = pool_service.create_pool(
            pool_name=data['pool_name'],
            minimum_vms=data['minimum_vms'],
            repo_name=data['repo_name'],
            branch_name=data['branch_name'],
            github_pat=data['github_pat']
        )
        
        return jsonify(result), 201
        
    except ValueError as e:
        logger.warning(f"Validation error in create_pool: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in create_pool: {e}")
        return jsonify({"error": str(e)}), 500


@pool_bp.route('/<pool_name>', methods=['GET'])
@token_required
def get_pool(current_user, pool_name):
    """Get details for a specific pool"""
    try:
        pool_info = pool_service.get_pool(pool_name)
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
        result = pool_service.delete_pool(pool_name)
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
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        data = request.json
        
        if 'minimum_vms' not in data:
            return jsonify({"error": "Missing required field: minimum_vms"}), 400
        
        if not isinstance(data['minimum_vms'], int) or data['minimum_vms'] < 1:
            return jsonify({"error": "minimum_vms must be a positive integer"}), 400
        
        result = pool_service.scale_pool(pool_name, data['minimum_vms'])
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
        workspace = pool_service.get_available_workspace(pool_name)
        
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
            }), 404
            
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
        pool_info = pool_service.get_pool(pool_name)
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
        pool_info = pool_service.get_pool(pool_name)
        return jsonify({
            "pool_name": pool_name,
            "workspaces": pool_info['status']['workspaces']
        })
    except ValueError as e:
        logger.warning(f"Pool not found: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in list_pool_workspaces: {e}")
        return jsonify({"error": str(e)}), 500