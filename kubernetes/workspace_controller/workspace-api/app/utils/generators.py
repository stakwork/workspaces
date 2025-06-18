import uuid
import time
import string
import random


def generate_random_subdomain(length=8):
    """Generate a random subdomain name"""
    letters = string.ascii_lowercase + string.digits
    return ''.join(random.choice(letters) for i in range(length))


def random_password(length=12):
    """Generate a random password"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for i in range(length))


def generate_workspace_identifiers(workspace_domain):
    """Generate unique identifiers for the workspace"""
    build_timestamp = int(time.time())
    workspace_id = str(uuid.uuid4())[:8]
    subdomain = generate_random_subdomain()
    namespace_name = f"workspace-{workspace_id}"
    fqdn = f"{subdomain}.{workspace_domain}"
    password = random_password()
    
    return {
        'workspace_id': workspace_id,
        'subdomain': subdomain,
        'namespace_name': namespace_name,
        'fqdn': fqdn,
        'build_timestamp': build_timestamp,
        'password': password
    }


def extract_workspace_config(data):
    """Extract and validate workspace configuration from request data"""
    github_urls = data.get('githubUrls', [])
    github_branches = data.get('githubBranches', [])

    if data.get('githubUrl') and not github_urls:
        github_urls = [data.get('githubUrl')]
        # Handle single branch for backward compatibility
        if data.get('githubBranch'):
            github_branches = [data.get('githubBranch')]
    
    if not github_urls:
        raise ValueError("At least one GitHub URL is required")
    
    # Ensure branches array matches URLs array length
    while len(github_branches) < len(github_urls):
        github_branches.append("")  # Empty string for default branch

    # Extract primary repo details
    primary_repo_url = github_urls[0].rstrip('/')
    repo_parts = primary_repo_url.split('/')
    repo_name = repo_parts[-1].replace('.git', '') if len(repo_parts) > 1 else "unknown"
    
    # Get custom image configuration
    custom_image = data.get('image', 'linuxserver/code-server:latest')
    custom_image_url = data.get('imageUrl', '')
    use_custom_image_url = bool(custom_image_url)
    use_dev_container = data.get('useDevContainer', True)

    # Get optional GitHub token
    github_token = data.get('githubToken', None)
        
    return {
        'github_urls': github_urls,
        'github_branches': github_branches,
        'primary_repo_url': primary_repo_url,
        'repo_name': repo_name,
        'custom_image': custom_image,
        'custom_image_url': custom_image_url,
        'use_custom_image_url': use_custom_image_url,
        'use_dev_container': use_dev_container,
        'github_token': github_token
    }