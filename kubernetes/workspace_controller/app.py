import json
import base64
import socket
import os
import json
import uuid
import yaml
import string
import random
import logging
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
from kubernetes import client, config
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    # Load in-cluster config
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes configuration")
except config.config_exception.ConfigException:
    # Load kubeconfig for local development
    config.load_kube_config()
    logger.info("Loaded kubeconfig for local development")

# Initialize Kubernetes clients
core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
networking_v1 = client.NetworkingV1Api()

# Get domain from config map
try:
    config_map = core_v1.read_namespaced_config_map("workspace-config", "workspace-system")
    DOMAIN = config_map.data.get("domain", "SUBDOMAIN_REPLACE_ME")
    PARENT_DOMAIN = config_map.data.get("parent-domain", "REPLACE_ME")
    WORKSPACE_DOMAIN = config_map.data.get("workspace-domain", "SUBDOMAIN_REPLACE_ME")
    logger.info(f"Using domain: {DOMAIN}, parent domain: {PARENT_DOMAIN}, workspace domain: {WORKSPACE_DOMAIN}")
except Exception as e:
    logger.error(f"Error reading config map: {e}")
    DOMAIN = "SUBDOMAIN_REPLACE_ME"
    PARENT_DOMAIN = "REPLACE_ME"
    WORKSPACE_DOMAIN = "SUBDOMAIN_REPLACE_ME"

def generate_random_subdomain(length=8):
    """Generate a random subdomain name"""
    letters = string.ascii_lowercase + string.digits
    return ''.join(random.choice(letters) for i in range(length))

def random_password(length=12):
    """Generate a random password"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for i in range(length))

@app.route('/api/workspaces', methods=['GET'])
def list_workspaces():
    """List all workspaces"""
    workspaces = []
    
    try:
        # Get all namespaces with the workspace label
        namespaces = core_v1.list_namespace(label_selector="app=workspace")
        
        for ns in namespaces.items:
            try:
                # Get workspace info from config map
                config_maps = core_v1.list_namespaced_config_map(ns.metadata.name, label_selector="app=workspace-info")
                if not config_maps.items:
                    continue
                    
                workspace_info = json.loads(config_maps.items[0].data.get("info", "{}"))
                
                # Don't expose password
                if "password" in workspace_info:
                    workspace_info["password"] = "********"
                    
                # Get pods to determine state
                pods = core_v1.list_namespaced_pod(ns.metadata.name, label_selector="app=code-server")
                if pods.items:
                    if pods.items[0].status.phase == "Running":
                        workspace_info["state"] = "running"
                    else:
                        workspace_info["state"] = pods.items[0].status.phase.lower()
                else:
                    workspace_info["state"] = "unknown"
                    
                workspaces.append(workspace_info)
            except Exception as e:
                logger.error(f"Error getting workspace info from namespace {ns.metadata.name}: {e}")
                continue
    except Exception as e:
        logger.error(f"Error listing workspaces: {e}")
        return jsonify({"error": str(e)}), 500
        
    return jsonify({"workspaces": workspaces})

@app.route('/api/workspaces', methods=['POST'])
def create_workspace():
    """Create a new workspace"""
    # Extract and validate request data
    workspace_config = _extract_workspace_config(request.json)
    
    # Generate workspace identifiers
    workspace_ids = _generate_workspace_identifiers()
    
    try:
        # Create the namespace
        _create_namespace(workspace_ids)
        
        # Create storage and credentials
        _create_persistent_volume_claim(workspace_ids)
        _create_workspace_secret(workspace_ids)
        
        # Create initialization scripts
        _create_init_script_configmap(workspace_ids, workspace_config)
        _create_workspace_info_configmap(workspace_ids, workspace_config)

        # Copy required ConfigMaps and Secrets
        _copy_port_detector_configmap(workspace_ids)
        _copy_wildcard_certificate(workspace_ids)
        
        # Create Kubernetes resources
        _create_deployment(workspace_ids, workspace_config)
        _create_service(workspace_ids)
        _create_ingress(workspace_ids)
        
        return jsonify({
            "success": True,
            "message": "Workspace creation initiated",
            "workspace": _get_workspace_info(workspace_ids, workspace_config)
        })
        
    except Exception as e:
        logger.error(f"Error creating workspace: {e}")
        # Try to clean up if something went wrong
        try:
            core_v1.delete_namespace(workspace_ids['namespace_name'])
        except:
            pass
        return jsonify({"error": str(e)}), 500

def _create_post_start_command():
    """Create the post-start command for Docker setup with explicit Debian/Ubuntu handling"""
    return [
        "/bin/bash",
        "-c", 
        """
            exec > /workspaces/poststart.log 2>&1
            echo "Starting post-start initialization at $(date)"

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
            apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release git

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
                dockerd --host=unix:///var/run/docker.sock --host=tcp://0.0.0.0:2375 --storage-driver=overlay2 &
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
            
            # Execute feature installation if the script exists
            if [ -f /workspaces/install-features.sh ]; then
                echo "Running feature installation script"
                /workspaces/install-features.sh
            else
                echo "No feature installation script found"
            fi
            
            echo "Post-start initialization completed at $(date)"
        """
    ]

def _create_port_detector_container():
    """Create the port detector container"""
    return client.V1Container(
        name="port-detector",
        image="ubuntu:22.04",
        command=["/bin/bash", "/scripts/port-detector.sh"],
        security_context=client.V1SecurityContext(
            run_as_user=0  # Run as root to install packages
        ),
        volume_mounts=[
            client.V1VolumeMount(
                name="port-detector-script",
                mount_path="/scripts"
            )
        ]
    )


def _create_service(workspace_ids):
    """Create service for the code-server"""
    service = client.V1Service(
        metadata=client.V1ObjectMeta(
            name="code-server",
            namespace=workspace_ids['namespace_name'],
            labels={"app": "workspace"}
        ),
        spec=client.V1ServiceSpec(
            selector={"app": "code-server"},
            ports=[
                client.V1ServicePort(
                    name="code-server-port",
                    port=8443,
                    target_port=8443
                )
            ]
        )
    )
    core_v1.create_namespaced_service(workspace_ids['namespace_name'], service)
    logger.info(f"Created service in namespace: {workspace_ids['namespace_name']}")


def _create_ingress(workspace_ids):
    """Create ingress for the code-server"""
    ingress = client.V1Ingress(
        metadata=client.V1ObjectMeta(
            name="code-server",
            namespace=workspace_ids['namespace_name'],
            labels={"app": "workspace"},
            annotations={
                "kubernetes.io/ingress.class": "nginx",
                "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
                "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600"
            }
        ),
        spec=client.V1IngressSpec(
            tls=[
                client.V1IngressTLS(
                    hosts=[workspace_ids['fqdn']],
                    secret_name="workspace-domain-wildcard-tls"
                )
            ],
            rules=[
                client.V1IngressRule(
                    host=workspace_ids['fqdn'],
                    http=client.V1HTTPIngressRuleValue(
                        paths=[
                            client.V1HTTPIngressPath(
                                path="/",
                                path_type="Prefix",
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name="code-server",
                                        port=client.V1ServiceBackendPort(
                                            number=8443
                                        )
                                    )
                                )
                            )
                        ]
                    )
                )
            ]
        )
    )
    networking_v1.create_namespaced_ingress(workspace_ids['namespace_name'], ingress)
    logger.info(f"Created ingress in namespace: {workspace_ids['namespace_name']}")

def _extract_workspace_config(data):
    """Extract and validate workspace configuration from request data"""
    github_urls = data.get('githubUrls', [])
    if data.get('githubUrl') and not github_urls:
        github_urls = [data.get('githubUrl')]
    
    if not github_urls:
        raise ValueError("At least one GitHub URL is required")

    # Extract primary repo details
    primary_repo_url = github_urls[0].rstrip('/')
    repo_parts = primary_repo_url.split('/')
    repo_name = repo_parts[-1].replace('.git', '') if len(repo_parts) > 1 else "unknown"
    
    # Get custom image configuration
    custom_image = data.get('image', 'linuxserver/code-server:latest')
    custom_image_url = data.get('imageUrl', '')
    use_custom_image_url = bool(custom_image_url)
    use_dev_container = data.get('useDevContainer', True)
    
    if use_custom_image_url:
        logger.info(f"Custom image URL provided: {custom_image_url}")
    else:
        logger.info(f"Using specified Docker image: {custom_image}")
        if use_dev_container:
            logger.info(f"Using dev container mode with image: {custom_image}")
    
    return {
        'github_urls': github_urls,
        'primary_repo_url': primary_repo_url,
        'repo_name': repo_name,
        'custom_image': custom_image,
        'custom_image_url': custom_image_url,
        'use_custom_image_url': use_custom_image_url,
        'use_dev_container': use_dev_container
    }


def _generate_workspace_identifiers():
    """Generate unique identifiers for the workspace"""
    workspace_id = str(uuid.uuid4())[:8]
    subdomain = generate_random_subdomain()
    namespace_name = f"workspace-{workspace_id}"
    fqdn = f"{subdomain}.{WORKSPACE_DOMAIN}"
    password = random_password()
    
    return {
        'workspace_id': workspace_id,
        'subdomain': subdomain,
        'namespace_name': namespace_name,
        'fqdn': fqdn,
        'password': password
    }


def _create_namespace(workspace_ids):
    """Create the Kubernetes namespace for the workspace"""
    namespace = client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=workspace_ids['namespace_name'],
            labels={
                "app": "workspace",
                "workspaceId": workspace_ids['workspace_id'],
                "allowed-registry-access": "true"
            }
        )
    )
    core_v1.create_namespace(namespace)
    logger.info(f"Created namespace: {workspace_ids['namespace_name']}")


def _create_persistent_volume_claim(workspace_ids):
    """Create PVC for workspace data"""
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name="workspace-data",
            namespace=workspace_ids['namespace_name'],
            labels={"app": "workspace"}
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],
            resources=client.V1ResourceRequirements(
                requests={"storage": "10Gi"}
            ),
            storage_class_name="efs-sc"
        )
    )
    core_v1.create_namespaced_persistent_volume_claim(workspace_ids['namespace_name'], pvc)
    logger.info(f"Created PVC in namespace: {workspace_ids['namespace_name']}")


def _create_workspace_secret(workspace_ids):
    """Create secret for workspace credentials"""
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="workspace-secret",
            namespace=workspace_ids['namespace_name'],
            labels={"app": "workspace"}
        ),
        string_data={
            "password": workspace_ids['password']
        }
    )
    core_v1.create_namespaced_secret(workspace_ids['namespace_name'], secret)
    logger.info(f"Created secret in namespace: {workspace_ids['namespace_name']}")

def _create_feature_installation_script():
    """Generate a script that handles common dev container features installation"""
    return """#!/bin/bash
# Script to install common dev container features
set -e

FEATURES_FILE="/workspaces/.devcontainer-features"
if [ ! -f "$FEATURES_FILE" ]; then
    echo "No features file found, skipping feature installation"
    exit 0
fi

echo "Installing dev container features from features file"
echo "Features content:"
cat "$FEATURES_FILE"

# Convert features file to JSON for easier parsing
FEATURES_JSON=$(cat "$FEATURES_FILE")

# Helper function to check if a feature exists
feature_exists() {
    echo "$FEATURES_JSON" | grep -q "\"$1\""
}

# Helper to extract feature version/options
get_feature_option() {
    local feature=$1
    local option=$2
    local default=$3
    
    # Try to extract the version or option using grep and sed
    # Format is typically "feature": { "version": "value", "optionName": "value" }
    result=$(echo "$FEATURES_JSON" | grep -o "\"$feature\"[^}]*" | grep -o "\"$option\"[^,}]*" | grep -o "\"[^\"]*\"$" | tr -d '"' || echo "")
    
    if [ -z "$result" ]; then
        echo "$default"
    else
        echo "$result"
    fi
}

# Install Docker feature
if feature_exists "docker"; then
    echo "Installing Docker feature"
    # Docker is already installed by the post-start script
    echo "✓ Docker already configured"
fi

# Install Docker-in-Docker feature (alternative to Docker)
if feature_exists "docker-in-docker" || feature_exists "docker-from-docker"; then
    echo "Installing Docker-in-Docker feature"
    # Docker is already installed by the post-start script
    echo "✓ Docker already configured via post-start script"
    
    # Add Docker Compose v2 if not already added
    if ! command -v docker-compose &> /dev/null; then
        echo "Installing Docker Compose v2"
        mkdir -p /usr/local/lib/docker/cli-plugins
        curl -SL "https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-$(uname -m)" -o /usr/local/lib/docker/cli-plugins/docker-compose
        chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
        ln -sf /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose
        echo "✓ Docker Compose installed: $(docker-compose version)"
    fi
fi

# Install Node.js feature
if feature_exists "node"; then
    echo "Installing Node.js feature"
    VERSION=$(get_feature_option "node" "version" "lts")
    
    # Install Node.js using NVM
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.3/install.sh | bash
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    
    if [ "$VERSION" = "lts" ] || [ "$VERSION" = "latest" ]; then
        nvm install --lts
    else
        nvm install "$VERSION"
    fi
    
    # Add NVM to shell initialization
    echo 'export NVM_DIR="$HOME/.nvm"' >> /etc/profile.d/nvm.sh
    echo '[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"' >> /etc/profile.d/nvm.sh
    
    echo "✓ Node.js $(node -v) installed"
fi

# Install Python feature
if feature_exists "python"; then
    echo "Installing Python feature"
    VERSION=$(get_feature_option "python" "version" "3.10")
    INSTALL_TOOLS=$(get_feature_option "python" "installTools" "true")
    INSTALL_JUPYTER=$(get_feature_option "python" "installJupyter" "false")
    
    # Install Python with apt
    apt-get update
    apt-get install -y python3 python3-pip python3-venv
    
    # Create symbolic links
    ln -sf /usr/bin/python3 /usr/bin/python
    
    echo "✓ Python $(python3 --version) installed"
    
    # Install common tools if requested
    if [ "$INSTALL_TOOLS" = "true" ]; then
        echo "Installing Python tools"
        pip3 install --no-cache-dir ipython pytest pylint flake8 black
        echo "✓ Common Python tools installed"
    fi
    
    # Install Jupyter if requested
    if [ "$INSTALL_JUPYTER" = "true" ]; then
        echo "Installing Jupyter"
        pip3 install --no-cache-dir jupyter notebook
        echo "✓ Jupyter installed"
    fi
fi

# Install Go feature
if feature_exists "go"; then
    echo "Installing Go feature"
    VERSION=$(get_feature_option "go" "version" "latest")
    
    if [ "$VERSION" = "latest" ]; then
        VERSION=$(curl -s https://go.dev/VERSION?m=text | head -n1)
    fi
    
    # Download and install Go
    curl -sSL "https://golang.org/dl/${VERSION}.linux-amd64.tar.gz" -o go.tar.gz
    tar -C /usr/local -xzf go.tar.gz
    rm go.tar.gz
    
    # Add Go to PATH
    echo 'export PATH=$PATH:/usr/local/go/bin' > /etc/profile.d/go.sh
    echo 'export PATH=$PATH:$HOME/go/bin' >> /etc/profile.d/go.sh
    
    # Set up environment for current session
    export PATH=$PATH:/usr/local/go/bin
    
    echo "✓ Go $(go version) installed"
fi

# Install Java feature
if feature_exists "java"; then
    echo "Installing Java feature"
    VERSION=$(get_feature_option "java" "version" "17")
    
    # Install OpenJDK
    apt-get update
    apt-get install -y openjdk-${VERSION}-jdk
    
    echo "✓ Java $(java -version 2>&1 | head -n 1) installed"
fi

# Install Rust feature
if feature_exists "rust"; then
    echo "Installing Rust feature"
    
    # Install Rust using rustup
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    
    # Add Rust to PATH
    echo 'export PATH=$PATH:$HOME/.cargo/bin' > /etc/profile.d/rust.sh
    
    # Set up environment for current session
    export PATH=$PATH:$HOME/.cargo/bin
    
    echo "✓ Rust $(rustc --version) installed"
fi

# Install .NET feature
if feature_exists "dotnet"; then
    echo "Installing .NET feature"
    VERSION=$(get_feature_option "dotnet" "version" "latest")
    
    # Install .NET SDK
    apt-get update
    apt-get install -y wget
    
    if [ "$VERSION" = "latest" ]; then
        wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh
        chmod +x dotnet-install.sh
        ./dotnet-install.sh
    else
        wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh
        chmod +x dotnet-install.sh
        ./dotnet-install.sh --version $VERSION
    fi
    
    # Add .NET to PATH
    echo 'export PATH=$PATH:$HOME/.dotnet' > /etc/profile.d/dotnet.sh
    
    # Set up environment for current session
    export PATH=$PATH:$HOME/.dotnet
    
    echo "✓ .NET installed"
fi

# Install PHP feature
if feature_exists "php"; then
    echo "Installing PHP feature"
    VERSION=$(get_feature_option "php" "version" "8.2")
    COMPOSER=$(get_feature_option "php" "composer" "true")
    
    # Install PHP
    apt-get update
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:ondrej/php
    apt-get update
    apt-get install -y php${VERSION} php${VERSION}-cli php${VERSION}-common php${VERSION}-curl php${VERSION}-mbstring php${VERSION}-mysql php${VERSION}-xml php${VERSION}-zip
    
    # Install Composer if requested
    if [ "$COMPOSER" = "true" ]; then
        echo "Installing Composer"
        curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer
        echo "✓ Composer installed: $(composer --version)"
    fi
    
    echo "✓ PHP installed: $(php -v | head -n 1)"
fi

# Install common utilities
if feature_exists "common-utils"; then
    echo "Installing common utilities"
    
    apt-get update
    apt-get install -y wget curl vim git jq unzip zip sudo 
    apt-get install -y build-essential pkg-config libssl-dev
    
    echo "✓ Common utilities installed"
fi

# Install GitHub CLI
if feature_exists "github-cli"; then
    echo "Installing GitHub CLI"
    
    # Install GitHub CLI
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt-get update
    apt-get install -y gh
    
    echo "✓ GitHub CLI $(gh --version | head -n 1) installed"
fi

# Install Azure CLI
if feature_exists "azure-cli"; then
    echo "Installing Azure CLI"
    
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash
    
    echo "✓ Azure CLI $(az --version | head -n 1) installed"
fi

# Install AWS CLI
if feature_exists "aws-cli"; then
    echo "Installing AWS CLI"
    
    apt-get update
    apt-get install -y unzip
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
    unzip awscliv2.zip
    ./aws/install
    rm -rf aws awscliv2.zip
    
    echo "✓ AWS CLI $(aws --version) installed"
fi

# Install Terraform
if feature_exists "terraform"; then
    echo "Installing Terraform"
    VERSION=$(get_feature_option "terraform" "version" "latest")
    
    apt-get update
    apt-get install -y gnupg software-properties-common curl
    
    curl -fsSL https://apt.releases.hashicorp.com/gpg | apt-key add -
    apt-add-repository "deb [arch=amd64] https://apt.releases.hashicorp.com $(lsb_release -cs) main"
    apt-get update
    
    if [ "$VERSION" = "latest" ]; then
        apt-get install -y terraform
    else
        apt-get install -y terraform=$VERSION
    fi
    
    echo "✓ Terraform $(terraform version | head -n 1) installed"
fi

# Install kubectl
if feature_exists "kubectl" || feature_exists "kubernetes-tools"; then
    echo "Installing kubectl"
    VERSION=$(get_feature_option "kubectl" "version" "latest")
    
    if [ "$VERSION" = "latest" ]; then
        VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt)
    fi
    
    curl -LO "https://dl.k8s.io/release/$VERSION/bin/linux/amd64/kubectl"
    chmod +x kubectl
    mv kubectl /usr/local/bin/
    
    echo "✓ kubectl $(kubectl version --client -o json | jq -r '.clientVersion.gitVersion') installed"
fi

echo "Feature installation completed"
"""

def _create_init_script_configmap(workspace_ids, workspace_config):
    """Create ConfigMap with initialization scripts"""
    # Start with base repository cloning script
    init_script = _generate_init_script(workspace_ids, workspace_config)
    
    namespace_name = workspace_ids['namespace_name']
    repo_name = workspace_config['repo_name']

    # Add code to create a wrapper Dockerfile that uses the user's Dockerfile as a base
    init_script += f"""
    # Create directory for wrapper Dockerfile and user Dockerfile
    mkdir -p /workspaces/.code-server-wrapper
    mkdir -p /workspaces/.user-dockerfile
    cd /workspaces/.code-server-wrapper
    
    # Locate the user's Dockerfile in their repo
    USER_REPO_PATH="/workspaces/{repo_name}"
    DOCKERFILE_PATH="$USER_REPO_PATH/.devcontainer/Dockerfile"
    DEVCONTAINER_JSON_PATH="$USER_REPO_PATH/.devcontainer/devcontainer.json"
    
    """
    
    # Continuing the script
    init_script += """
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
    """
    
    # Clone repository if needed
    init_script += f"""
    # Check if the first repository actually got cloned
    if [ ! -d "$USER_REPO_PATH" ]; then
        echo "Repository not found at $USER_REPO_PATH, attempting to clone again"
        cd /workspaces
        git clone {workspace_config['github_urls'][0]} {repo_name}
    fi
    """
    
    # Main processing script
    init_script += """
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
                apt-get update && apt-get install -y jq
            fi
            
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
            
            # Create a comprehensive extension installation script
            echo "Creating extension installation script"
            cat > /workspaces/install-extensions.sh << 'EOL'
#!/bin/bash
EXTENSIONS_FILE="/workspaces/.extensions-list"

if [ -f "$EXTENSIONS_FILE" ]; then
  echo "Installing extensions from devcontainer.json..."
  
  # Create cache directory for extensions
  mkdir -p /workspaces/.vscode-extensions-cache
  
  # Function to install extension with better error handling and caching
  install_extension() {
    local extension="$1"
    local extension_id="${extension##*/}"  # Get last part after slash for GitHub URLs
    local cache_file="/workspaces/.vscode-extensions-cache/${extension_id}.vsix"
    
    echo "Installing extension: $extension"
    
    # Check if it's a GitHub URL or extension ID
    if [[ "$extension" == *"github.com"* ]] || [[ "$extension" == *"http://"* ]] || [[ "$extension" == *"https://"* ]]; then
      # It's a URL, download it if not cached
      if [ ! -f "$cache_file" ]; then
        echo "Downloading extension from URL: $extension"
        if curl -sL "$extension" -o "$cache_file"; then
          echo "Download successful"
        else
          echo "Failed to download extension from URL"
          return 1
        fi
      else
        echo "Using cached extension file"
      fi
      
      # Install from downloaded file
      if code-server --install-extension "$cache_file"; then
        echo "Successfully installed extension from file: $extension_id"
        return 0
      else
        echo "Failed to install extension from file: $extension_id"
        # Delete cache file to force re-download next time
        rm -f "$cache_file"
        return 1
      fi
    else
      # Standard extension ID from marketplace
      # Try up to 3 times with increasing delays
      for i in 1 2 3; do
        if code-server --install-extension "$extension"; then
          echo "Successfully installed: $extension"
          return 0
        else
          echo "Attempt $i failed to install: $extension"
          if [ $i -lt 3 ]; then
            sleep_time=$((i * 5))
            echo "Retrying in $sleep_time seconds..."
            sleep $sleep_time
          else
            echo "Failed to install extension after 3 attempts: $extension"
            return 1
          fi
        fi
      done
    fi
    
    return 1
  }
  
  # Install all extensions
  while IFS= read -r extension; do
    if [ ! -z "$extension" ]; then
      # Trim any extra whitespace or quotes
      extension=$(echo "$extension" | tr -d '"' | tr -d "'" | xargs)
      install_extension "$extension"
    fi
  done < "$EXTENSIONS_FILE"
  
  # Report installation results
  echo "=== Extension Installation Report ==="
  echo "Installed extensions:"
  code-server --list-extensions
  echo "========================================="
  echo "Finished installing extensions"
else
  echo "No extensions list found"
fi
EOL
            
            # Create a script to apply environment variables
            echo "Creating environment setup script"
            cat > /workspaces/setup-env.sh << 'EOL'
#!/bin/bash
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
EOL
            chmod +x /workspaces/setup-env.sh
            
            # Create a script to run lifecycle commands
            echo "Creating lifecycle script"
            cat > /workspaces/run-lifecycle.sh << 'EOL'
#!/bin/bash
# Run post-create command if it exists
if [ -f "/workspaces/post-create-command.sh" ]; then
  echo "Running postCreateCommand..."
  /workspaces/post-create-command.sh
fi

# Run post-start command if it exists
if [ -f "/workspaces/post-start-command.sh" ]; then
  echo "Running postStartCommand..."
  /workspaces/post-start-command.sh
fi

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
EOL
            chmod +x /workspaces/run-lifecycle.sh
    """
    
    # Add the feature installation script directly
    feature_script = _create_feature_installation_script()
    init_script += """
            # Create features installation script
            echo "Creating features installation script"
            cat > /workspaces/install-features.sh << 'EOL'
"""
    init_script += feature_script
    init_script += """
EOL
            chmod +x /workspaces/install-features.sh
    """
    
    # Continue with the rest of the script
    init_script += """
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
    """
    
    # Add Dockerfile creation
    init_script += f"""
    # Create a wrapper Dockerfile that uses the user's image as a base
    cat > Dockerfile << 'EOF'
# This will be replaced with the tag for the user's custom image
FROM xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-user-{namespace_name}

# Install code-server
RUN curl -fsSL https://code-server.dev/install.sh | sh

# Expose default code-server port
EXPOSE 8443

# Set up entrypoint to run code-server
ENTRYPOINT ["/bin/sh", "-c", "if [ -f /workspaces/install-features.sh ]; then /workspaces/install-features.sh; fi && if [ -f /workspaces/setup-env.sh ]; then source /workspaces/setup-env.sh; fi && if [ -f /workspaces/install-extensions.sh ]; then /workspaces/install-extensions.sh; fi && if [ -f /workspaces/run-lifecycle.sh ]; then /workspaces/run-lifecycle.sh & fi && /usr/bin/code-server --bind-addr 0.0.0.0:8443 --auth password"]
CMD ["--user-data-dir", "/config/data", "--extensions-dir", "/config/extensions", "/workspaces"]
EOF
    
    # Create a flag file to indicate setup is done
    touch /workspaces/.code-server-initialized
    
    # Initialize workspace
    echo "Workspace initialization completed!"
    """
    
    init_config_map = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name="workspace-init",
            namespace=workspace_ids['namespace_name'],
            labels={"app": "workspace"}
        ),
        data={
            "init.sh": init_script
        }
    )
    core_v1.create_namespaced_config_map(workspace_ids['namespace_name'], init_config_map)
    logger.info(f"Created init script ConfigMap in namespace: {workspace_ids['namespace_name']}")

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

    """

    # Add repository clone commands
    for i, repo_url in enumerate(workspace_config['github_urls']):
        # Extract repo name from the URL
        repo_name_parts = repo_url.rstrip('/').split('/')
        folder_name = repo_name_parts[-1].replace('.git', '') if len(repo_name_parts) > 1 else f"repo-{i}"
            
        # Add clone command for this repository
        init_script += f"""
        # Clone repository {i+1}: {repo_url}
        if [ ! -d "/workspaces/{folder_name}" ]; then
            echo "Cloning {repo_url} into {folder_name}..."
            git clone {repo_url} {folder_name}
        fi
    """

    # Add custom image building section if required
    if workspace_config['use_custom_image_url']:
        init_script += _generate_custom_image_script(workspace_ids, workspace_config)

    # Add standard initialization code
    init_script += _generate_standard_init_code()
    
    return init_script


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


def _generate_standard_init_code():
    """Generate standard initialization code common to all workspaces"""
    return """

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


def _create_workspace_info_configmap(workspace_ids, workspace_config):
    """Create ConfigMap with workspace information"""
    workspace_info = _get_workspace_info(workspace_ids, workspace_config)
    
    info_config_map = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name="workspace-info",
            namespace=workspace_ids['namespace_name'],
            labels={"app": "workspace-info"}
        ),
        data={
            "info": json.dumps(workspace_info)
        }
    )
    core_v1.create_namespaced_config_map(workspace_ids['namespace_name'], info_config_map)
    logger.info(f"Created workspace info ConfigMap in namespace: {workspace_ids['namespace_name']}")


def _get_workspace_info(workspace_ids, workspace_config):
    """Create the workspace information dictionary"""
    workspace_info = {
        "id": workspace_ids['workspace_id'],
        "repositories": workspace_config['github_urls'],
        "primaryRepo": workspace_config['primary_repo_url'],
        "repoName": workspace_config['repo_name'],
        "subdomain": workspace_ids['subdomain'],
        "fqdn": workspace_ids['fqdn'],
        "url": f"https://{workspace_ids['fqdn']}",
        "password": workspace_ids['password'],
        "created": datetime.now().isoformat()
    }

    if workspace_config['use_custom_image_url']:
        workspace_info["imageUrl"] = workspace_config['custom_image_url']
        workspace_info["customImage"] = True
    else:
        workspace_info["image"] = workspace_config['custom_image']
        workspace_info["customImage"] = False
        workspace_info["useDevContainer"] = workspace_config['use_dev_container']
        
    return workspace_info


def _copy_port_detector_configmap(workspace_ids):
    """Copy port-detector ConfigMap from workspace-system to the new namespace"""
    try:
        # Get the ConfigMap from workspace-system
        port_detector_cm = core_v1.read_namespaced_config_map(
            name="port-detector", 
            namespace="workspace-system"
        )
        
        # Create a new ConfigMap in the workspace namespace
        new_cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name="port-detector",
                namespace=workspace_ids['namespace_name'],
                labels={"app": "workspace"}
            ),
            data=port_detector_cm.data  # Copy the data from the original ConfigMap
        )
        
        # Create the ConfigMap in the new namespace
        core_v1.create_namespaced_config_map(workspace_ids['namespace_name'], new_cm)
        logger.info(f"Copied port-detector ConfigMap to namespace: {workspace_ids['namespace_name']}")
        
    except Exception as e:
        logger.error(f"Error copying port-detector ConfigMap: {e}")
        # Continue anyway, as this is not critical


def _copy_wildcard_certificate(workspace_ids):
    """Copy wildcard certificate from workspace-system to the new namespace"""
    try:
        # Check if the wildcard certificate secret exists in workspace-system
        wildcard_cert = core_v1.read_namespaced_secret(
            name="workspace-domain-wildcard-tls", 
            namespace="workspace-system"
        )
        
        # Create a new secret in the workspace namespace with the same data
        wildcard_cert_data = wildcard_cert.data
        wildcard_cert_new = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name="workspace-domain-wildcard-tls",
                namespace=workspace_ids['namespace_name'],
                labels={"app": "workspace"}
            ),
            data=wildcard_cert_data,
            type=wildcard_cert.type
        )
        
        # Create the secret in the new namespace
        core_v1.create_namespaced_secret(workspace_ids['namespace_name'], wildcard_cert_new)
        logger.info(f"Copied wildcard certificate secret to namespace: {workspace_ids['namespace_name']}")
        
    except Exception as e:
        logger.error(f"Error copying wildcard certificate: {e}")
        # Continue anyway, but log it - this might cause SSL errors


def _create_deployment(workspace_ids, workspace_config):
    """Create deployment for the code-server"""
    # Create storage for local registry
    _create_pvc_for_registry(workspace_ids)

    # Define init containers
    init_containers = _create_init_containers(workspace_ids, workspace_config)

    # Define volumes
    volumes = _create_volumes(workspace_ids)

    # try:
    #     # Get the CA certificate from the workspace-system namespace
    #     registry_ca = client.CoreV1Api().read_namespaced_config_map(
    #         name="registry-ca",
    #         namespace="workspace-system"
    #     )
        
    #     # Create the same ConfigMap in the workspace namespace
    #     ca_cm = client.V1ConfigMap(
    #         metadata=client.V1ObjectMeta(
    #             name="registry-ca",
    #             namespace=workspace_ids['namespace_name']
    #         ),
    #         data=registry_ca.data
    #     )
        
    #     client.CoreV1Api().create_namespaced_config_map(
    #         namespace=workspace_ids['namespace_name'],
    #         body=ca_cm
    #     )
        
    #     logging.info(f"Copied registry CA to namespace {workspace_ids['namespace_name']}")
    # except Exception as e:
    #     logging.error(f"Error copying registry CA: {e}")

    
    # Create a Docker config JSON with registry authentication
    auth_config = {
        "auths": {
            "registry.workspace-system.svc.cluster.local:5000": {
                "auth": ""  # Empty auth for registry without username/password
            }
        }
    }

    # Convert to base64
    auth_json = json.dumps(auth_config).encode()
    auth_b64 = base64.b64encode(auth_json).decode()

    # Create the secret
    registry_secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="registry-credentials",
            namespace=workspace_ids['namespace_name']
        ),
        type="kubernetes.io/dockerconfigjson",
        data={
            ".dockerconfigjson": auth_b64
        }
    )

    # Create the secret in the namespace
    client.CoreV1Api().create_namespaced_secret(
        namespace=workspace_ids['namespace_name'],
        body=registry_secret
    )

    create_service_workspace_account(workspace_ids['namespace_name'])

    # Define containers
    code_server_container = _create_code_server_container(workspace_ids, workspace_config)
    port_detector_container = _create_port_detector_container()

    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name="code-server",
            namespace=workspace_ids['namespace_name'],
            labels={
                "app": "workspace",
                "allowed-registry-access": "true"
            }
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={"app": "code-server"}
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={
                        "app": "code-server",
                        "allowed-registry-access": "true"
                    },
                    annotations={
                        # Add this to allow insecure registry
                        "container.apparmor.security.beta.kubernetes.io/code-server": "unconfined"
                    }
                ),
                spec=client.V1PodSpec(
                    host_network=True,
                    service_account_name="workspace-controller",
                    init_containers=init_containers,
                    containers=[code_server_container, port_detector_container],
                    volumes=volumes,
                    image_pull_secrets=[
                        client.V1LocalObjectReference(name="registry-credentials")
                    ]
                )
            )
        )
    )

    apps_v1.create_namespaced_deployment(workspace_ids['namespace_name'], deployment)
    logger.info(f"Created deployment in namespace: {workspace_ids['namespace_name']}")


def _create_init_containers(workspace_ids, workspace_config):
    """Create the initialization containers for the deployment"""
    init_containers = [
        _create_workspace_init_container()
    ]

    # init_containers.append(
    #     client.V1Container(
    #         name="update-ca-certificates",
    #         image="ubuntu:20.04",
    #         command=["/bin/sh", "-c"],
    #         args=[
    #             "apt-get update && apt-get install -y ca-certificates && " +
    #             "cp /registry-ca/ca.crt /usr/local/share/ca-certificates/ && " +
    #             "update-ca-certificates && " +
    #             "echo 'CA certificates updated'"
    #         ],
    #         volume_mounts=[
    #             client.V1VolumeMount(
    #                 name="registry-ca",
    #                 mount_path="/registry-ca"
    #             )
    #         ]
    #     )
    # )
    
    # Add code-server setup container when using dev container mode
    # if workspace_config['use_dev_container']:
    #     init_containers.append(_create_codeserver_setup_container())
    
    # init_containers.append(_create_custom_image_build_container(workspace_ids))

    # Add build container for custom image if URL is provided
    # if workspace_config['use_custom_image_url']:
    #     init_containers.append(_create_custom_image_build_container(workspace_ids))
    
    init_containers.append(_create_base_image_kaniko_container(workspace_ids)),
    init_containers.append(_create_wrapper_kaniko_container(workspace_ids))

    return init_containers


def _create_workspace_init_container():
    """Create the main workspace initialization container"""
    return client.V1Container(
        name="init-workspace",
        image="alpine/git",
        command=["/bin/sh", "/scripts/init.sh"],
        security_context=client.V1SecurityContext(
            capabilities=client.V1Capabilities(
                add=["CHOWN", "FOWNER", "FSETID", "DAC_OVERRIDE"]
            )
        ),
        volume_mounts=[
            client.V1VolumeMount(
                name="workspace-data",
                mount_path="/config",  # LinuxServer.io's main config directory
                sub_path="config"
            ),
            client.V1VolumeMount(
                name="workspace-data",
                mount_path="/workspaces",
                sub_path="workspaces"
            ),
            client.V1VolumeMount(
                name="init-script",  # Add this new volume mount
                mount_path="/scripts"  # Match the command's expected path
            ),
            client.V1VolumeMount(
                name="docker-sock",
                mount_path="/var/run/docker.sock"
            )
        ]
    )

def _create_pvc_for_registry(workspace_ids):
    """Create PVC for local registry storage"""
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name="registry-storage",
            namespace=workspace_ids['namespace_name']
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1ResourceRequirements(
                requests={"storage": "5Gi"}
            ),
            storage_class_name="efs-sc"
        )
    )
    core_v1.create_namespaced_persistent_volume_claim(workspace_ids['namespace_name'], pvc)
    logger.info(f"Created registry storage PVC in namespace: {workspace_ids['namespace_name']}")


def _create_base_image_kaniko_container(workspace_ids):
    """Create container for building user's base Docker image using Kaniko"""

    return client.V1Container(
        name="build-base-image",
        image="gcr.io/kaniko-project/executor:latest",
        args=[
            "--dockerfile=/workspace/Dockerfile",
            "--context=/workspace",
            f"--destination=xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-user-{workspace_ids['namespace_name']}",
            "--insecure",
            "--skip-tls-verify"
        ],
        volume_mounts=[
            client.V1VolumeMount(
                name="workspace-data",
                mount_path="/workspace",
                sub_path="workspaces/.user-dockerfile"  # Path to the user's Dockerfile
            )
        ]
    )


def _create_wrapper_kaniko_container(workspace_ids):
    """Create container for building code-server wrapper image using Kaniko"""
    nodes = client.CoreV1Api().list_node()
    node_ip = nodes.items[0].status.addresses[0].address

    return client.V1Container(
        name="build-wrapper-image",
        image="gcr.io/kaniko-project/executor:latest",
        args=[
            "--dockerfile=/workspace/Dockerfile",
            "--context=/workspace",
            f"--destination=xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-wrapper-{workspace_ids['namespace_name']}",
            "--insecure",
            "--skip-tls-verify"
        ],
        volume_mounts=[
            client.V1VolumeMount(
                name="workspace-data",
                mount_path="/workspace",
                sub_path="workspaces/.code-server-wrapper"
            )
        ]
    )

def _create_volumes(workspace_ids):
    """Create the volume definitions for the deployment"""
    return [
        client.V1Volume(
            name="workspace-data",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name="workspace-data"
            )
        ),
        client.V1Volume(
            name="registry-storage",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name="registry-storage"
            )
        ),
        client.V1Volume(
            name="init-script",
            config_map=client.V1ConfigMapVolumeSource(
                name="workspace-init",
                default_mode=0o755
            )
        ),
        # Add volume for code-server in dev container mode
        client.V1Volume(
            name="code-server-data",
            empty_dir=client.V1EmptyDirVolumeSource()
        ),
        # Docker volumes
        client.V1Volume(
            name="docker-lib",
            empty_dir=client.V1EmptyDirVolumeSource()
        ),
        client.V1Volume(
            name="docker-sock",
            empty_dir=client.V1EmptyDirVolumeSource()
        ),
        # Port detector script
        client.V1Volume(
            name="port-detector-script",
            config_map=client.V1ConfigMapVolumeSource(
                name="port-detector",
                default_mode=0o755
            )
        # ),
        # client.V1Volume(
        #     name="registry-ca",
        #     config_map=client.V1ConfigMapVolumeSource(
        #         name="registry-ca"
        #     )
        )
    ]

def create_service_workspace_account(workspace_namespace):
    service_account = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name="workspace-controller",
            namespace=workspace_namespace,
            annotations={
                "eks.amazonaws.com/role-arn": "arn:aws:iam::xxxyyyzzz:role/workspace-controller-role"
            }
        )
    )
    
    try:
        api_instance = client.CoreV1Api()
        api_instance.create_namespaced_service_account(
            namespace=workspace_namespace,
            body=service_account
        )
        print(f"Created service account in namespace {workspace_namespace}")
    except Exception as e:
        print(f"Error creating service account: {e}")

def _create_code_server_container(workspace_ids, workspace_config):
    """Create the main code-server container"""
    # Set the container image based on configuration
    # container_image = workspace_config['custom_image']
    image_pull_policy = "Always"

    return client.V1Container(
        name="code-server",
        image=f"xxxyyyzzz.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-wrapper-{workspace_ids['namespace_name']}",
        image_pull_policy=image_pull_policy,
        args=[
            "--user-data-dir", "/config/data",
            "--extensions-dir", "/config/extensions",
            "/workspaces"
        ],
        ports=[
            client.V1ContainerPort(container_port=8443)
        ],
        env=[
            # LinuxServer.io specific environment variables
            client.V1EnvVar(name="PUID", value="1000"),  # User ID
            client.V1EnvVar(name="PGID", value="1000"),  # Group ID
            client.V1EnvVar(name="TZ", value="UTC"),  # Timezone
            client.V1EnvVar(name="DEFAULT_WORKSPACE", value="/workspaces"),  
            client.V1EnvVar(name="VSCODE_PROXY_URI", value=f"https://{workspace_ids['subdomain']}-{{{{port}}}}.{WORKSPACE_DOMAIN}/"),
            # Authentication and core settings
            client.V1EnvVar(
                name="PASSWORD",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name="workspace-secret",
                        key="password"
                    )
                )
            ),
            # Docker support
            client.V1EnvVar(name="DOCKER_HOST", value="unix:///var/run/docker.sock"),
            # Add this for dev container mode
            client.V1EnvVar(name="CODE_SERVER_PATH", value="/opt/code-server/bin/code-server" if workspace_config['use_dev_container'] else ""),
        ],
        volume_mounts=_create_code_server_volume_mounts(workspace_config),
        lifecycle=client.V1Lifecycle(
            post_start=client.V1LifecycleHandler(
                _exec=client.V1ExecAction(
                    command=_create_post_start_command()
                )
            )
        ),
        security_context=client.V1SecurityContext(
            run_as_user=0 if workspace_config['use_dev_container'] else None,
            privileged=True,  # Ensure full access to Docker
            capabilities=client.V1Capabilities(
                add=["SYS_ADMIN", "NET_ADMIN"]
            )
        ),
        resources=client.V1ResourceRequirements(
            requests={
                "cpu": "2",
                "memory": "4Gi"
            },
            limits={
                "cpu": "3",
                "memory": "6Gi"
            }
        )
    )

def _create_code_server_volume_mounts(workspace_config):
    """Create volume mounts for the code-server container"""
    volume_mounts = [
        client.V1VolumeMount(
            name="workspace-data",
            mount_path="/config",  # LinuxServer.io uses /config for persistent data
            sub_path="config"
        ),
        client.V1VolumeMount(
            name="workspace-data",
            mount_path="/workspaces",
            sub_path="workspaces"
        ),
        # Docker daemon storage and socket
        client.V1VolumeMount(
            name="docker-lib",
            mount_path="/var/lib/docker"
        ),
        client.V1VolumeMount(
            name="docker-sock",
            mount_path="/var/run"
        # ),
        # client.V1VolumeMount(
        #     name="registry-ca",
        #     mount_path="/usr/local/share/ca-certificates/registry-ca.crt",
        #     sub_path="ca.crt"
        )
    ]
    
    # Add the code-server volume mount when in dev container mode
    if workspace_config['use_dev_container']:
        volume_mounts.append(
            client.V1VolumeMount(
                name="code-server-data",
                mount_path="/opt/code-server"
            )
        )
    
    return volume_mounts

@app.route('/api/workspaces/<workspace_id>', methods=['GET'])
def get_workspace(workspace_id):
    """Get details for a specific workspace"""
    try:
        # Find the namespace for this workspace
        namespaces = core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
        
        if not namespaces.items:
            return jsonify({"error": "Workspace not found"}), 404
            
        namespace_name = namespaces.items[0].metadata.name
        
        # Get workspace info from config map
        config_maps = core_v1.list_namespaced_config_map(namespace_name, label_selector="app=workspace-info")
        if not config_maps.items:
            return jsonify({"error": "Workspace info not found"}), 404
            
        workspace_info = json.loads(config_maps.items[0].data.get("info", "{}"))
        
        # Don't expose password unless explicitly requested
        if "password" in workspace_info and request.args.get("includePassword") != "true":
            workspace_info["password"] = "********"
        
        # Get pods to determine state
        pods = core_v1.list_namespaced_pod(namespace_name, label_selector="app=code-server")
        if pods.items:
            if pods.items[0].status.phase == "Running":
                workspace_info["state"] = "running"
            else:
                workspace_info["state"] = pods.items[0].status.phase.lower()
        else:
            workspace_info["state"] = "unknown"
        
        return jsonify(workspace_info)
    except Exception as e:
        logger.error(f"Error getting workspace: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/workspaces/<workspace_id>/delete', methods=['DELETE'])
def delete_workspace(workspace_id):
    """Delete a workspace"""
    try:
        # Find the namespace for this workspace
        namespaces = core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
        
        if not namespaces.items:
            return jsonify({"error": "Workspace not found"}), 404
            
        namespace_name = namespaces.items[0].metadata.name
        
        # Delete the namespace (this will delete all resources in it)
        core_v1.delete_namespace(namespace_name)
        
        return jsonify({
            "success": True,
            "message": f"Workspace {workspace_id} deleted"
        })
    except Exception as e:
        logger.error(f"Error deleting workspace: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/workspaces/<workspace_id>/stop', methods=['POST'])
def stop_workspace(workspace_id):
    """Stop a workspace by scaling it to 0 replicas"""
    try:
        # Find the namespace for this workspace
        namespaces = core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
        
        if not namespaces.items:
            return jsonify({"error": "Workspace not found"}), 404
            
        namespace_name = namespaces.items[0].metadata.name
        
        # Scale the deployment to 0
        apps_v1.patch_namespaced_deployment_scale(
            name="code-server",
            namespace=namespace_name,
            body={"spec": {"replicas": 0}}
        )
        
        return jsonify({
            "success": True,
            "message": f"Workspace {workspace_id} stopped"
        })
    except Exception as e:
        logger.error(f"Error stopping workspace: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/workspaces/<workspace_id>/start', methods=['POST'])
def start_workspace(workspace_id):
    """Start a workspace by scaling it to 1 replica"""
    try:
        # Find the namespace for this workspace
        namespaces = core_v1.list_namespace(label_selector=f"workspaceId={workspace_id}")
        
        if not namespaces.items:
            return jsonify({"error": "Workspace not found"}), 404
            
        namespace_name = namespaces.items[0].metadata.name
        
        # Scale the deployment to 1
        apps_v1.patch_namespaced_deployment_scale(
            name="code-server",
            namespace=namespace_name,
            body={"spec": {"replicas": 1}}
        )
        
        return jsonify({
            "success": True,
            "message": f"Workspace {workspace_id} started"
        })
    except Exception as e:
        logger.error(f"Error starting workspace: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000)