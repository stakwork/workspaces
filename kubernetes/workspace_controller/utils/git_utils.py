"""Git related utility functions for workspace management."""

import os
import time
import random
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class GitCloneManager:
    """Manages git clone operations with retries and backoff."""
    
    MAX_RETRIES = 5
    BASE_DELAY = 1
    MAX_DELAY = 30
    
    @staticmethod
    def generate_clone_script(repo_url: str, folder_name: str, branch: Optional[str] = None) -> str:
        """
        Generate a bash script for cloning with retries and exponential backoff.
        
        Args:
            repo_url: The Git repository URL
            folder_name: The target folder name
            branch: Optional branch name
        
        Returns:
            str: Bash script that handles cloning with retries
        """
        script = f"""
# Function to perform git clone with retry
function clone_with_retry() {{
    local repo_url="$1"
    local target_dir="$2"
    local branch="$3"
    local max_retries=5
    local retry_count=0
    local wait_time=1

    while [ "$retry_count" -lt "$max_retries" ]; do
        # Check if we already have a valid clone
        if [ -d "$target_dir/.git" ]; then
            echo "Repository already exists in $target_dir, attempting to update..."
            cd "$target_dir"
            
            # Fetch updates
            if git fetch origin; then
                # If branch specified, checkout that branch
                if [ ! -z "$branch" ]; then
                    git checkout "$branch" && git pull origin "$branch"
                else
                    git pull origin
                fi
                cd ..
                return 0
            else
                echo "Failed to update existing repository"
                cd ..
                # Remove failed repo and try fresh clone
                rm -rf "$target_dir"
            fi
        fi

        echo "Attempt $((retry_count + 1)) of $max_retries: Cloning $repo_url..."
        
        # Construct clone command based on branch
        local clone_cmd="git clone"
        if [ ! -z "$branch" ]; then
            clone_cmd="$clone_cmd -b $branch"
        fi
        clone_cmd="$clone_cmd --depth 1 $repo_url $target_dir"
        
        if $clone_cmd; then
            echo "Successfully cloned repository"
            return 0
        else
            retry_count=$((retry_count + 1))
            if [ "$retry_count" -lt "$max_retries" ]; then
                wait_time=$((wait_time * 2 + RANDOM % 5))
                echo "Clone failed. Waiting $wait_time seconds before retry..."
                sleep "$wait_time"
            else
                echo "Failed to clone repository after $max_retries attempts"
                return 1
            fi
        fi
    done
    return 1
}}

# Set git global configs for efficient cloning
git config --global core.compression 9
git config --global protocol.version 2
git config --global http.postBuffer 524288000

# Attempt the clone with retry logic
echo "Cloning {repo_url} into {folder_name}..."
if ! clone_with_retry "{repo_url}" "{folder_name}" "{branch or ''}"; then
    echo "Failed to clone repository after all retries"
    exit 1
fi

# Mark repo as safe
git config --global --add safe.directory "/workspaces/{folder_name}"
"""
        return script
