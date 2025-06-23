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
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self):
        return {
            'pool_name': self.pool_name,
            'minimum_vms': self.minimum_vms,
            'repo_name': self.repo_name,
            'branch_name': self.branch_name,
            'github_pat': '********',  # Never expose the actual PAT
            'github_username': self.github_username,
            'created_at': self.created_at.isoformat()
        }
    
    def to_json(self):
        return json.dumps(self.to_dict())


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