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
    
    repo_url = workspace_config.get('github_urls', [""])[0]
    repo_name = repo_url.split('/')[-1].replace('.git', '')
    branch = workspace_config.get('github_branches', ["main"])[0]

    init_script = [f"""#!/bin/bash
set -e

# Initialize error counter
ERROR_COUNT=0

# Create necessary directories
mkdir -p /workspaces/.user-dockerfile
cd /workspaces

# Clone repository first
echo "Cloning repository {repo_url}..."
if [ ! -d "{repo_name}" ]; then
    if [ ! -z "{branch}" ]; then
        echo "Cloning with branch: {branch}"
        git clone -b {branch} "{repo_url}" "{repo_name}"
    else
        echo "Cloning with default branch"
        git clone "{repo_url}" "{repo_name}"
    fi

    if [ $? -eq 0 ]; then
        echo "Repository cloned successfully"
        git config --global --add safe.directory "/workspaces/{repo_name}"
    else
        echo "Failed to clone repository"
        ((ERROR_COUNT++))
    fi
fi

# Set up paths
USER_REPO_PATH="/workspaces/{repo_name}"
DOCKERFILE_PATH="$USER_REPO_PATH/.devcontainer/Dockerfile"
DEVCONTAINER_JSON_PATH="$USER_REPO_PATH/.devcontainer/devcontainer.json"

# Process devcontainer configuration
if [ -d "$USER_REPO_PATH" ]; then
    echo "Repository exists at $USER_REPO_PATH"
    
    if [ -f "$DEVCONTAINER_JSON_PATH" ]; then
        echo "Found devcontainer.json, processing configuration"
        cp "$DEVCONTAINER_JSON_PATH" /workspaces/.user-dockerfile/
        
        # Install jq if needed
        if ! command -v jq &> /dev/null; then
            apt-get update && apt-get install -y jq tmux
        fi
        
        # Process and install features first
        FEATURES=$(jq -r '.features // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
        if [ ! -z "$FEATURES" ]; then
            echo "Found features in devcontainer.json"
            echo "$FEATURES" > /workspaces/.devcontainer-features
            
            # Install features immediately
            echo "Installing features..."
            if [ -f /workspaces/install-features.sh ]; then
                chmod +x /workspaces/install-features.sh
                /workspaces/install-features.sh
                
            fi
        fi
        
        # Process environment variables
        ENV_VARS=$(jq -r '.containerEnv // empty | to_entries[] | "\(.key)=\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
        if [ ! -z "$ENV_VARS" ]; then
            echo "$ENV_VARS" > /workspaces/.container-env
        fi
        
        # Process remote environment variables
        REMOTE_ENV_VARS=$(jq -r '.remoteEnv // empty | to_entries[] | "\(.key)=\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
        if [ ! -z "$REMOTE_ENV_VARS" ]; then
            echo "$REMOTE_ENV_VARS" > /workspaces/.remote-env
        fi
        
        # Process and run post-create command
        POST_CREATE=$(jq -r '.postCreateCommand // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
        if [ ! -z "$POST_CREATE" ]; then
            echo "Creating post-create command script"
            echo "#!/bin/bash" > /workspaces/post-create-command.sh
            echo "set -e" >> /workspaces/post-create-command.sh
            echo "#!/bin/bash" >> /workspaces/post-create-command.sh
            echo "$POST_CREATE" >> /workspaces/post-create-command.sh
            chmod +x /workspaces/post-create-command.sh
        fi
    else
        echo "No devcontainer.json found, using default Go image"
        echo "FROM mcr.microsoft.com/devcontainers/go:latest" > /workspaces/.user-dockerfile/Dockerfile
    fi
else
    echo "ERROR: Repository directory does not exist"
    ((ERROR_COUNT++))
fi

# Final status
echo "Workspace initialization completed with $ERROR_COUNT errors"
touch /workspaces/.pool-workspace-initialized
echo "[$(date)] Initialization finished"
"""
    ]
    init_script.extend([
    f"""
        # Create directory for wrapper Dockerfile and user Dockerfile
        mkdir -p /workspaces/
        cd /workspaces/

        # Locate the user's Dockerfile in their repo
        USER_REPO_PATH="{repo_name}"
        DOCKERFILE_PATH="$USER_REPO_PATH/.devcontainer/Dockerfile"
        DEVCONTAINER_JSON_PATH="$USER_REPO_PATH/.devcontainer/devcontainer.json"

        # Debugging and validation
        echo "DEBUG: Checking repository and Dockerfile"
        if [ -d "$USER_REPO_PATH" ]; then
            echo "DEBUG: Repository directory exists at $USER_REPO_PATH"
            ls -la "$USER_REPO_PATH"
        else
            echo "DEBUG: ERROR - Repository directory does not exist at $USER_REPO_PATH"
        fi

        if [ -d "$USER_REPO_PATH/.devcontainer" ]; then
            echo "DEBUG: .devcontainer directory exists"
            ls -la "$USER_REPO_PATH/.devcontainer"
        else
            echo "DEBUG: .devcontainer directory does not exist"
        fi

        if [ -f "$DOCKERFILE_PATH" ]; then
            echo "DEBUG: Dockerfile exists at $DOCKERFILE_PATH"
            cat "$DOCKERFILE_PATH" | head -n 10
        else
            echo "DEBUG: Dockerfile does not exist at $DOCKERFILE_PATH"
        fi

        if [ -f "$DEVCONTAINER_JSON_PATH" ]; then
            echo "DEBUG: devcontainer.json exists at $DEVCONTAINER_JSON_PATH"
            cat "$DEVCONTAINER_JSON_PATH" | head -n 20
        else
            echo "DEBUG: devcontainer.json does not exist at $DEVCONTAINER_JSON_PATH"
        fi

        # Clone repository if needed
        if [ ! -d "$USER_REPO_PATH" ]; then
            echo "Repository not found at $USER_REPO_PATH, attempting to clone again"
            cd /workspaces

            BRANCH="{workspace_config['github_branches'][0] if workspace_config['github_branches'] and workspace_config['github_branches'][0] else ''}"

            if [ ! -z "$BRANCH" ]; then
                echo "Cloning with specific branch: $BRANCH"
                git clone -b $BRANCH {workspace_config['github_urls'][0]} {workspace_config['repo_name']}
            else
                echo "Cloning with default branch"
                git clone {workspace_config['github_urls'][0]} {workspace_config['repo_name']}
            fi

            git config --global --add safe.directory /workspaces/{workspace_config['repo_name']}
        fi

        # Check again after potential re-cloning
        if [ -f "$DOCKERFILE_PATH" ]; then
            echo "Found user Dockerfile at $DOCKERFILE_PATH"
            cp "$DOCKERFILE_PATH" /workspaces/.user-dockerfile/Dockerfile

            if [ -f "$DEVCONTAINER_JSON_PATH" ]; then
                echo "Found devcontainer.json - processing configuration"
                cp "$DEVCONTAINER_JSON_PATH" /workspaces/.user-dockerfile/

                if ! command -v jq &> /dev/null; then
                    echo "Installing jq to parse devcontainer.json"
                    apt-get update && apt-get install -y jq tmux
                fi

                EXTENSIONS=$(jq -r '.extensions[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ -z "$EXTENSIONS" ]; then
                    EXTENSIONS=$(jq -r '.customizations.vscode.extensions[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                fi

                if [ ! -z "$EXTENSIONS" ]; then
                    echo "$EXTENSIONS" > /workspaces/.extensions-list
                fi

                SETTINGS=$(jq -r '.settings // .customizations.vscode.settings // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$SETTINGS" ]; then
                    mkdir -p /workspaces/.vscode
                    echo "$SETTINGS" > /workspaces/.vscode/settings.json
                fi

                FEATURES=$(jq -r '.features // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$FEATURES" ]; then
                    echo "$FEATURES" > /workspaces/.devcontainer-features
                fi

                PORTS=$(jq -r '.forwardPorts[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$PORTS" ]; then
                    echo "$PORTS" > /workspaces/.forward-ports
                fi

                CUSTOMIZATIONS=$(jq -r '.customizations // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$CUSTOMIZATIONS" ]; then
                    echo "$CUSTOMIZATIONS" > /workspaces/.customizations
                fi

                ENV_VARS=$(jq -r '.containerEnv // empty | to_entries[] | "\(.key)=\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$ENV_VARS" ]; then
                    echo "$ENV_VARS" > /workspaces/.container-env
                fi

                REMOTE_ENV_VARS=$(jq -r '.remoteEnv // empty | to_entries[] | "\(.key)=\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$REMOTE_ENV_VARS" ]; then
                    echo "$REMOTE_ENV_VARS" > /workspaces/.remote-env
                fi

                REMOTE_USER=$(jq -r '.remoteUser // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                CONTAINER_USER=$(jq -r '.containerUser // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)

                if [ ! -z "$REMOTE_USER" ]; then
                    echo "REMOTE_USER=$REMOTE_USER" > /workspaces/.user-config
                fi

                if [ ! -z "$CONTAINER_USER" ]; then
                    echo "CONTAINER_USER=$CONTAINER_USER" >> /workspaces/.user-config
                fi

                POST_CREATE_CMD=$(jq -r '.postCreateCommand // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$POST_CREATE_CMD" ]; then
                    echo "$POST_CREATE_CMD" > /workspaces/post-create-command.sh
                    chmod +x /workspaces/post-create-command.sh
                fi

                POST_START_CMD=$(jq -r '.postStartCommand // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$POST_START_CMD" ]; then
                    echo "$POST_START_CMD" > /workspaces/post-start-command.sh
                    chmod +x /workspaces/post-start-command.sh
                fi
            fi
        else
            echo "Warning: No Dockerfile found at $DOCKERFILE_PATH"
            echo "Using default Go dev container image instead"
            echo "FROM mcr.microsoft.com/devcontainers/go:latest" > /workspaces/.user-dockerfile/Dockerfile
        fi
        """
    ])
    # Add repository cloning commands
    for url, branch in zip(
        workspace_config.get('github_urls', []), 
        workspace_config.get('github_branches', [])
    ):

        init_script.extend([
        # Function to handle devcontainer setup and feature installation
        'function setup_devcontainer() {',
        '    local dir="$1"',
        '    echo "Setting up devcontainer in $dir..."',
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
        '            # Install features immediately after finding them',
        '            echo "Installing devcontainer features..."',
        '            # Signal feature installation completion',
        '            touch /workspaces/.features-installed',
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
        "    # Create an empty features file if none exists to prevent waiting",
        "    if [ ! -f /workspaces/.devcontainer-features ]; then",
        "        echo '{}' > /workspaces/.devcontainer-features",
        "        touch /workspaces/.features-installed",
        "    fi",
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
        '# Final status update with detailed validation',
        'if [ $ERROR_COUNT -gt 0 ] || [ $VALIDATION_FAILED -gt 0 ]; then',
        '    MSG="Workspace initialization completed with $ERROR_COUNT dependency warnings and $VALIDATION_FAILED validation errors"',
        '    echo "$MSG"',
        'else',
        '    echo "Workspace initialization completed successfully"',
        'fi',
        '',
        '# Mark initialization as complete',
        'touch /workspaces/.pool-workspace-initialized',
        'echo "[$(date)] Initialization finished"',
    ])

    return "\n".join(init_script)


def _generate_workspace_script(workspace_ids, workspace_config):
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

