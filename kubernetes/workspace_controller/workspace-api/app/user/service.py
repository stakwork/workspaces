# app/user/service.py
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime
from kubernetes import client
from app.config import app_config
from app.user.models import User

logger = logging.getLogger(__name__)


class UserService:
    """Service for managing users"""
    
    def __init__(self):
        self.core_v1 = app_config.core_v1
        self.users: Dict[str, User] = {}  # username -> User
        self.token_to_user: Dict[str, str] = {}  # token -> username
        self._load_existing_users()
    
    def create_user(self, username: str, email: str, password: str) -> Dict:
        """Create a new user"""
        try:
            # Validate inputs
            if not username or not email or not password:
                raise ValueError("Username, email, and password are required")
            
            if username in self.users:
                raise ValueError(f"User '{username}' already exists")
            
            # Basic email validation
            if '@' not in email:
                raise ValueError("Invalid email format")
            
            # Check if email is already used
            for user in self.users.values():
                if user.email == email:
                    raise ValueError(f"Email '{email}' is already in use")
            
            # Create user
            user = User(
                username=username,
                email=email,
                password_hash=User.hash_password(password)
            )
            
            # Store user
            self._store_user(user)
            
            # Add to local cache
            self.users[username] = user
            self.token_to_user[user.authentication_token] = username
            
            logger.info(f"Created user '{username}'")
            
            return {
                "success": True,
                "message": f"User '{username}' created successfully",
                "user": user.to_dict(include_sensitive=True)
            }
            
        except Exception as e:
            logger.error(f"Error creating user '{username}': {e}")
            raise Exception(f"Failed to create user: {str(e)}")
    
    def authenticate_user(self, username: str, password: str) -> Optional[Dict]:
        """Authenticate a user with username and password"""
        try:
            if username not in self.users:
                return None
            
            user = self.users[username]
            
            if not user.is_active:
                return None
            
            if not user.verify_password(password):
                return None
            
            # Update last login
            user.last_login = datetime.now()
            self._store_user(user)
            
            logger.info(f"User '{username}' authenticated successfully")
            
            return {
                "success": True,
                "user": user.to_dict(include_sensitive=True)
            }
            
        except Exception as e:
            logger.error(f"Error authenticating user '{username}': {e}")
            return None
    
    def get_user_by_token(self, token: str) -> Optional[User]:
        """Get user by authentication token"""
        try:
            if token not in self.token_to_user:
                return None
            
            username = self.token_to_user[token]
            user = self.users.get(username)
            
            if not user or not user.is_active:
                return None
            
            return user
            
        except Exception as e:
            logger.error(f"Error getting user by token: {e}")
            return None
    
    def get_user(self, username: str) -> Optional[User]:
        """Get user by username"""
        return self.users.get(username)
    
    def list_users(self) -> List[Dict]:
        """List all users (admin function)"""
        return [user.to_dict() for user in self.users.values()]
    
    def update_user(self, username: str, update_data: Dict) -> Dict:
        """Update user information"""
        try:
            if username not in self.users:
                raise ValueError(f"User '{username}' not found")
            
            user = self.users[username]
            
            # Update allowed fields
            if 'email' in update_data:
                # Check if email is already used by another user
                for other_user in self.users.values():
                    if other_user.username != username and other_user.email == update_data['email']:
                        raise ValueError(f"Email '{update_data['email']}' is already in use")
                user.email = update_data['email']
            
            if 'is_active' in update_data:
                user.is_active = bool(update_data['is_active'])
            
            if 'password' in update_data:
                user.password_hash = User.hash_password(update_data['password'])
            
            # Store updated user
            self._store_user(user)
            
            logger.info(f"Updated user '{username}'")
            
            return {
                "success": True,
                "message": f"User '{username}' updated successfully",
                "user": user.to_dict()
            }
            
        except Exception as e:
            logger.error(f"Error updating user '{username}': {e}")
            raise Exception(f"Failed to update user: {str(e)}")
    
    def delete_user(self, username: str) -> Dict:
        """Delete a user"""
        try:
            if username not in self.users:
                raise ValueError(f"User '{username}' not found")
            
            user = self.users[username]
            
            # Remove from Kubernetes
            self._delete_user_config(username)
            
            # Remove from local cache
            del self.users[username]
            if user.authentication_token in self.token_to_user:
                del self.token_to_user[user.authentication_token]
            
            logger.info(f"Deleted user '{username}'")
            
            return {
                "success": True,
                "message": f"User '{username}' deleted successfully"
            }
            
        except Exception as e:
            logger.error(f"Error deleting user '{username}': {e}")
            raise Exception(f"Failed to delete user: {str(e)}")
    
    def regenerate_user_token(self, username: str) -> Dict:
        """Regenerate authentication token for a user"""
        try:
            if username not in self.users:
                raise ValueError(f"User '{username}' not found")
            
            user = self.users[username]
            
            # Remove old token from mapping
            if user.authentication_token in self.token_to_user:
                del self.token_to_user[user.authentication_token]
            
            # Generate new token
            new_token = user.regenerate_token()
            
            # Update token mapping
            self.token_to_user[new_token] = username
            
            # Store updated user
            self._store_user(user)
            
            logger.info(f"Regenerated token for user '{username}'")
            
            return {
                "success": True,
                "message": f"Token regenerated for user '{username}'",
                "authentication_token": new_token
            }
            
        except Exception as e:
            logger.error(f"Error regenerating token for user '{username}': {e}")
            raise Exception(f"Failed to regenerate token: {str(e)}")
    
    def add_pool_to_user(self, username: str, pool_name: str):
        """Add a pool to a user's owned pools"""
        if username in self.users:
            user = self.users[username]
            user.add_pool(pool_name)
            self._store_user(user)
    
    def remove_pool_from_user(self, username: str, pool_name: str):
        """Remove a pool from a user's owned pools"""
        if username in self.users:
            user = self.users[username]
            user.remove_pool(pool_name)
            self._store_user(user)
    
    def _load_existing_users(self):
        """Load existing users from Kubernetes"""
        try:
            # Get all user ConfigMaps
            config_maps = self.core_v1.list_namespaced_config_map(
                namespace="workspace-system",
                label_selector="app=workspace-user"
            )
            
            for cm in config_maps.items:
                try:
                    user_data = json.loads(cm.data.get("user.json", "{}"))
                    
                    # Handle missing fields for backward compatibility
                    if 'pools' not in user_data:
                        user_data['pools'] = []
                    if 'last_login' not in user_data:
                        user_data['last_login'] = None
                    
                    user = User(
                        username=user_data['username'],
                        email=user_data['email'],
                        password_hash=user_data['password_hash'],
                        authentication_token=user_data['authentication_token'],
                        is_active=user_data.get('is_active', True),
                        created_at=datetime.fromisoformat(user_data['created_at']),
                        last_login=datetime.fromisoformat(user_data['last_login']) if user_data['last_login'] else None,
                        pools=user_data['pools']
                    )
                    
                    self.users[user.username] = user
                    self.token_to_user[user.authentication_token] = user.username
                    
                    logger.info(f"Loaded existing user: {user.username}")
                    
                except Exception as e:
                    logger.error(f"Error loading user from ConfigMap {cm.metadata.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Error loading existing users: {e}")
    
    def _store_user(self, user: User):
        """Store user configuration in Kubernetes"""
        user_data = {
            'username': user.username,
            'email': user.email,
            'password_hash': user.password_hash,
            'authentication_token': user.authentication_token,
            'is_active': user.is_active,
            'created_at': user.created_at.isoformat(),
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'pools': user.pools
        }
        
        config_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"user-{user.username}",
                namespace="workspace-system",
                labels={
                    "app": "workspace-user",
                    "username": user.username
                }
            ),
            data={
                "user.json": json.dumps(user_data)
            }
        )
        
        try:
            # Try to update first
            self.core_v1.patch_namespaced_config_map(
                name=f"user-{user.username}",
                namespace="workspace-system",
                body=config_map
            )
        except client.rest.ApiException as e:
            if e.status == 404:
                # Create if it doesn't exist
                self.core_v1.create_namespaced_config_map(
                    namespace="workspace-system",
                    body=config_map
                )
            else:
                raise
    
    def _delete_user_config(self, username: str):
        """Delete user configuration from Kubernetes"""
        try:
            self.core_v1.delete_namespaced_config_map(
                name=f"user-{username}",
                namespace="workspace-system"
            )
        except client.rest.ApiException as e:
            if e.status != 404:  # Ignore if already deleted
                raise


# Global service instance
user_service = UserService()