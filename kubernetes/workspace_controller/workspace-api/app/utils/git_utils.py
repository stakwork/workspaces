import hashlib
import logging
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def get_git_commit_hash(repo_url, branch=None):
    """
    Get the latest commit hash for a repository and branch.
    
    Args:
        repo_url (str): The Git repository URL
        branch (str): The branch name (optional, defaults to default branch)
    
    Returns:
        str: The commit hash or None if failed
    """
    
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Clone just the metadata (shallow clone with depth=1)
            clone_cmd = ['git', 'clone', '--depth=1']
            if branch:
                clone_cmd.extend(['-b', branch])
            clone_cmd.extend([repo_url, temp_dir])
            
            result = subprocess.run(
                clone_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                logger.warning(f"Failed to clone {repo_url}: {result.stderr}")
                return None
            
            # Get the commit hash
            hash_result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if hash_result.returncode == 0:
                commit_hash = hash_result.stdout.strip()
                logger.info(f"Got commit hash for {repo_url}:{branch}: {commit_hash[:8]}")
                return commit_hash
            else:
                logger.warning(f"Failed to get commit hash: {hash_result.stderr}")
                return None
                
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout getting commit hash for {repo_url}")
        return None
    except Exception as e:
        logger.warning(f"Error getting commit hash for {repo_url}: {e}")
        return None


def generate_cache_key(workspace_config):
    """
    Generate a cache key based on workspace configuration.
    
    Args:
        workspace_config (dict): The workspace configuration
    
    Returns:
        str: A hash that can be used as a cache key
    """
    cache_components = []
    
    # Add git commit hashes for all repositories
    for i, repo_url in enumerate(workspace_config['github_urls']):
        branch = workspace_config['github_branches'][i] if i < len(workspace_config['github_branches']) else None
        commit_hash = get_git_commit_hash(repo_url, branch)
        
        if commit_hash:
            cache_components.append(f"repo_{i}:{repo_url}:{branch or 'default'}:{commit_hash}")
        else:
            # Fallback to URL+branch if we can't get commit hash
            cache_components.append(f"repo_{i}:{repo_url}:{branch or 'default'}:fallback")
    
    # Add image configuration
    if workspace_config['use_custom_image_url']:
        cache_components.append(f"custom_image_url:{workspace_config['custom_image_url']}")
    else:
        cache_components.append(f"custom_image:{workspace_config['custom_image']}")
        cache_components.append(f"use_dev_container:{workspace_config['use_dev_container']}")
    
    # Add container files hash if present
    container_files = workspace_config.get('container_files')
    if container_files:
        for key, value in container_files.items():
            if value and value != "None" and value != "null":
                # Hash the base64 content
                content_hash = hashlib.md5(value.encode()).hexdigest()
                cache_components.append(f"container_file_{key}:{content_hash}")
    
    # Add resource requirements
    cache_components.append(f"cpu:{workspace_config.get('cpu', '2')}")
    cache_components.append(f"memory:{workspace_config.get('memory', '8Gi')}")
    
    # Create final cache key
    cache_string = '|'.join(sorted(cache_components))
    cache_hash = hashlib.sha256(cache_string.encode()).hexdigest()[:16]
    
    logger.info(f"Generated cache key: {cache_hash}")
    logger.debug(f"Cache components: {cache_components}")
    
    return cache_hash


def extract_repo_name_from_url(repo_url):
    """
    Extract repository name from a Git URL.
    
    Args:
        repo_url (str): The Git repository URL
    
    Returns:
        str: The repository name
    """
    repo_parts = repo_url.rstrip('/').split('/')
    return repo_parts[-1].replace('.git', '') if len(repo_parts) > 1 else "unknown"


def extract_repo_owner_from_url(repo_url):
    """
    Extract repository owner from a Git URL.
    
    Args:
        repo_url (str): The Git repository URL
    
    Returns:
        str: The repository owner
    """
    repo_parts = repo_url.rstrip('/').split('/')
    return repo_parts[-2] if len(repo_parts) > 2 else "unknown-owner"