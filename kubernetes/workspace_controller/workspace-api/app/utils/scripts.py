def create_post_start_command():
    """Create the post-start command for Docker setup with explicit Debian/Ubuntu handling - runs in background"""
    return [
        "/bin/bash",
        "-c", 
        """
            # Create status file to track progress
            echo "STARTING" > /workspaces/setup-status
            
            # Run the entire setup in background
            nohup bash -c '
                exec > /workspaces/poststart.log 2>&1
                echo "Starting post-start initialization at $(date)"
                echo "RUNNING" > /workspaces/setup-status

                git config --global --add safe.directory /workspaces
                git config --global --add safe.directory /workspaces/*

                # Detect OS distribution
                if [ -f /etc/os-release ]; then
                    . /etc/os-release
                    OS=$ID
                    VERSION_CODENAME=$VERSION_CODENAME
                    echo "Detected OS: $OS $VERSION_CODENAME"
                else
                    echo "Cannot detect OS, assuming Ubuntu"
                    OS="ubuntu"
                    VERSION_CODENAME="focal"
                fi

                # Install common dependencies
                apt-get update -y || {
                    echo "WARNING: apt-get update failed, retrying with a delay"
                    sleep 5
                    apt-get update -y || echo "WARNING: apt-get update failed again, proceeding anyway"
                }
                apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release git tmux

                # Wait for code-server to be ready before installing extensions
                echo "Waiting for code-server to be ready..."
                timeout=60
                while ! pgrep -f "code-server" > /dev/null; do
                    echo "Waiting for code-server process to start..."
                    sleep 2
                    timeout=$((timeout - 1))
                    if [ $timeout -le 0 ]; then
                        echo "Timeout waiting for code-server"
                        break
                    fi
                done

                # Additional wait to ensure code-server is fully ready
                sleep 10

                # Check and install any extensions from devcontainer.json if not already installed
                if [ -f /workspaces/install-extensions.sh ] && [ -f /workspaces/.extensions-list ]; then
                    echo "Running extension installation script again to ensure all extensions are installed"
                    /workspaces/install-extensions.sh
                fi

                echo "Installing Docker for $OS $VERSION_CODENAME"
                
                # Function to check if Docker is running
                docker_running() {
                    docker info &>/dev/null
                }
                
                # Function to check if Docker CLI is installed
                docker_installed() {
                    command -v docker &>/dev/null
                }
                
                # Function to start Docker daemon
                start_docker_daemon() {
                    echo "Starting Docker daemon"
                    mkdir -p /var/run/docker
                    chown root:docker /var/run/docker
                    chmod 770 /var/run/docker
                    
                    # Check if dockerd is already running
                    if pgrep dockerd; then
                        echo "Docker daemon is already running"
                        return 0
                    fi
                    
                    # Start Docker daemon
                    dockerd \
                        --host=unix:///var/run/docker.sock \
                        --host=tcp://127.0.0.1:2376 \
                        --storage-driver=overlay2 \
                        --tls=false &
                    DOCKER_PID=$!
                    echo "Docker daemon started with PID: $DOCKER_PID"
                    
                    # Wait for Docker to start
                    timeout=30
                    while ! docker_running; do
                        echo "Waiting for docker to start..."
                        if [ $timeout -le 0 ]; then
                            echo "Docker daemon failed to start"
                            return 1
                        fi
                        timeout=$((timeout - 1))
                        sleep 1
                    done
                    
                    # Set proper permissions on Docker socket
                    echo "Setting Docker socket permissions"
                    chown root:docker /var/run/docker.sock
                    chmod 666 /var/run/docker.sock
                    
                    return 0
                }
                
                # Install Docker based on distribution
                if [ "$OS" = "debian" ]; then
                    # Debian-specific Docker installation
                    echo "Setting up Docker for Debian $VERSION_CODENAME"
                    
                    # Install Docker using Debian approach
                    install -m 0755 -d /etc/apt/keyrings
                    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
                    chmod a+r /etc/apt/keyrings/docker.gpg
                    
                    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $VERSION_CODENAME stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
                    
                    apt-get update -y
                    # Try to install Docker CE packages, with fallback
                    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || {
                        echo "Standard Docker packages failed to install for Debian, trying docker.io"
                        apt-get install -y docker.io
                    }
                elif [ "$OS" = "ubuntu" ]; then
                    # Ubuntu-specific Docker installation
                    echo "Setting up Docker for Ubuntu $VERSION_CODENAME"
                    
                    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
                    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
                    
                    apt-get update -y
                    apt-get install -y docker-ce docker-ce-cli containerd.io || {
                        echo "Standard Docker packages failed to install for Ubuntu, trying docker.io"
                        apt-get install -y docker.io
                    }
                else
                    # Fallback for other distributions
                    echo "Unknown distribution: $OS, attempting generic Docker installation"
                    apt-get install -y docker.io || {
                        echo "Could not install docker.io, trying snap"
                        apt-get install -y snapd
                        snap install docker
                    }
                fi

                echo "Docker installed. Checking version:"
                docker --version || echo "Docker command not found"
                
                # Setup Docker user and permissions
                echo "Setting up Docker group and permissions"
                groupadd -f docker
                getent passwd root > /dev/null && usermod -aG docker root
                getent passwd abc > /dev/null && usermod -aG docker abc
                getent passwd coder > /dev/null && usermod -aG docker coder
                getent passwd vscode > /dev/null && usermod -aG docker vscode
                
                # Start the Docker daemon if not already running
                if ! docker_running; then
                    start_docker_daemon
                fi
                
                # Verify Docker is working
                if docker_running; then
                    echo "Docker daemon started successfully"
                    # Pull a few common images to speed up future operations
                    echo "Pulling common Docker images in background"
                    docker pull hello-world &>/dev/null &
                    docker pull node:lts-slim &>/dev/null &
                    docker pull python:3-slim &>/dev/null &
                else
                    echo "WARNING: Docker daemon is not running properly"
                    echo "Trying to fix Docker setup..."
                    
                    # Try to fix Docker setup
                    pkill dockerd
                    sleep 2
                    rm -f /var/run/docker.pid
                    rm -f /var/run/docker.sock
                    
                    start_docker_daemon
                    
                    if docker_running; then
                        echo "Docker fixed and is now running"
                    else
                        echo "WARNING: Could not fix Docker, it may not be available"
                        echo "ERROR" > /workspaces/setup-status
                        exit 1
                    fi
                fi
                
                # Install Docker Compose if not already installed
                if ! command -v docker-compose &>/dev/null; then
                    echo "Installing Docker Compose"
                    mkdir -p /usr/local/lib/docker/cli-plugins
                    COMPOSE_VERSION="v2.24.6"
                    curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" -o /usr/local/bin/docker-compose
                    chmod +x /usr/local/bin/docker-compose
                    ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
                    echo "Docker Compose installed:"
                    docker-compose --version || echo "Docker Compose installation failed"
                fi
                
                # Run start-docker-compose command if it exists
                if [ -f "/workspaces/start-docker-compose.sh" ]; then
                    echo "Running docker-compose..."
                    /workspaces/start-docker-compose.sh
                fi

                cd /workspaces

                # Run post-create command if it exists
                if [ -f "/workspaces/post-create-command.sh" ]; then
                    echo "Running postCreateCommand..."
                    /workspaces/post-create-command.sh
                fi

                echo "Post-start initialization completed at $(date)"
                echo "COMPLETE" > /workspaces/setup-status
                
            ' > /workspaces/background-setup.log 2>&1 &
            
            echo "Background setup started. Check /workspaces/setup-status for progress."
            echo "Logs available at: /workspaces/poststart.log and /workspaces/background-setup.log"
        """
    ]


def generate_init_script(workspace_ids, workspace_config):
    """Generate the initialization bash script"""
    # Start with base script
    init_script = """#!/bin/bash
      set -e
      set -x

      git config --global --add safe.directory '*'
      
      # Ensure the workspace directory exists
      mkdir -p /workspaces

      # Change to the workspace directory
      cd /workspaces

      # Configure git to use GITHUB_TOKEN for private repos
      if [ ! -z "$GITHUB_TOKEN" ]; then
        echo "Using GITHUB_TOKEN for private repo access"
        
        git config --global user.name "$GITHUB_USERNAME"
        git config --global user.email "$GITHUB_USERNAME@users.noreply.github.com"
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
            git config --global user.name "$GITHUB_USERNAME"
            git config --global user.email "$GITHUB_USERNAME@users.noreply.github.com"
            git remote set-url origin https://$GITHUB_TOKEN@github.com/{owner}/{folder_name}.git
            cd ..
        fi
        """

    # Add custom image building section if required
    if workspace_config['use_custom_image_url']:
        init_script += generate_custom_image_script(workspace_ids, workspace_config)

    # Add standard initialization code
    init_script += generate_standard_init_code(repo_names)
    
    return init_script


def generate_custom_image_script(workspace_ids, workspace_config):
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
            else
                # It might be a direct file or directory URL
                # Convert github.com URLs to raw.githubusercontent.com if needed
                RAW_URL=$(echo "{workspace_config['custom_image_url']}" | sed 's|github.com|raw.githubusercontent.com|g' | sed 's|/blob/|/|g')
                curl -L "$RAW_URL" -o dockerfile.zip
                unzip dockerfile.zip
                rm dockerfile.zip
            fi
        else
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


def generate_standard_init_code(repo_names):
    """Generate standard initialization code common to all workspaces"""
    script = ""
    for repo_name in repo_names:
        script += f"""
git config --global --add safe.directory /workspaces/{repo_name}
"""

    script += """
# Set up git config if needed
git config --global --add safe.directory /workspaces

if [ ! -z "$GITHUB_USERNAME" ]; then
    echo "Using GITHUB_USERNAME"

    git config --global user.name "$GITHUB_USERNAME"
    git config --global user.email "$GITHUB_USERNAME@users.noreply.github.com"
fi

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


def generate_comprehensive_init_script(workspace_ids, workspace_config, aws_account_id):
    """Generate the comprehensive initialization script with devcontainer support"""
    
    # Get the basic init script
    init_script = generate_init_script(workspace_ids, workspace_config)
    
    # Add devcontainer processing
    repo_name = workspace_config['repo_name']
    
    init_script += f"""
    # Create directory for wrapper Dockerfile and user Dockerfile
    mkdir -p /workspaces/.code-server-wrapper
    mkdir -p /workspaces/.user-dockerfile
    cd /workspaces/.code-server-wrapper
    
    # Locate the user's Dockerfile in their repo
    USER_REPO_PATH="/workspaces/{repo_name}"
    DOCKERFILE_PATH="$USER_REPO_PATH/.devcontainer/Dockerfile"
    DEVCONTAINER_JSON_PATH="$USER_REPO_PATH/.devcontainer/devcontainer.json"
    
    # Debug info
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

    # Check for devcontainer.json
    if [ -f "$DEVCONTAINER_JSON_PATH" ]; then
        echo "DEBUG: devcontainer.json exists at $DEVCONTAINER_JSON_PATH"
        cat "$DEVCONTAINER_JSON_PATH" | head -n 20
    else
        echo "DEBUG: devcontainer.json does not exist at $DEVCONTAINER_JSON_PATH"
    fi
    
    # Check if the first repository actually got cloned
    if [ ! -d "$USER_REPO_PATH" ]; then
        echo "Repository not found at $USER_REPO_PATH, attempting to clone again"
        cd /workspaces
        
        # Get the branch for the first repository
        BRANCH="{workspace_config['github_branches'][0] if workspace_config['github_branches'] and workspace_config['github_branches'][0] else ''}"
        
        if [ ! -z "$BRANCH" ]; then
            echo "Cloning with specific branch: $BRANCH"
            git clone -b $BRANCH {workspace_config['github_urls'][0]} {repo_name}
        else
            echo "Cloning with default branch"
            git clone {workspace_config['github_urls'][0]} {repo_name}
        fi

        git config --global --add safe.directory /workspaces/{repo_name}
    fi
    
    # Check again after potential re-cloning
    if [ -f "$DOCKERFILE_PATH" ]; then
        echo "Found user Dockerfile at $DOCKERFILE_PATH"
        # Copy the user's Dockerfile
        cp "$DOCKERFILE_PATH" /workspaces/.user-dockerfile/Dockerfile
        
        # Process devcontainer.json if it exists
        if [ -f "$DEVCONTAINER_JSON_PATH" ]; then
            echo "Found devcontainer.json - processing configuration"
            
            # Copy the devcontainer.json file to the build context
            cp "$DEVCONTAINER_JSON_PATH" /workspaces/.user-dockerfile/
            
            # Install jq if needed for JSON processing
            if ! command -v jq &> /dev/null; then
                echo "Installing jq to parse devcontainer.json"
                apt-get update && apt-get install -y jq tmux
            fi
            
            # Process devcontainer.json content
            {_generate_devcontainer_processing_script(repo_name)}
        fi
        
        # Check if there's a docker-compose.yml file
        if [ -f "$USER_REPO_PATH/.devcontainer/docker-compose.yml" ]; then
            echo "Found docker-compose.yml - copying to build context"
            cp "$USER_REPO_PATH/.devcontainer/docker-compose.yml" /workspaces/.user-dockerfile/
        fi
        
        # Copy any other files in the .devcontainer directory
        if [ -d "$USER_REPO_PATH/.devcontainer" ]; then
            echo "Copying all files from .devcontainer directory"
            cp -r "$USER_REPO_PATH/.devcontainer/"* /workspaces/.user-dockerfile/
        fi
    else
        echo "Warning: No Dockerfile found at $DOCKERFILE_PATH"
        echo "Using default Go dev container image instead"
        echo "FROM mcr.microsoft.com/devcontainers/go:latest" > /workspaces/.user-dockerfile/Dockerfile
    fi
    
    # Create a wrapper Dockerfile that uses the user's image as a base
    cat > Dockerfile << 'EOF'
# This will be replaced with the tag for the user's custom image
FROM {aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-user-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}

RUN git config --global --add safe.directory /workspaces && \
    git config --global --add safe.directory '*'

# Install code-server
RUN curl -fsSL https://code-server.dev/install.sh | sh

# Expose default code-server port
EXPOSE 8444

# Set up entrypoint to run code-server
ENTRYPOINT ["/bin/bash", "-c", "if [ -f /workspaces/install-features.sh ]; then /workspaces/install-features.sh; fi && if [ -f /workspaces/setup-env.sh ]; then source /workspaces/setup-env.sh; fi && if [ -f /workspaces/install-extensions.sh ]; then /workspaces/install-extensions.sh; fi && if [ -f /workspaces/run-lifecycle.sh ]; then /workspaces/run-lifecycle.sh & fi && /usr/bin/code-server --bind-addr 0.0.0.0:8444 --auth password --user-data-dir /config/data --extensions-dir /config/extensions /workspaces"]
EOF
    
    # Create a flag file to indicate setup is done
    touch /workspaces/.code-server-initialized
    
    # Initialize workspace
    echo "Workspace initialization completed!"
    """
    
    return init_script


def _generate_devcontainer_processing_script(repo_name):
    """Generate the devcontainer.json processing script"""
    return f"""
            # Extract extensions from devcontainer.json (support both formats)
            EXTENSIONS=$(jq -r '.extensions[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ -z "$EXTENSIONS" ]; then
                EXTENSIONS=$(jq -r '.customizations.vscode.extensions[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            fi
            
            # Save extensions to file if found
            if [ ! -z "$EXTENSIONS" ]; then
                echo "Found extensions in devcontainer.json:"
                echo "$EXTENSIONS"
                echo "$EXTENSIONS" > /workspaces/.extensions-list
            else
                echo "No extensions found in devcontainer.json or couldn't parse"
            fi
            
            # Extract VS Code settings
            SETTINGS=$(jq -r '.settings // .customizations.vscode.settings // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$SETTINGS" ]; then
                echo "Found VS Code settings in devcontainer.json"
                mkdir -p /workspaces/.vscode
                echo "$SETTINGS" > /workspaces/.vscode/settings.json
            fi
            
            # Extract features
            FEATURES=$(jq -r '.features // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$FEATURES" ]; then
                echo "Found features in devcontainer.json:"
                echo "$FEATURES" > /workspaces/.devcontainer-features
                echo "Features will be installed during workspace initialization"
            fi
            
            # Extract port forwarding configuration
            PORTS=$(jq -r '.forwardPorts[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$PORTS" ]; then
                echo "Found ports to forward in devcontainer.json:"
                echo "$PORTS"
                echo "$PORTS" > /workspaces/.forward-ports
            fi
            
            # Extract other customizations
            CUSTOMIZATIONS=$(jq -r '.customizations // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$CUSTOMIZATIONS" ]; then
                echo "Found customizations in devcontainer.json"
                echo "$CUSTOMIZATIONS" > /workspaces/.customizations
            fi
            
            # Extract environment variables
            ENV_VARS=$(jq -r '.containerEnv // empty | to_entries[] | "\\(.key)=\\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$ENV_VARS" ]; then
                echo "Found environment variables in devcontainer.json:"
                echo "$ENV_VARS"
                echo "$ENV_VARS" > /workspaces/.container-env
            fi
            
            # Extract remote environment variables
            REMOTE_ENV_VARS=$(jq -r '.remoteEnv // empty | to_entries[] | "\\(.key)=\\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$REMOTE_ENV_VARS" ]; then
                echo "Found remote environment variables in devcontainer.json:"
                echo "$REMOTE_ENV_VARS"
                echo "$REMOTE_ENV_VARS" > /workspaces/.remote-env
            fi
            
            # Extract user configuration
            REMOTE_USER=$(jq -r '.remoteUser // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            CONTAINER_USER=$(jq -r '.containerUser // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            
            if [ ! -z "$REMOTE_USER" ] || [ ! -z "$CONTAINER_USER" ]; then
                echo "Found user configuration in devcontainer.json"
                
                if [ ! -z "$REMOTE_USER" ]; then
                    echo "remoteUser: $REMOTE_USER"
                    echo "REMOTE_USER=$REMOTE_USER" > /workspaces/.user-config
                fi
                
                if [ ! -z "$CONTAINER_USER" ]; then
                    echo "containerUser: $CONTAINER_USER"
                    echo "CONTAINER_USER=$CONTAINER_USER" >> /workspaces/.user-config
                fi
            fi
            
            # Extract lifecycle commands
            POST_CREATE_CMD=$(jq -r '.postCreateCommand // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$POST_CREATE_CMD" ]; then
                echo "Found postCreateCommand in devcontainer.json"
                echo "#!/bin/bash" > /workspaces/post-create-command.sh
                echo "$POST_CREATE_CMD" >> /workspaces/post-create-command.sh
                chmod +x /workspaces/post-create-command.sh
            fi
            
            POST_START_CMD=$(jq -r '.postStartCommand // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            if [ ! -z "$POST_START_CMD" ]; then
                echo "Found postStartCommand in devcontainer.json"
                echo "#!/bin/bash" > /workspaces/post-start-command.sh
                echo "$POST_START_CMD" >> /workspaces/post-start-command.sh
                chmod +x /workspaces/post-start-command.sh
            fi

            DOCKER_COMPOSE_FILE=$(jq -r '.dockerComposeFile // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            SERVICE_NAME=$(jq -r '.service // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
            WORKSPACE_FOLDER=$(jq -r '.workspaceFolder // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)

            if [ ! -z "$DOCKER_COMPOSE_FILE" ]; then
                echo "Found dockerComposeFile in devcontainer.json: $DOCKER_COMPOSE_FILE"
                echo "Service: $SERVICE_NAME"
                echo "Workspace folder: $WORKSPACE_FOLDER"
                
                # Save docker-compose configuration
                echo "{repo_name}/.devcontainer/$DOCKER_COMPOSE_FILE" > /workspaces/.docker-compose-file
                [ ! -z "$SERVICE_NAME" ] && echo "$SERVICE_NAME" > /workspaces/.docker-compose-service
                [ ! -z "$WORKSPACE_FOLDER" ] && echo "$WORKSPACE_FOLDER" > /workspaces/.docker-compose-workspace-folder
                
                echo "Docker Compose configuration will be started during workspace initialization"
            fi
    """


def generate_helper_scripts():
    """Generate helper scripts for docker-compose, extensions, etc."""
    return {
        "docker_compose_script": """#!/bin/bash
set -e

if [ ! -z "$GITHUB_TOKEN" ]; then
  echo "Using GITHUB_TOKEN for private repo access"

  git config --global user.name "$GITHUB_USERNAME"
  git config --global user.email "$GITHUB_USERNAME@users.noreply.github.com"
  git config --global url."https://$GITHUB_TOKEN@github.com/".insteadOf "https://github.com/"
else
  echo "No GITHUB_TOKEN found"
fi

COMPOSE_FILE_PATH="/workspaces/.docker-compose-file"
SERVICE_FILE_PATH="/workspaces/.docker-compose-service"
WORKSPACE_FOLDER_FILE="/workspaces/.docker-compose-workspace-folder"

if [ ! -f "$COMPOSE_FILE_PATH" ]; then
    echo "No docker-compose configuration found"
    exit 0
fi

DOCKER_COMPOSE_FILE=$(cat "$COMPOSE_FILE_PATH")
SERVICE_NAME=""
WORKSPACE_FOLDER="/workspaces"

#if [ -f "$SERVICE_FILE_PATH" ]; then
#    SERVICE_NAME=$(cat "$SERVICE_FILE_PATH")
#fi

if [ -f "$WORKSPACE_FOLDER_FILE" ]; then
    WORKSPACE_FOLDER=$(cat "$WORKSPACE_FOLDER_FILE")
fi

echo "Starting Docker Compose setup..."
echo "Compose file: $DOCKER_COMPOSE_FILE"
echo "Service: $SERVICE_NAME"
echo "Workspace folder: $WORKSPACE_FOLDER"

# Change to the workspace directory
cd /workspaces

# Find the docker-compose file (could be relative to .devcontainer or repo root)
COMPOSE_FILE_FULL_PATH=""

# Check in .devcontainer directory first
if [ -f ".devcontainer/$DOCKER_COMPOSE_FILE" ]; then
    COMPOSE_FILE_FULL_PATH=".devcontainer/$DOCKER_COMPOSE_FILE"
    cd /workspaces
elif [ -f "$DOCKER_COMPOSE_FILE" ]; then
    COMPOSE_FILE_FULL_PATH="$DOCKER_COMPOSE_FILE"
    cd /workspaces
else
    # Look for it in the first repository directory
    for repo_dir in */; do
        if [ -f "$repo_dir/.devcontainer/$DOCKER_COMPOSE_FILE" ]; then
            COMPOSE_FILE_FULL_PATH="$repo_dir/.devcontainer/$DOCKER_COMPOSE_FILE"
            cd "/workspaces/$repo_dir"
            break
        elif [ -f "$repo_dir/$DOCKER_COMPOSE_FILE" ]; then
            COMPOSE_FILE_FULL_PATH="$repo_dir/$DOCKER_COMPOSE_FILE"
            cd "/workspaces/$repo_dir"
            break
        fi
    done
fi

if [ -z "$COMPOSE_FILE_FULL_PATH" ]; then
    echo "ERROR: Could not find docker-compose file: $DOCKER_COMPOSE_FILE"
    exit 1
fi

echo "Found docker-compose file at: $COMPOSE_FILE_FULL_PATH"
echo "Working directory: $(pwd)"

# Ensure Docker is running
echo "Checking Docker daemon..."
timeout=30
while ! docker info >/dev/null 2>&1; do
    if [ $timeout -le 0 ]; then
        echo "ERROR: Docker daemon is not running"
        exit 1
    fi
    echo "Waiting for Docker daemon to start..."
    timeout=$((timeout - 1))
    sleep 1
done

echo "Docker daemon is ready"

# Start the docker-compose services
echo "Starting Docker Compose services..."

if [ ! -z "$SERVICE_NAME" ]; then
    echo "Starting specific service: $SERVICE_NAME"
    docker-compose -f "$COMPOSE_FILE_FULL_PATH" up -d "$SERVICE_NAME"
    
    # If this is a dev container setup, we might want to exec into the service
    echo "Docker Compose service '$SERVICE_NAME' is running"
    
    # Optional: Get the container ID for the service
    CONTAINER_ID=$(docker-compose -f "$COMPOSE_FILE_FULL_PATH" ps -q "$SERVICE_NAME")
    if [ ! -z "$CONTAINER_ID" ]; then
        echo "Service container ID: $CONTAINER_ID"
        echo "$CONTAINER_ID" > /workspaces/.service-container-id
        
        # You could potentially exec into this container or forward ports
        echo "Service is accessible via container: $CONTAINER_ID"
    fi
else
    echo "Starting all services"
    docker-compose -f "$COMPOSE_FILE_FULL_PATH" up -d
fi

# Show running containers
echo "Docker Compose services status:"
docker-compose -f "$COMPOSE_FILE_FULL_PATH" ps

# Save the compose file path for later use
echo "$COMPOSE_FILE_FULL_PATH" > /workspaces/.active-compose-file
echo "$(pwd)" > /workspaces/.compose-working-directory

echo "Docker Compose startup completed successfully"
""",
        
        "extension_install_script": """#!/bin/bash
EXTENSIONS_FILE="/workspaces/.extensions-list"

if [ -f "$EXTENSIONS_FILE" ]; then
  echo "Installing extensions from devcontainer.json..."

  # Set the correct extension directory for the web UI
  export VSCODE_EXTENSIONS="/config/extensions"
  export CODE_SERVER_EXTENSIONS_DIR="/config/extensions"
  
  # Ensure directories exist with proper permissions
  mkdir -p /config/extensions
  mkdir -p /config/data/User
  mkdir -p /config/data/logs
  
  # Create cache directory for extensions
  mkdir -p /workspaces/.vscode-extensions-cache
  
  # Function to install extension properly
  install_extension() {
    local extension="$1"
    echo "Installing extension: $extension"
    
    # Install using the web-based code-server with explicit paths
    if /usr/bin/code-server \
        --extensions-dir /config/extensions \
        --user-data-dir /config/data \
        --install-extension "$extension" 2>&1 | tee -a /workspaces/extension-install.log; then
      echo "Successfully installed: $extension"
      return 0
    else
      echo "Failed to install: $extension, trying alternative method..."
      
      # Alternative: try with different approach
      if code-server \
          --extensions-dir=/config/extensions \
          --user-data-dir=/config/data \
          --install-extension="$extension"; then
        echo "Successfully installed with alternative method: $extension"
        return 0
      else
        echo "Failed to install with all methods: $extension"
        return 1
      fi
    fi
  }
  
  # Install all extensions
  while IFS= read -r extension; do
    if [ ! -z "$extension" ]; then
      extension=$(echo "$extension" | tr -d '"' | tr -d "'" | xargs)
      install_extension "$extension"
      sleep 3  # Longer delay between installations
    fi
  done < "$EXTENSIONS_FILE"
  
  # Force refresh of extension cache
  rm -rf /config/data/CachedExtensions
  rm -rf /config/data/logs/extension-host*
  
  # List extensions from the correct directory
  echo "=== Extension Installation Report ==="
  echo "Extensions in /config/extensions:"
  ls -la /config/extensions/ || echo "No extensions directory found"
  echo "Extensions reported by code-server:"
  /usr/bin/code-server --extensions-dir /config/extensions --user-data-dir /config/data --list-extensions || echo "Could not list extensions"
  echo "========================================="
else
  echo "No extensions list found"
fi
""",
        
        "env_setup_script": """#!/bin/bash
# Apply container environment variables
if [ -f "/workspaces/.container-env" ]; then
  echo "Applying container environment variables"
  while IFS= read -r env_var; do
    if [ ! -z "$env_var" ]; then
      export "$env_var"
      echo "Exported: $env_var"
    fi
  done < "/workspaces/.container-env"
fi

# Apply remote environment variables
if [ -f "/workspaces/.remote-env" ]; then
  echo "Applying remote environment variables"
  while IFS= read -r env_var; do
    if [ ! -z "$env_var" ]; then
      export "$env_var"
      echo "Exported: $env_var"
    fi
  done < "/workspaces/.remote-env"
fi

# Apply user configuration
if [ -f "/workspaces/.user-config" ]; then
  echo "Applying user configuration from devcontainer.json"
  source /workspaces/.user-config
  
  # Note: Full user switching would require more complex handling
  # This is just setting environment variables for reference
  if [ ! -z "$REMOTE_USER" ]; then
    echo "Remote user set to: $REMOTE_USER"
  fi
  
  if [ ! -z "$CONTAINER_USER" ]; then
    echo "Container user set to: $CONTAINER_USER"
  fi
fi
""",
        
        "lifecycle_script": """#!/bin/bash
cd /workspaces

# Set up VS Code settings if they exist
if [ -f "/workspaces/.vscode/settings.json" ]; then
  echo "Applying VS Code settings..."
  mkdir -p /config/data/User
  cp /workspaces/.vscode/settings.json /config/data/User/settings.json
fi

# Handle port forwarding if configured
if [ -f "/workspaces/.forward-ports" ]; then
  echo "Setting up port forwarding..."
  # Note: The actual port forwarding is handled by the Kubernetes ingress
  # This is just for informational purposes
  cat /workspaces/.forward-ports
fi
"""
    }