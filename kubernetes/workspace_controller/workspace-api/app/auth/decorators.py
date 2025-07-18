# app/auth/decorators.py (updated to handle both JWT and user service tokens)
import jwt
import logging
from functools import wraps
from flask import request, jsonify
from app.config import app_config
from app.user.service import user_service

logger = logging.getLogger(__name__)

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Check for token in Authorization header
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]  # Bearer <token>
            except IndexError:
                return jsonify({'error': 'Invalid token format'}), 401
        
        if not token:
            return jsonify({'error': 'Token is missing'}), 401
        
        current_user = None
        
        # Try JWT token first (your existing system)
        try:
            # Decode the JWT token
            data = jwt.decode(token, app_config.JWT_SECRET_KEY, algorithms=['HS256'])
            current_user = {
                'username': data['username'],
                'role': data.get('role', 'user'),
                'exp': data['exp'],
                'is_admin': data.get('role') == 'admin',
                'token_type': 'jwt'
            }
            logger.debug(f"JWT token validated for user: {current_user['username']}")
            
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'JWT token has expired'}), 401
        except jwt.InvalidTokenError:
            # JWT failed, try user service token
            try:
                user = user_service.get_user_by_token(token)
                if not user:
                    return jsonify({'error': 'Invalid or expired token'}), 401
                
                # Convert user to dict for compatibility
                current_user = user.to_dict(include_sensitive=True)
                current_user['is_admin'] = False
                current_user['role'] = 'admin' if current_user['is_admin'] else 'user'
                current_user['token_type'] = 'user_service'
                
                logger.debug(f"User service token validated for user: {current_user['username']}")
                
            except Exception as e:
                logger.error(f"Error validating user service token: {e}")
                return jsonify({'error': 'Token validation failed'}), 401
        
        if not current_user:
            return jsonify({'error': 'Token validation failed'}), 401
        
        return f(current_user, *args, **kwargs)
    
    return decorated


def admin_required(f):
    """Decorator to require admin privileges"""
    @wraps(f)
    def decorated_function(current_user, *args, **kwargs):
        # Check both role and is_admin flag for backward compatibility
        is_admin = (current_user.get('role') == 'admin' or 
                   current_user.get('is_admin', False))
        
        if not is_admin:
            return jsonify({'error': 'Admin privileges required'}), 403
        
        return f(current_user, *args, **kwargs)
    
    return decorated_function