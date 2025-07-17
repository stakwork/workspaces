# app/user/routes.py
import logging
from flask import Blueprint, request, jsonify
from app.auth.decorators import token_required, admin_required
from app.user.service import user_service

logger = logging.getLogger(__name__)
user_bp = Blueprint('user', __name__)


# @user_bp.route('/register', methods=['POST'])
# def register_user():
#     """Register a new user"""
#     try:
#         if not request.json:
#             return jsonify({"error": "Request body must be JSON"}), 400
        
#         data = request.json
        
#         # Validate required fields
#         required_fields = ['username', 'email', 'password']
#         for field in required_fields:
#             if field not in data:
#                 return jsonify({"error": f"Missing required field: {field}"}), 400
        
#         # Validate password strength (basic)
#         if len(data['password']) < 6:
#             return jsonify({"error": "Password must be at least 6 characters long"}), 400
        
#         result = user_service.create_user(
#             username=data['username'],
#             email=data['email'],
#             password=data['password']
#         )
        
#         return jsonify(result), 201
        
#     except ValueError as e:
#         logger.warning(f"Validation error in register_user: {e}")
#         return jsonify({"error": str(e)}), 400
#     except Exception as e:
#         logger.error(f"Error in register_user: {e}")
#         return jsonify({"error": str(e)}), 500


@user_bp.route('/login', methods=['POST'])
def login_user():
    """Login a user"""
    try:
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        data = request.json
        
        # Validate required fields
        required_fields = ['username', 'password']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        result = user_service.authenticate_user(
            username=data['username'],
            password=data['password']
        )
        
        if result:
            return jsonify(result)
        else:
            return jsonify({"error": "Invalid username or password"}), 401
        
    except Exception as e:
        logger.error(f"Error in login_user: {e}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/me', methods=['GET'])
@token_required
def get_current_user(current_user):
    """Get current user information"""
    try:
        return jsonify({
            "success": True,
            "user": current_user
        })
    except Exception as e:
        logger.error(f"Error in get_current_user: {e}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/me', methods=['PUT'])
@token_required
def update_current_user(current_user):
    """Update current user information"""
    try:
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        data = request.json
        username = current_user['username']
        
        # Users can only update their own email and password
        allowed_fields = ['email', 'password']
        update_data = {k: v for k, v in data.items() if k in allowed_fields}
        
        if not update_data:
            return jsonify({"error": "No valid fields to update"}), 400
        
        result = user_service.update_user(username, update_data)
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Validation error in update_current_user: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in update_current_user: {e}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/me/token/regenerate', methods=['POST'])
@token_required
def regenerate_current_user_token(current_user):
    """Regenerate authentication token for current user"""
    try:
        username = current_user['username']
        result = user_service.regenerate_user_token(username)
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error in regenerate_current_user_token: {e}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/me/pools', methods=['GET'])
@token_required
def get_user_pools(current_user):
    """Get pools owned by current user"""
    try:
        return jsonify({
            "success": True,
            "pools": current_user.get('pools', []),
            "pool_count": len(current_user.get('pools', []))
        })
    except Exception as e:
        logger.error(f"Error in get_user_pools: {e}")
        return jsonify({"error": str(e)}), 500

@user_bp.route('', methods=['POST'])
@token_required
@admin_required
def create_user(current_user):
    """Create a new user (admin only)"""
    try:
        if not request.json:
            return jsonify({"error": "Request body must be JSON"}), 400
        
        data = request.json
        admin_username = current_user['username']
        
        # Validate required fields
        required_fields = ['username', 'email', 'password']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Validate password strength (basic)
        if len(data['password']) < 6:
            return jsonify({"error": "Password must be at least 6 characters long"}), 400
        
        result = user_service.create_user(
            username=data['username'],
            email=data['email'],
            password=data['password']
        )
        
        return jsonify(result), 201
        
    except ValueError as e:
        logger.warning(f"Validation error in create_user: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in create_user: {e}")
        return jsonify({"error": str(e)}), 500

@user_bp.route('', methods=['GET'])
@token_required
@admin_required
def list_users(current_user):
    """List all users (admin only)"""
    try:
        users = user_service.list_users()
        return jsonify({"users": users})
    except Exception as e:
        logger.error(f"Error in list_users: {e}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/<username>', methods=['GET'])
@token_required
@admin_required
def get_user(current_user, username):
    """Get user by username (admin only)"""
    try:
        user = user_service.get_user(username)
        if not user:
            return jsonify({"error": "User not found"}), 404
        
        return jsonify({
            "success": True,
            "user": user.to_dict()
        })
    except Exception as e:
        logger.error(f"Error in get_user: {e}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/<username>', methods=['DELETE'])
@token_required
@admin_required
def delete_user(current_user, username):
    """Delete user (admin only)"""
    try:
        result = user_service.delete_user(username)
        return jsonify(result)
    except ValueError as e:
        logger.warning(f"User not found: {e}")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in delete_user: {e}")
        return jsonify({"error": str(e)}), 500