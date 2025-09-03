from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime
import json


@dataclass
class PoolConfig:
    """Configuration for a pool"""
    pool_name: str
    minimum_vms: int
    repo_name: str
    branch_name: str
    github_pat: Optional[str] = None
    github_username: Optional[str] = None
    env_vars: List[Dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    owner_username: Optional[str] = None
    devcontainer_json: Optional[str] = None  # base64 encoded
    dockerfile: Optional[str] = None  # base64 encoded  
    docker_compose_yml: Optional[str] = None  # base64 encoded
    pm2_config_js: Optional[str] = None  # base64 encoded
    cpu: Optional[str] = "2"  # CPU allocation (sets both request and limit)
    memory: Optional[str] = "8Gi"  # Memory allocation (sets both request and limit)

    def _mask_value(self, value: str) -> str:
        """Mask sensitive value showing only first 2 and last 2 characters"""
        if not value:
            return ""
        if len(value) <= 4:
            return "*" * len(value)
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    
    def _mask_env_value(self, value: str) -> str:
        """Mask environment variable value showing only first 2 and last 2 characters"""
        return self._mask_value(value)
    
    def to_dict(self, mask_sensitive=True):
        """Convert to dict, optionally masking sensitive values"""
        env_vars_output = []
        if self.env_vars and mask_sensitive:
            env_vars_output = [
                {
                    'name': env_var['name'],
                    'value': self._mask_env_value(env_var['value']),
                    'masked': True
                }
                for env_var in self.env_vars
            ]
        elif self.env_vars:
            env_vars_output = self.env_vars
        
        # Mask GitHub PAT
        github_pat_output = '********'
        if mask_sensitive and self.github_pat:
            github_pat_output = {
                'value': self._mask_value(self.github_pat),
                'masked': True
            }
        elif not mask_sensitive:
            github_pat_output = self.github_pat
            
        return {
            'pool_name': self.pool_name,
            'minimum_vms': self.minimum_vms,
            'repo_name': self.repo_name,
            'branch_name': self.branch_name,
            'github_pat': github_pat_output,
            'github_username': self.github_username,
            'env_vars': env_vars_output,
            'created_at': self.created_at.isoformat(),
            'owner_username': self.owner_username,
            'devcontainer_json': self.devcontainer_json,
            'dockerfile': self.dockerfile, 
            'docker_compose_yml': self.docker_compose_yml,
            'pm2_config_js': self.pm2_config_js,
            'cpu': self.cpu,
            'memory': self.memory
        }
    
    def to_json(self, mask_sensitive=True):
        return json.dumps(self.to_dict(mask_sensitive))



@dataclass
class PoolStatus:
    """Status information for a pool"""
    pool_name: str
    minimum_vms: int
    current_vms: int
    running_vms: int
    pending_vms: int
    failed_vms: int
    used_vms: int = 0  # Add used VMs count
    unused_vms: int = 0  # Add unused VMs count
    workspaces: List[Dict] = field(default_factory=list)
    last_check: datetime = field(default_factory=datetime.now)
    
    @property
    def needs_scaling(self) -> bool:
        """Check if pool needs more VMs"""
        return self.running_vms < self.minimum_vms
    
    @property
    def scale_needed(self) -> int:
        """How many VMs need to be created"""
        return max(0, self.minimum_vms - (self.running_vms + self.pending_vms))
    
    @property
    def available_vms(self) -> int:
        """How many VMs are available for use (running and unused)"""
        return self.unused_vms
    
    def to_dict(self):
        return {
            'pool_name': self.pool_name,
            'minimum_vms': self.minimum_vms,
            'current_vms': self.current_vms,
            'running_vms': self.running_vms,
            'pending_vms': self.pending_vms,
            'failed_vms': self.failed_vms,
            'used_vms': self.used_vms,
            'unused_vms': self.unused_vms,
            'needs_scaling': self.needs_scaling,
            'scale_needed': self.scale_needed,
            'workspaces': self.workspaces,
            'last_check': self.last_check.isoformat()
        }