from datetime import datetime, timezone


class CleanupStatus:
    """Tracks the status and progress of cleanup operations"""
    def __init__(self):
        self.total_resources = 0
        self.cleaned_resources = 0
        self.failed_resources = []
        self.start_time = None
        self.end_time = None
        self.in_progress = False
        
    def start(self):
        """Mark cleanup as started"""
        self.start_time = datetime.now(timezone.utc)
        self.in_progress = True
        
    def complete(self):
        """Mark cleanup as completed"""
        self.end_time = datetime.now(timezone.utc)
        self.in_progress = False
        
    def add_failure(self, resource_type, resource_name, error):
        """Record a cleanup failure"""
        self.failed_resources.append({
            "type": resource_type,
            "name": resource_name,
            "error": str(error),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    
    def to_dict(self):
        """Convert cleanup status to dictionary representation"""
        return {
            "total_resources": self.total_resources,
            "cleaned_resources": self.cleaned_resources,
            "failed_resources": self.failed_resources,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "in_progress": self.in_progress,
            "success_rate": (
                (self.cleaned_resources / self.total_resources * 100) 
                if self.total_resources > 0 else 0
            )
        }
