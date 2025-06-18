import jwt
import bcrypt
import logging
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
from app.config import app_config
from app.auth.decorators import token_required

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
        
        # Find user in configuration
        user = None
        for u in app_config.USERS_CONFIG.get('users', []):
            if u['username'] == username:
                user = u
                break
        
        if not user:
            return jsonify({
                'success': False,
                'error': 'Invalid username or password'
            }), 401
        
        # Verify password
        if not bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            return jsonify({
                'success': False,
                'error': 'Invalid username or password'
            }), 401
        
        # Generate JWT token
        payload = {
            'username': user['username'],
            'role': user.get('role', 'user'),
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)  # Token expires in 24 hours
        }
        
        token = jwt.encode(payload, app_config.JWT_SECRET_KEY, algorithm='HS256')
        
        return jsonify({
            'success': True,
            'token': token,
            'user': {
                'username': user['username'],
                'role': user.get('role', 'user')
            }
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
            'role': current_user['role']
        }
    })

@auth_bp.route('/refresh', methods=['POST'])
@token_required
def refresh_token(current_user):
    """Refresh the JWT token"""
    try:
        # Generate new JWT token
        payload = {
            'username': current_user['username'],
            'role': current_user['role'],
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)
        }
        
        token = jwt.encode(payload, app_config.JWT_SECRET_KEY, algorithm='HS256')
        
        return jsonify({
            'success': True,
            'token': token,
            'user': {
                'username': current_user['username'],
                'role': current_user['role']
            }
        })
        
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        return jsonify({
            'success': False,
            'error': 'Token refresh failed'
        }), 500