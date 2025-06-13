from datetime import datetime, timezone


class Pool:
    """
    Represents a pool of workspaces with common configuration.
    A pool maintains a minimum number of workspaces with the same repository setup.
    """
    def __init__(self, name, minimum_vms, repo_name, branch_name, github_pat):
        self.name = name
        self.minimum_vms = minimum_vms
        self.repo_name = repo_name
        self.branch_name = branch_name
        self.github_pat = github_pat
        self.workspace_ids = set()  # Set of workspace IDs in this pool
        self.used_workspace_ids = set()  # Set of workspace IDs that are currently in use
        self.last_check = datetime.now(timezone.utc)
        self.is_healthy = True
        self.status_message = "Pool created"
        
    def to_dict(self):
        """Convert pool object to dictionary for JSON serialization"""
        return {
            'name': self.name,
            'minimum_vms': self.minimum_vms,
            'repo_name': self.repo_name,
            'branch_name': self.branch_name or 'main',
            'github_pat': '********' if self.github_pat else None,  # Mask the PAT
            'workspace_count': len(self.workspace_ids),
            'available_vms': len(self.workspace_ids - self.used_workspace_ids),
            'used_vms': len(self.used_workspace_ids),
            'is_healthy': self.is_healthy,
            'status_message': self.status_message,
            'last_check': self.last_check.isoformat() if self.last_check else None
        }
    
    def update_status(self, is_healthy, message):
        """Update pool health status and message"""
        self.is_healthy = is_healthy
        self.status_message = message
        self.last_check = datetime.now(timezone.utc)
    
    def add_workspace(self, workspace_id):
        """Add a workspace to the pool"""
        self.workspace_ids.add(workspace_id)
    
    def remove_workspace(self, workspace_id):
        """Remove a workspace from the pool"""
        self.workspace_ids.discard(workspace_id)
    
    def mark_workspace_as_used(self, workspace_id):
        """Mark a workspace as being used"""
        if workspace_id not in self.workspace_ids:
            raise ValueError(f"Workspace {workspace_id} does not belong to this pool")
        self.used_workspace_ids.add(workspace_id)
    
    def mark_workspace_as_unused(self, workspace_id):
        """Mark a workspace as no longer being used"""
        self.used_workspace_ids.discard(workspace_id)

    def is_workspace_used(self, workspace_id):
        """Check if a workspace is currently in use"""
        return workspace_id in self.used_workspace_ids
    
    def get_available_workspaces(self):
        """Get a list of workspace IDs that are not currently in use"""
        return list(self.workspace_ids - self.used_workspace_ids)
