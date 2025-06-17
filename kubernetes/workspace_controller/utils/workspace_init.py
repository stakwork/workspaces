"""Utilities for generating workspace initialization scripts"""
import json
import logging

logger = logging.getLogger(__name__)

def generate_init_script(workspace_ids: dict, workspace_config: dict) -> str:
    """Generate the initialization script for a workspace
    
    Args:
        workspace_ids: Dictionary containing namespace_name and build_timestamp
        workspace_config: Dictionary containing github_urls, github_branches, github_pat, etc.
    """
    # Validate inputs
    if not workspace_ids.get('namespace_name'):
        raise ValueError("namespace_name is required in workspace_ids")
    
    init_script = [
        "#!/bin/bash",
        "set -e",
        
        # Required packages for devcontainer setup
        'echo "Installing required packages for devcontainer support..."',
        'apt-get update && apt-get install -y jq git-lfs >/dev/null 2>&1 || true',
        
        # Setup error handling and logging with more detail
        'function handle_error() {',
        '    local exit_code=$?',
        '    local line_no=$1',
        '    local command=$(sed -n "${line_no}p" "$0")',
        '    echo "Error occurred in script at line $line_no: \'$command\' exited with status $exit_code"',
        '    echo "Failed" > /workspaces/.init-status',
        '    exit $exit_code',
        '}',
        'trap \'handle_error ${LINENO}\' ERR',
        
        # Setup status reporting
        'function update_status() {',
        '    echo "$1" > /workspaces/.init-status',
        '    echo "[$(date)] Status: $1" >> /workspaces/logs/init.log',
        '}',
        
        # Create logging directory with timestamps
        "mkdir -p /workspaces/logs",
        "LOGFILE=/workspaces/logs/init.log",
        "exec 1> >(tee $LOGFILE)",
        "exec 2>&1",
        
        # Initialize status
        'update_status "Initializing"',
        "echo '[$(date)] Starting workspace initialization...'",
        
        # Wait for feature installation to complete
        'echo "Waiting for feature installation to complete..."',
        'FEATURE_TIMEOUT=300',
        'while [ ! -f /workspaces/.features-installed ] && [ $FEATURE_TIMEOUT -gt 0 ]; do',
        '    sleep 1',
        '    FEATURE_TIMEOUT=$((FEATURE_TIMEOUT - 1))',
        '    if [ $((FEATURE_TIMEOUT % 10)) -eq 0 ]; then',
        '        echo "Still waiting for feature installation... ${FEATURE_TIMEOUT}s remaining"',
        '    fi',
        'done',
        '',
        'if [ ! -f /workspaces/.features-installed ]; then',
        '    echo "ERROR: Feature installation timed out"',
        '    update_status "feature_install_timeout"',
        '    exit 1',
        'fi',
        '',
        'echo "Feature installation completed, proceeding with workspace setup"',
        
        # Save workspace IDs and config
        f"echo '{json.dumps(workspace_ids)}' > /workspaces/.workspace-ids",
        f"echo '{json.dumps(workspace_config)}' > /workspaces/.workspace-config",
        
        # Create working directory with proper permissions
        "mkdir -p workspaces",
        "cd workspaces",
        "chmod 755 /workspaces/",

        # Function to clone/update repos with retry
        'function clone_repo() {',
        '    local url="$1"',
        '    local branch="$2"',
        '    local retries=3',
        '    local repo_name=$(basename "$url" .git)',
        '    local clone_opts="--recurse-submodules"',
        '    ',
        '    echo "[$(date)] Cloning repository $repo_name from $url${branch:+ branch $branch}..."',
        '    update_status "cloning_repository"',
        '    ',
        '    # Check if repo already exists and is valid',
        '    if [ -d "$repo_name/.git" ]; then',
        '        echo "Repository exists, checking status..."',
        '        cd "$repo_name"',
        '        if git status &>/dev/null; then',
        '            echo "Repository is valid, updating..."',
        '            git fetch origin',
        '            if [ ! -z "$branch" ]; then',
        '                git reset --hard "origin/$branch"',
        '            else',
        '                git reset --hard origin/HEAD',
        '            fi',
        '            git submodule update --init --recursive',
        '            cd ..',
        '            return 0',
        '        fi',
        '        cd ..',
        '        echo "Repository is invalid, removing..."',
        '        rm -rf "$repo_name"',
        '    fi',

        '    # Clone with retries',
        '    for i in $(seq 1 $retries); do',
        '        echo "Clone attempt $i of $retries..."',
        '        if [ ! -z "$branch" ]; then',
        '            clone_opts="$clone_opts -b $branch"',
        '        fi',
        '        if git clone $clone_opts "$url" 2>&1; then',
        '            cd "$repo_name"',
        '            # Setup LFS if needed',
        '            if [ -f ".gitattributes" ] && grep -q "filter=lfs" .gitattributes; then',
        '                echo "LFS detected, pulling LFS files..."',
        '                git lfs pull',
        '            fi',
        '            cd ..',
        '            return 0',
        '        fi',
        '        echo "Clone failed, retrying in 5 seconds..."',
        '        sleep 5',
        '    done',
        '    echo "Failed to clone repository after $retries attempts"',
        '    return 1',
        '}',

        # Clone repositories
        "echo '[$(date)] Cloning repositories...'"
    ]

    # Add repository cloning commands
    for url, branch in zip(
        workspace_config.get('github_urls', []), 
        workspace_config.get('github_branches', [])
    ):
        init_script.append(f"clone_repo '{url}' '{branch}'")

    init_script.extend([
        # Workspace readiness checks
        "echo '[$(date)] Running workspace readiness checks...'",
        "update_status 'checking_readiness'",
        "ERROR_COUNT=0",

        # Function to handle devcontainer setup
        'function setup_devcontainer() {',
        '    local dir="$1"',
        '    echo "Setting up devcontainer in $dir..."',
        '    update_status "setting_up_devcontainer"',
        '',
        '    # Check for devcontainer configuration',
        '    if [ -f "$dir/.devcontainer/devcontainer.json" ]; then',
        '        echo "Found devcontainer configuration"',
        '        local config_file="$dir/.devcontainer/devcontainer.json"',
        '        ',
        '        # Parse and apply features',
        '        if jq -e .features "$config_file" >/dev/null 2>&1; then',
        '            echo "Setting up devcontainer features..."',
        '            jq -r .features "$config_file" > /workspaces/.devcontainer-features',
        '        fi',
        '',
        '        # Parse and apply extensions',
        '        if jq -e .customizations.vscode.extensions "$config_file" >/dev/null 2>&1; then',
        '            echo "Configuring VS Code extensions..."',
        '            jq -r .customizations.vscode.extensions[] "$config_file" > /workspaces/.extensions-list',
        '        fi',
        '',
        '        # Parse and apply environment variables',
        '        if jq -e .containerEnv "$config_file" >/dev/null 2>&1; then',
        '            echo "Setting up environment variables..."',
        '            jq -r "to_entries | .[] | .key + \"=\" + .value" "$config_file" > /workspaces/.container-env',
        '        fi',
        '',
        '        # Check for and run dockerfile commands if specified',
        '        if jq -e .dockerFile "$config_file" >/dev/null 2>&1; then',
        '            echo "Found Dockerfile configuration, processing..."',
        '            local dockerfile=$(jq -r .dockerFile "$config_file")',
        '            local context_path="$dir/.devcontainer"',
        '            if [ -f "$context_path/$dockerfile" ]; then',
        '                echo "Building from Dockerfile..."',
        '                docker build -t devcontainer-image "$context_path" -f "$context_path/$dockerfile" || echo "Warning: Dockerfile build failed"',
        '            fi',
        '        fi',
        '',
        '        # Check for postCreateCommand',
        '        if jq -e .postCreateCommand "$config_file" >/dev/null 2>&1; then',
        '            echo "Running post-create commands..."',
        '            # Source cargo and other environment setups',
        '            source ~/.cargo/env 2>/dev/null || true',
        '            source ~/.profile 2>/dev/null || true',
        '            source ~/.bashrc 2>/dev/null || true',
        '            local cmd=$(jq -r .postCreateCommand "$config_file")',
        '            eval "$cmd" || echo "Warning: postCreateCommand failed"',
        '        fi',
        '    fi',
        '}',
        '',
        '# Check each repository',
        "for d in */; do",
        "    cd \"$d\"",
        "    # Setup devcontainer first",
        "    setup_devcontainer \"$(pwd)\"",
        "",
        "    # Check node_modules",
        "    if [ -f 'package.json' ] && [ ! -d 'node_modules' ]; then",
        "        echo \"Installing node modules in $d...\"",
        "        npm install --silent || echo \"Warning: npm install failed in $d\"",
        "        ((ERROR_COUNT++))",
        "    fi",
        "    # Check Python requirements",
        "    if [ -f 'requirements.txt' ]; then",
        "        echo \"Installing Python dependencies in $d...\"",
        "        python3 -m pip install -r requirements.txt --quiet || echo \"Warning: pip install failed in $d\"",
        "        ((ERROR_COUNT++))",
        "    fi",
        "    # Check Git LFS files",
        "    if [ -f '.gitattributes' ] && grep -q \"filter=lfs\" .gitattributes; then",
        "        if ! git lfs ls-files | grep -q .; then",
        "            echo \"Warning: LFS files not properly pulled in $d\"",
        "            git lfs pull || echo \"Warning: git lfs pull failed in $d\"",
        "            ((ERROR_COUNT++))",
        "        fi",
        "    fi",
        "    cd ..",
        "done",

        # Validate devcontainer setup
        'echo "Validating workspace setup..."',
        'VALIDATION_FAILED=0',
        '',
        '# Check if devcontainer features were properly configured',
        'if [ -f /workspaces/.devcontainer-features ]; then',
        '    if ! jq . /workspaces/.devcontainer-features >/dev/null 2>&1; then',
        '        echo "Warning: Invalid devcontainer features configuration"',
        '        ((VALIDATION_FAILED++))',
        '    fi',
        'fi',
        '',
        '# Check if all required VS Code extensions are available',
        'if [ -f /workspaces/.extensions-list ]; then',
        '    while IFS= read -r ext; do',
        '        if ! code-server --list-extensions | grep -q "$ext"; then',
        '            echo "Warning: Extension $ext not properly installed"',
        '            ((VALIDATION_FAILED++))',
        '        fi',
        '    done < /workspaces/.extensions-list',
        'fi',
        '',
        '# Check environment variables',
        'if [ -f /workspaces/.container-env ]; then',
        '    if [ ! -f ~/.config/code-server/env ]; then',
        '        echo "Warning: Environment variables not properly configured"',
        '        ((VALIDATION_FAILED++))',
        '    fi',
        'fi',
        '',
        '# Final status update with detailed validation',
        'if [ $ERROR_COUNT -gt 0 ] || [ $VALIDATION_FAILED -gt 0 ]; then',
        '    MSG="Workspace initialization completed with $ERROR_COUNT dependency warnings and $VALIDATION_FAILED validation errors"',
        '    echo "$MSG"',
        '    update_status "ready_with_warnings|$MSG"',
        'else',
        '    echo "Workspace initialization completed successfully"',
        '    update_status "ready"',
        'fi',
        '',
        '# Mark initialization as complete',
        'touch /workspaces/.pool-workspace-initialized',
        'echo "[$(date)] Initialization finished"',
        '',
        '# Additional workspace metadata',
        'cat > /workspaces/.workspace-metadata << EOF',
        '{',
        '  "lastInitialized": "$(date -Iseconds)",',
        '  "initializationStatus": "$(cat /workspaces/.init-status)",',
        '  "features": $([ -f /workspaces/.devcontainer-features ] && cat /workspaces/.devcontainer-features || echo "{}")',
        '}',
        'EOF'
    ])

    return "\n".join(init_script)


def _generate_init_script(workspace_ids, workspace_config):
    """Generate the initialization bash script"""
    # Start with base script
    init_script = """#!/bin/bash
      set -e
      set -x
      
      # Ensure the workspace directory exists
      mkdir -p /workspaces

      # Change to the workspace directory
      cd /workspaces

      # Configure git to use GITHUB_TOKEN for private repos
      if [ ! -z "$GITHUB_TOKEN" ]; then
        echo "Using GITHUB_TOKEN for private repo access"
        git config --global url."https://$GITHUB_TOKEN@github.com/".insteadOf "https://github.com/"
      fi
    """

    repo_names = []

    # Add repository clone commands
    for i, repo_url in enumerate(workspace_config['github_urls']):
        # Extract repo name from the URL
        repo_name_parts = repo_url.rstrip('/').split('/')
        folder_name = repo_name_parts[-1].replace('.git', '') if len(repo_name_parts) > 1 else f"repo-{i}"
        owner = repo_name_parts[-2] if len(repo_name_parts) > 2 else "unknown-owner"

        repo_names.append(folder_name)

        branch = workspace_config['github_branches'][i] if i < len(workspace_config['github_branches']) else ""
            
        if branch:
            init_script += f"""
        # Clone repository {i+1}: {repo_url} (branch: {branch})
        if [ ! -d "/workspaces/{folder_name}" ]; then
            echo "Cloning {repo_url} branch {branch} into {folder_name}..."
            git clone -b {branch} {repo_url} {folder_name}
        fi

        # Mark repo as safe
        git config --global --add safe.directory "/workspaces/{folder_name}"
        """
        else:
            init_script += f"""
        # Clone repository {i+1}: {repo_url} (default branch)
        if [ ! -d "/workspaces/{folder_name}" ]; then
            echo "Cloning {repo_url} into {folder_name}..."
            git clone {repo_url} {folder_name}
        fi

        # Mark repo as safe
        git config --global --add safe.directory "/workspaces/{folder_name}"
        """

        # 🔐 Set the remote URL with GITHUB_TOKEN
        init_script += f"""
        # Set Git remote URL to use GITHUB_TOKEN
        if [ ! -z "$GITHUB_TOKEN" ]; then
            cd /workspaces/{folder_name}
            git remote set-url origin https://$GITHUB_TOKEN@github.com/{owner}/{folder_name}.git
            cd ..
        fi
        """

    # Add custom image building section if required
    if workspace_config['use_custom_image_url']:
        init_script += _generate_custom_image_script(workspace_ids, workspace_config)

    # Add standard initialization code
    init_script += _generate_standard_init_code(repo_names)
    
    return init_script

def _generate_standard_init_code(repo_names):
    """Generate standard initialization code common to all workspaces"""
    script = ""
    for repo_name in repo_names:
        script += f"""
git config --global --add safe.directory /workspaces/{repo_name}
"""

    script += """
# Set up git config if needed
git config --global --add safe.directory /workspaces

git config --global user.email "user@example.com"
git config --global user.name "Code Server User"

# Create Docker helper scripts for the user
cat > /workspaces/docker-info.sh << 'EOF'
#!/bin/bash
echo "Docker is available as a separate daemon inside this container."
echo "The Docker daemon starts automatically and is ready to use."
echo "You can verify it's working by running: docker info"
EOF
chmod +x /workspaces/docker-info.sh

# Create a custom .bashrc extension with Docker information
cat > /workspaces/.bash_docker << 'EOF'
#!/bin/bash

# Display Docker status on login
if command -v docker &> /dev/null; then
    if docker info &>/dev/null; then
        echo "🐳 Docker daemon is running and ready to use!"
        echo "Try running 'docker run hello-world' to test it."
    else
        echo "⚠️ Docker CLI is installed but the daemon isn't responding."
        echo "The daemon may still be starting up. Try again in a moment."
    fi
else
    echo "⚠️ Docker CLI is not installed. Something went wrong with the setup."
fi

# Add Docker-related aliases
alias d='docker'
alias dc='docker-compose'
alias dps='docker ps'
alias di='docker images'
EOF

      touch /workspaces/.code-server-initialized

      # Initialize workspace
      echo "Workspace initialized successfully!"
  """
    
    return script


def _generate_custom_image_script(workspace_ids, workspace_config):
    """Generate script for custom image handling"""
    return f"""
        # Create directory for custom image
        mkdir -p /workspaces/.custom-image
        cd /workspaces/.custom-image
        
        # Download custom image configuration
        echo "Downloading custom image configuration from {workspace_config['custom_image_url']}..."
        if [[ "{workspace_config['custom_image_url']}" == *github* ]]; then
            # If it's a GitHub URL, use special handling
            if [[ "{workspace_config['custom_image_url']}" == *.git ]]; then
                # It's a Git repository
                git clone {workspace_config['custom_image_url']} .
            else:
                # It might be a direct file or directory URL
                # Convert github.com URLs to raw.githubusercontent.com if needed
                RAW_URL=$(echo "{workspace_config['custom_image_url']}" | sed 's|github.com|raw.githubusercontent.com|g' | sed 's|/blob/|/|g')
                curl -L "$RAW_URL" -o dockerfile.zip
                unzip dockerfile.zip
                rm dockerfile.zip
            fi
        else:
            # Regular URL to a file
            curl -L "{workspace_config['custom_image_url']}" -o image-config.zip
            unzip image-config.zip
            rm image-config.zip
        fi
        
        # Check if there's a Dockerfile
        if [ ! -f "Dockerfile" ]; then
            echo "Error: No Dockerfile found in the downloaded configuration"
            echo "Using default image instead: linuxserver/code-server:latest"
            touch /workspaces/.use-default-image
        fi
    """
