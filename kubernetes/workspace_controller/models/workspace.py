from datetime import datetime, timezone
import uuid

class Workspace:
    """Represents a workspace instance with its configuration and state"""
    def __init__(self, repo_name, branch_name, github_pat, pool_name=None):
        self.id = str(uuid.uuid4())
        self.repo_name = repo_name
        self.branch_name = branch_name
        self.github_pat = github_pat
        self.pool_name = pool_name
        self.created_at = datetime.now(timezone.utc)
        self.status = "creating"
        self.owner = None
        self.last_accessed = datetime.now(timezone.utc)
        self.ports = {}
        self.ready = False
        
    def to_dict(self):
        """Convert workspace to dictionary representation"""
        return {
            "id": self.id,
            "repo_name": self.repo_name,
            "branch_name": self.branch_name,
            "pool_name": self.pool_name,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "owner": self.owner,
            "last_accessed": self.last_accessed.isoformat(),
            "ports": self.ports,
            "ready": self.ready
        }
    
    def update_status(self, status: str):
        """Update workspace status"""
        self.status = status
        
    def mark_accessed(self):
        """Update last access time"""
        self.last_accessed = datetime.now(timezone.utc)
        
    def add_port(self, port: int, protocol: str):
        """Add a detected port"""
        self.ports[str(port)] = protocol
        
    def set_owner(self, username: str):
        """Set the workspace owner"""
        self.owner = username
        
    def mark_ready(self):
        """Mark workspace as ready"""
        self.ready = True
        self.status = "ready"
