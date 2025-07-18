# app/user/models.py
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
import json
import secrets
import hashlib


@dataclass
class User:
    """User model for authentication and authorization"""
    username: str
    email: str
    password_hash: str
    authentication_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    last_login: Optional[datetime] = None
    pools: List[str] = field(default_factory=list)  # Pool names owned by this user
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using SHA-256"""
        return hashlib.sha256(password.encode()).hexdigest()
    
    def verify_password(self, password: str) -> bool:
        """Verify a password against the hash"""
        return self.password_hash == self.hash_password(password)
    
    def regenerate_token(self) -> str:
        """Generate a new authentication token"""
        self.authentication_token = secrets.token_urlsafe(32)
        return self.authentication_token
    
    def add_pool(self, pool_name: str):
        """Add a pool to the user's owned pools"""
        if pool_name not in self.pools:
            self.pools.append(pool_name)
    
    def remove_pool(self, pool_name: str):
        """Remove a pool from the user's owned pools"""
        if pool_name in self.pools:
            self.pools.remove(pool_name)
    
    def owns_pool(self, pool_name: str) -> bool:
        """Check if user owns a specific pool"""
        return pool_name in self.pools
    
    def to_dict(self, include_sensitive=False):
        """Convert to dict, optionally including sensitive data"""
        result = {
            'username': self.username,
            'email': self.email,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat(),
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'pools': self.pools.copy(),
            'authentication_token': self.authentication_token,
            'pool_count': len(self.pools)
        }
        
        # if include_sensitive:
        #     result['authentication_token'] = self.authentication_token
            
        return result
    
    def to_json(self, include_sensitive=False):
        return json.dumps(self.to_dict(include_sensitive))