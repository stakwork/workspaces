from kubernetes import client, config
import base64
from functools import wraps
from flask import request, jsonify
import jwt
import logging
import os

logger = logging.getLogger(__name__)

# Initialize Kubernetes client
try:
    # Load in-cluster config when running in Kubernetes
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes configuration")
except config.config_exception.ConfigException:
    # Load kubeconfig for local development
    try:
        config.load_kube_config()
        logger.info("Loaded kubeconfig for local development")
    except Exception as e:
        logger.error(f"Failed to load any Kubernetes config: {e}")

# Initialize Kubernetes API client
core_v1 = client.CoreV1Api()

def get_jwt_secret():
    """Get JWT secret from Kubernetes secret or environment variable"""
    try:
        # Try to get from Kubernetes secret first
        secret = core_v1.read_namespaced_secret("workspace-auth-secret", "workspace-system")
        jwt_secret = base64.b64decode(secret.data.get("jwt-secret")).decode('utf-8')
        logger.info("Successfully loaded JWT secret from Kubernetes secret")
        return jwt_secret
    except Exception as e:
        logger.warning(f"Could not load JWT secret from Kubernetes: {e}")
        # Fallback to environment variable
        jwt_secret = os.environ.get('JWT_SECRET_KEY')
        if jwt_secret:
            logger.info("Using JWT secret from environment variable")
            return jwt_secret
        else:
            # Final fallback for development
            logger.warning("Using default JWT secret - DO NOT USE IN PRODUCTION")
            return "development-secret-key-change-me"

# Get JWT secret
JWT_SECRET_KEY = get_jwt_secret()

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Check for token in Authorization header
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]  # Bearer <token>
                logger.debug(f"Token received: {token}")
            except IndexError:
                logger.warning("Authorization header is malformed")
        
        if not token:
            logger.warning("Missing token in request headers")
            return jsonify({'error': 'Token is missing'}), 401
        
        try:
            # Decode the token
            data = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
            current_user = {
                'username': data['username'],
                'role': data.get('role', 'user'),
                'exp': data['exp']
            }
            logger.debug(f"Token valid. Decoded data: {current_user}")
        except jwt.ExpiredSignatureError:
            logger.warning("Token has expired")
            return jsonify({'error': 'Token has expired'}), 401
        except jwt.InvalidTokenError:
            logger.warning("Invalid token")
            return jsonify({'error': 'Token is invalid'}), 401
        except Exception as e:
            logger.error(f"Token validation error: {str(e)}", exc_info=True)
            return jsonify({'error': 'Token validation failed'}), 401
        
        return f(current_user, *args, **kwargs)
    
    return decorated

def generate_token(user_data):
    """Generate a JWT token for a user"""
    try:
        if not isinstance(user_data, dict):
            raise ValueError("user_data must be a dictionary")
        
        token = jwt.encode(user_data, JWT_SECRET_KEY, algorithm='HS256')
        return token
    except Exception as e:
        logger.error(f"Error generating token: {e}")
        return None

def verify_token(token):
    """Verify a JWT token"""
    try:
        if not token:
            return None
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token has expired")
        return None
    except jwt.InvalidTokenError:
        logger.warning("Invalid token")
        return None
    except Exception as e:
        logger.error(f"Token verification error: {str(e)}")
        return None