# app/auth/routes.py (updated to handle both systems)
import jwt
import bcrypt
import logging
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
from app.config import app_config
from app.auth.decorators import token_required
from app.user.service import user_service

logger = logging.getLogger(__name__)
auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['POST'])
def login():
    """Authenticate user and return JWT token"""
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({
                'success': False,
                'error': 'Username and password are required'
            }), 400
        
        user = None
        auth_method = None
        
        # First, try to find user in the new user service
        try:
            user_service_result = user_service.authenticate_user(username, password)
            if user_service_result and user_service_result.get('success'):
                user = user_service_result['user']
                auth_method = 'user_service'
                logger.info(f"User '{username}' authenticated via user service")
        except Exception as e:
            logger.debug(f"User service authentication failed for '{username}': {e}")
        
        # If user service authentication failed, try configuration-based users
        if not user:
            try:
                config_user = None
                for u in app_config.USERS_CONFIG.get('users', []):
                    if u['username'] == username:
                        config_user = u
                        break
                
                if config_user:
                    # Verify password
                    if bcrypt.checkpw(password.encode('utf-8'), config_user['password'].encode('utf-8')):
                        user = {
                            'username': config_user['username'],
                            'role': config_user.get('role', 'user'),
                            'email': config_user.get('email', ''),
                            'is_active': True
                        }
                        auth_method = 'config'
                        logger.info(f"User '{username}' authenticated via configuration")
                    else:
                        logger.warning(f"Invalid password for config user '{username}'")
                else:
                    logger.warning(f"User '{username}' not found in configuration")
            except Exception as e:
                logger.error(f"Configuration authentication error for '{username}': {e}")
        
        if not user:
            return jsonify({
                'success': False,
                'error': 'Invalid username or password'
            }), 401
        
        # Check if user is active (for user service users)
        if not user.get('is_active', True):
            return jsonify({
                'success': False,
                'error': 'Account is deactivated'
            }), 401
        
        # Determine user role and admin status
        user_role = user.get('role', 'user')

        is_admin = user_role == 'admin'
        
        # If user is admin, ensure role is set correctly
        if is_admin:
            user_role = 'admin'
        
        # Generate JWT token
        payload = {
            'username': user['username'],
            'role': user_role,
            'auth_method': auth_method,
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)  # Token expires in 24 hours
        }
        
        token = jwt.encode(payload, app_config.JWT_SECRET_KEY, algorithm='HS256')
        
        # Prepare response user data
        response_user = {
            'username': user['username'],
            'role': user_role,
            'is_admin': is_admin,
            'auth_method': auth_method
        }
        
        # Add additional fields if available
        if user.get('email'):
            response_user['email'] = user['email']
        
        return jsonify({
            'success': True,
            'token': token,
            'user': response_user
        })
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({
            'success': False,
            'error': 'Authentication failed'
        }), 500

@auth_bp.route('/verify', methods=['GET'])
@token_required
def verify_token(current_user):
    """Verify if the current token is valid"""
    return jsonify({
        'success': True,
        'user': {
            'username': current_user['username'],
            'role': current_user.get('role', 'user'),
            'is_admin': current_user.get('is_admin', False),
            'token_type': current_user.get('token_type', 'unknown')
        }
    })

@auth_bp.route('/refresh', methods=['POST'])
@token_required
def refresh_token(current_user):
    """Refresh the JWT token"""
    try:
        # Only refresh JWT tokens, not user service tokens
        if current_user.get('token_type') == 'user_service':
            return jsonify({
                'success': False,
                'error': 'User service tokens cannot be refreshed via this endpoint'
            }), 400
        
        # Generate new JWT token
        payload = {
            'username': current_user['username'],
            'role': current_user.get('role', 'user'),
            'auth_method': current_user.get('auth_method', 'unknown'),
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)
        }
        
        token = jwt.encode(payload, app_config.JWT_SECRET_KEY, algorithm='HS256')
        
        return jsonify({
            'success': True,
            'token': token,
            'user': {
                'username': current_user['username'],
                'role': current_user.get('role', 'user'),
                'is_admin': current_user.get('is_admin', False)
            }
        })
        
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        return jsonify({
            'success': False,
            'error': 'Token refresh failed'
        }), 500

# Additional endpoint for user service token refresh
@auth_bp.route('/refresh-user-token', methods=['POST'])
@token_required
def refresh_user_service_token(current_user):
    """Refresh user service token"""
    try:
        # Only refresh user service tokens
        if current_user.get('token_type') != 'user_service':
            return jsonify({
                'success': False,
                'error': 'This endpoint is only for user service tokens'
            }), 400
        
        username = current_user['username']
        result = user_service.regenerate_user_token(username)
        
        if result.get('success'):
            return jsonify({
                'success': True,
                'token': result['authentication_token'],
                'user': {
                    'username': username,
                    'role': current_user.get('role', 'user'),
                    'is_admin': current_user.get('is_admin', False)
                }
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to refresh token'
            }), 500
        
    except Exception as e:
        logger.error(f"User service token refresh error: {e}")
        return jsonify({
            'success': False,
            'error': 'Token refresh failed'
        }), 500

@auth_bp.route('/logout', methods=['POST'])
@token_required
def logout(current_user):
    """Logout user (mainly for logging purposes)"""
    try:
        username = current_user['username']
        logger.info(f"User '{username}' logged out")
        
        return jsonify({
            'success': True,
            'message': 'Logged out successfully'
        })
        
    except Exception as e:
        logger.error(f"Logout error: {e}")
        return jsonify({
            'success': False,
            'error': 'Logout failed'
        }), 500

@auth_bp.route('/change-password', methods=['POST'])
@token_required
def change_password(current_user):
    """Change user password"""
    try:
        data = request.get_json()
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        
        if not current_password or not new_password:
            return jsonify({
                'success': False,
                'error': 'Current password and new password are required'
            }), 400
        
        if len(new_password) < 6:
            return jsonify({
                'success': False,
                'error': 'New password must be at least 6 characters long'
            }), 400
        
        username = current_user['username']
        
        # Handle password change based on auth method
        if current_user.get('token_type') == 'user_service':
            # User service user - verify current password first
            auth_result = user_service.authenticate_user(username, current_password)
            if not auth_result or not auth_result.get('success'):
                return jsonify({
                    'success': False,
                    'error': 'Current password is incorrect'
                }), 401
            
            # Update password
            result = user_service.update_user(username, {'password': new_password})
            if result.get('success'):
                logger.info(f"Password changed for user service user '{username}'")
                return jsonify({
                    'success': True,
                    'message': 'Password changed successfully'
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Failed to change password'
                }), 500
        else:
            # Configuration-based user - would need to implement config update
            # For now, return an error since config users are typically managed differently
            return jsonify({
                'success': False,
                'error': 'Password change not supported for configuration-based users'
            }), 400
        
    except Exception as e:
        logger.error(f"Change password error: {e}")
        return jsonify({
            'success': False,
            'error': 'Password change failed'
        }), 500

@auth_bp.route('/user-info', methods=['GET'])
@token_required
def get_user_info(current_user):
    """Get detailed user information"""
    try:
        username = current_user['username']
        user_info = {
            'username': username,
            'role': current_user.get('role', 'user'),
            'is_admin': current_user.get('is_admin', False),
            'token_type': current_user.get('token_type', 'unknown')
        }
        
        # Get additional info for user service users
        if current_user.get('token_type') == 'user_service':
            user = user_service.get_user(username)
            if user:
                user_info.update({
                    'email': user.email,
                    'is_active': user.is_active,
                    'created_at': user.created_at.isoformat(),
                    'last_login': user.last_login.isoformat() if user.last_login else None,
                    'pool_count': len(user.pools)
                })
        
        return jsonify({
            'success': True,
            'user': user_info
        })
        
    except Exception as e:
        logger.error(f"Get user info error: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to get user information'
        }), 500