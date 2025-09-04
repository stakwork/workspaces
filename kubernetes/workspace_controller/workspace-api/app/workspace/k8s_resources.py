import json
import base64
import time
import logging
from kubernetes import client
from app.config import app_config
from app.utils.scripts import (
    create_post_start_command, 
    generate_comprehensive_init_script,
    generate_helper_scripts,
    get_warmer_javascript
)

logger = logging.getLogger(__name__)


def create_namespace(workspace_ids):
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
    app_config.core_v1.create_namespace(namespace)
    logger.info(f"Created namespace: {workspace_ids['namespace_name']}")


# def create_persistent_volume_claim(workspace_ids):
#     """Create PVC for workspace data"""
#     pvc = client.V1PersistentVolumeClaim(
#         metadata=client.V1ObjectMeta(
#             name="workspace-data",
#             namespace=workspace_ids['namespace_name'],
#             labels={"app": "workspace"}
#         ),
#         spec=client.V1PersistentVolumeClaimSpec(
#             access_modes=["ReadWriteMany"],
#             resources=client.V1ResourceRequirements(
#                 requests={"storage": "10Gi"}
#             ),
#             storage_class_name="efs-sc"
#         )
#     )
#     app_config.core_v1.create_namespaced_persistent_volume_claim(workspace_ids['namespace_name'], pvc)
#     logger.info(f"Created PVC in namespace: {workspace_ids['namespace_name']}")


def create_workspace_secret(workspace_ids, workspace_config):
    """Create secret for workspace credentials and optional GitHub token"""
    string_data = {
        "password": workspace_ids['password']
    }

    github_token = workspace_config.get('github_token')
    github_username = workspace_config.get('github_username')
    env_vars = workspace_config.get('env_vars')

    if github_token:
        string_data["github_token"] = github_token
        string_data["github_username"] = github_username

    if env_vars:
        for env_var in env_vars:
            env_name = env_var.get('name', '').strip()
            env_value = env_var.get('value', '').strip()
            
            if env_name and env_value:
                # Prefix env vars to avoid conflicts with system secrets
                string_data[f"env_{env_name}"] = env_value

    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="workspace-secret",
            namespace=workspace_ids['namespace_name'],
            labels={"app": "workspace"}
        ),
        string_data=string_data
    )
    app_config.core_v1.create_namespaced_secret(workspace_ids['namespace_name'], secret)
    logger.info(f"Created secret in namespace: {workspace_ids['namespace_name']}")


def create_init_script_configmap(workspace_ids, workspace_config):
    """Create ConfigMap with initialization scripts"""
    # Generate the comprehensive init script
    init_script = generate_comprehensive_init_script(
        workspace_ids, 
        workspace_config, 
        app_config.AWS_ACCOUNT_ID
    )
    
    # Generate helper scripts
    helper_scripts = generate_helper_scripts()
    
    # Add helper scripts to the init script
    init_script += f"""
# Create docker-compose startup script
echo "Creating docker-compose startup script"
cat > /workspaces/.pod-config/start-docker-compose.sh << 'EOL'
{helper_scripts['docker_compose_script']}
EOL
chmod +x /workspaces/.pod-config/start-docker-compose.sh

# Create extension installation script
echo "Creating extension installation script"
cat > /workspaces/.pod-config/install-extensions.sh << 'EOL'
{helper_scripts['extension_install_script']}
EOL
chmod +x /workspaces/.pod-config/install-extensions.sh

# Create environment setup script
echo "Creating environment setup script"
cat > /workspaces/.pod-config/setup-env.sh << 'EOL'
{helper_scripts['env_setup_script']}
EOL
chmod +x /workspaces/.pod-config/setup-env.sh

# Create lifecycle script
echo "Creating lifecycle script"
cat > /workspaces/.pod-config/run-lifecycle.sh << 'EOL'
{helper_scripts['lifecycle_script']}
EOL
chmod +x /workspaces/.pod-config/run-lifecycle.sh
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
    app_config.core_v1.create_namespaced_config_map(workspace_ids['namespace_name'], init_config_map)
    logger.info(f"Created init script ConfigMap in namespace: {workspace_ids['namespace_name']}")


def create_workspace_info_configmap(workspace_ids, workspace_config):
    """Create ConfigMap with workspace information"""
    from datetime import datetime
    
    workspace_info = {
        "id": workspace_ids['workspace_id'],
        "repositories": workspace_config['github_urls'],
        "branches": workspace_config['github_branches'],
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
    app_config.core_v1.create_namespaced_config_map(workspace_ids['namespace_name'], info_config_map)
    logger.info(f"Created workspace info ConfigMap in namespace: {workspace_ids['namespace_name']}")


def copy_port_detector_configmap(workspace_ids):
    """Copy port-detector ConfigMap from workspace-system to the new namespace"""
    try:
        # Get the ConfigMap from workspace-system
        port_detector_cm = app_config.core_v1.read_namespaced_config_map(
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
        app_config.core_v1.create_namespaced_config_map(workspace_ids['namespace_name'], new_cm)
        logger.info(f"Copied port-detector ConfigMap to namespace: {workspace_ids['namespace_name']}")
        
    except Exception as e:
        logger.error(f"Error copying port-detector ConfigMap: {e}")
        # Continue anyway, as this is not critical


def copy_wildcard_certificate(workspace_ids):
    """Copy wildcard certificate from workspace-system to the new namespace"""
    try:
        # Check if the wildcard certificate secret exists in workspace-system
        wildcard_cert = app_config.core_v1.read_namespaced_secret(
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
        app_config.core_v1.create_namespaced_secret(workspace_ids['namespace_name'], wildcard_cert_new)
        logger.info(f"Copied wildcard certificate secret to namespace: {workspace_ids['namespace_name']}")
        
    except Exception as e:
        logger.error(f"Error copying wildcard certificate: {e}")
        # Continue anyway, but log it - this might cause SSL errors


def copy_dockerhub_secret(workspace_ids):
    """Copy dockerhub-secret from workspace-system to the new namespace"""
    try:
        # Get the secret from workspace-system
        dockerhub_secret = app_config.core_v1.read_namespaced_secret(
            name="dockerhub-secret", 
            namespace="workspace-system"
        )
        
        # Create a new secret in the workspace namespace
        new_secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name="dockerhub-secret",
                namespace=workspace_ids['namespace_name'],
                labels={"app": "workspace"}
            ),
            data=dockerhub_secret.data,
            type=dockerhub_secret.type
        )
        
        # Create the secret in the new namespace
        app_config.core_v1.create_namespaced_secret(workspace_ids['namespace_name'], new_secret)
        logger.info(f"Copied dockerhub-secret to namespace: {workspace_ids['namespace_name']}")
        
    except Exception as e:
        logger.error(f"Error copying dockerhub-secret: {e}")


# def create_pvc_for_registry(workspace_ids):
#     """Create PVC for local registry storage"""
#     pvc = client.V1PersistentVolumeClaim(
#         metadata=client.V1ObjectMeta(
#             name="registry-storage",
#             namespace=workspace_ids['namespace_name']
#         ),
#         spec=client.V1PersistentVolumeClaimSpec(
#             access_modes=["ReadWriteOnce"],
#             resources=client.V1ResourceRequirements(
#                 requests={"storage": "5Gi"}
#             ),
#             storage_class_name="efs-sc"
#         )
#     )
#     app_config.core_v1.create_namespaced_persistent_volume_claim(workspace_ids['namespace_name'], pvc)
#     logger.info(f"Created registry storage PVC in namespace: {workspace_ids['namespace_name']}")


def create_service_account(workspace_namespace):
    """Create service account for the workspace"""
    service_account = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name="workspace-controller",
            namespace=workspace_namespace,
            annotations={
                "eks.amazonaws.com/role-arn": f"arn:aws:iam::{app_config.AWS_ACCOUNT_ID}:role/workspace-controller-role"
            }
        )
    )
    
    try:
        app_config.core_v1.create_namespaced_service_account(
            namespace=workspace_namespace,
            body=service_account
        )
        logger.info(f"Created service account in namespace {workspace_namespace}")
    except Exception as e:
        logger.error(f"Error creating service account: {e}")


def create_registry_secret(workspace_ids):
    """Create registry authentication secret"""
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
    app_config.core_v1.create_namespaced_secret(
        namespace=workspace_ids['namespace_name'],
        body=registry_secret
    )
    logger.info(f"Created registry secret in namespace: {workspace_ids['namespace_name']}")


def create_deployment(workspace_ids, workspace_config):
    """Create deployment for the code-server"""
    # Create storage for local registry
    # create_pvc_for_registry(workspace_ids)  # Using EmptyDir instead

    # Define init containers
    init_containers = _create_init_containers(workspace_ids, workspace_config)

    # Define volumes
    volumes = _create_volumes(workspace_ids)

    # Create service account and registry secret
    create_service_account(workspace_ids['namespace_name'])
    create_registry_secret(workspace_ids)

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
                        "container.apparmor.security.beta.kubernetes.io/code-server": "unconfined",
                        "deployment.kubernetes.io/revision": str(int(time.time())),
                        "kubectl.kubernetes.io/restartedAt": str(int(time.time()))
                    }
                ),
                spec=client.V1PodSpec(
                    # host_network=True,
                    service_account_name="workspace-controller",
                    init_containers=init_containers,
                    containers=[code_server_container, port_detector_container],
                    volumes=volumes,
                    image_pull_secrets=[
                        client.V1LocalObjectReference(name="registry-credentials"),
                        client.V1LocalObjectReference(name="dockerhub-secret")
                    ]
                )
            )
        )
    )

    app_config.apps_v1.create_namespaced_deployment(workspace_ids['namespace_name'], deployment)
    logger.info(f"Created deployment in namespace: {workspace_ids['namespace_name']}")


def _create_init_containers(workspace_ids, workspace_config):
    """Create the initialization containers for the deployment"""
    init_containers = [
        _create_workspace_init_container(workspace_config),
        _create_base_image_kaniko_container(workspace_ids),
        _create_wrapper_kaniko_container(workspace_ids)
    ]
    return init_containers


def _create_workspace_init_container(workspace_config):
    """Create the main workspace initialization container"""
    base_env_vars = [
        # Add GITHUB_TOKEN env var if present in secret
        client.V1EnvVar(
            name="GITHUB_TOKEN",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name="workspace-secret",
                    key="github_token",
                    optional=True
                )
            )
        ),
        client.V1EnvVar(
            name="GITHUB_USERNAME",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name="workspace-secret",
                    key="github_username",
                    optional=True
                )
            )
        )
    ]
    
    env_vars = workspace_config.get('env_vars', [])
    
    # Add custom environment variables
    if env_vars:
        for env_var in env_vars:
            env_name = env_var.get('name', '').strip()
            if env_name:
                base_env_vars.append(
                    client.V1EnvVar(
                        name=env_name,
                        value_from=client.V1EnvVarSource(
                            secret_key_ref=client.V1SecretKeySelector(
                                name="workspace-secret",
                                key=f"env_{env_name}",
                                optional=True
                            )
                        )
                    )
                )
    
    return client.V1Container(
        name="init-workspace",
        image="buildpack-deps:22.04-scm",
        command=["/bin/bash", "/scripts/init.sh"],
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
        ],
        env=base_env_vars
    )


def _create_base_image_kaniko_container(workspace_ids):
    """Create container for building user's base Docker image using Kaniko"""
    return client.V1Container(
        name="build-base-image",
        image="gcr.io/kaniko-project/executor:latest",
        args=[
            "--dockerfile=/workspace/Dockerfile",
            "--context=/workspace",
            f"--destination={app_config.AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-user-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}",
            "--insecure",
            "--skip-tls-verify",
            "--verbosity=debug",
            "--push-retry=3"
        ],
        env=[
            client.V1EnvVar(name="DOCKER_CONFIG", value="/kaniko/.docker/"),
            client.V1EnvVar(name="HTTP_TIMEOUT", value="600s"),  # Increase timeout
            client.V1EnvVar(name="HTTPS_TIMEOUT", value="600s")
        ],
        volume_mounts=[
            client.V1VolumeMount(
                name="workspace-data",
                mount_path="/workspace",
                sub_path="workspaces/.pod-config/.user-dockerfile"  # Path to the user's Dockerfile
            )
        ]
    )


def _create_wrapper_kaniko_container(workspace_ids):
    """Create container for building code-server wrapper image using Kaniko"""
    return client.V1Container(
        name="build-wrapper-image",
        image="gcr.io/kaniko-project/executor:latest",
        args=[
            "--dockerfile=/workspace/Dockerfile",
            "--context=/workspace",
            f"--destination={app_config.AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-wrapper-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}",
            "--insecure",
            "--skip-tls-verify",
            "--verbosity=debug",
            "--push-retry=3"
        ],
        env=[
            client.V1EnvVar(name="DOCKER_CONFIG", value="/kaniko/.docker/"),
            client.V1EnvVar(name="HTTP_TIMEOUT", value="600s"),  # Increase timeout
            client.V1EnvVar(name="HTTPS_TIMEOUT", value="600s")
        ],
        volume_mounts=[
            client.V1VolumeMount(
                name="workspace-data",
                mount_path="/workspace",
                sub_path="workspaces/.pod-config/.code-server-wrapper"
            )
        ]
    )


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


def _create_code_server_container(workspace_ids, workspace_config):
    """Create the main code-server container"""
    image_pull_policy = "Always"

    # Base environment variables
    base_env_vars = [
        # LinuxServer.io specific environment variables
        client.V1EnvVar(name="PUID", value="1000"),  # User ID
        client.V1EnvVar(name="PGID", value="1000"),  # Group ID
        client.V1EnvVar(name="TZ", value="UTC"),  # Timezone
        client.V1EnvVar(name="DEFAULT_WORKSPACE", value="/workspaces"),  
        client.V1EnvVar(name="VSCODE_EXTENSIONS", value="/config/extensions"),
        client.V1EnvVar(name="CODE_SERVER_EXTENSIONS_DIR", value="/config/extensions"),
        client.V1EnvVar(name="VSCODE_USER_DATA_DIR", value="/config/data"),
        client.V1EnvVar(name="CS_DISABLE_GETTING_STARTED_OVERRIDE", value="true"),
        client.V1EnvVar(name="VSCODE_DISABLE_TELEMETRY", value="true"),
        client.V1EnvVar(name="DISABLE_TELEMETRY", value="true"),
        client.V1EnvVar(name="VSCODE_PROXY_URI", value=f"https://{workspace_ids['subdomain']}-{{{{port}}}}.{app_config.WORKSPACE_DOMAIN}/"),
        client.V1EnvVar(name="POD_URL", value=f"https://{workspace_ids['subdomain']}.{app_config.WORKSPACE_DOMAIN}/"),
        client.V1EnvVar(
            name="CODE_SERVER_PASSWORD", 
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name="workspace-secret",
                    key="password"
                )
            )
        ),
        client.V1EnvVar(
            name="GITHUB_TOKEN",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name="workspace-secret",
                    key="github_token",
                    optional=True
                )
            )
        ),
        client.V1EnvVar(
            name="GITHUB_USERNAME",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name="workspace-secret",
                    key="github_username",
                    optional=True
                )
            )
        ),
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
    ]

    env_vars = workspace_config.get('env_vars', [])
    # Add custom environment variables from pool configuration
    if env_vars:
        for env_var in env_vars:
            env_name = env_var.get('name', '').strip()
            if env_name:
                # Add environment variable that references the secret
                base_env_vars.append(
                    client.V1EnvVar(
                        name=env_name,
                        value_from=client.V1EnvVarSource(
                            secret_key_ref=client.V1SecretKeySelector(
                                name="workspace-secret",
                                key=f"env_{env_name}",
                                optional=True  # Make it optional in case the env var is not set
                            )
                        )
                    )
                )

    return client.V1Container(
        name="code-server",
        image=f"{app_config.AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-wrapper-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}",
        image_pull_policy=image_pull_policy,
        env=base_env_vars,  # Use the combined environment variables
        volume_mounts=_create_code_server_volume_mounts(workspace_config),
        lifecycle=client.V1Lifecycle(
            post_start=client.V1LifecycleHandler(
                _exec=client.V1ExecAction(
                    command=create_post_start_command()
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
                "cpu": workspace_config.get('cpu', '2'),
                "memory": workspace_config.get('memory', '8Gi')
            },
            limits={
                "cpu": workspace_config.get('cpu', '2'),
                "memory": workspace_config.get('memory', '8Gi')
            }
        )
    )

def create_smart_warmer_job(main_pod_name, workspace_ids):
    url = f"https://{workspace_ids['fqdn']}"  # Get the FQDN from workspace_ids
    namespace = workspace_ids['namespace_name']
    
    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=f"code-server-warmer-{main_pod_name}",
            namespace=namespace
        ),
        spec=client.V1JobSpec(
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="code-server-warmer",
                            image="docker.io/library/node:18-alpine",
                            command=["/bin/sh", "-c"],
                            args=[f"""
                                echo "⏳ Waiting for code-server to be ready..."
                                
                                # Install curl and debugging tools
                                apk add --no-cache curl ca-certificates
                                
                                # Wait for code-server to be responsive via HTTPS
                                READY=false
                                ATTEMPTS=0
                                MAX_ATTEMPTS=120  # 10 minutes max wait
                                
                                while [ "$READY" = false ] && [ $ATTEMPTS -lt $MAX_ATTEMPTS ]; do
                                    ATTEMPTS=$((ATTEMPTS + 1))
                                    echo "🔍 Checking code-server readiness via HTTPS... attempt $ATTEMPTS/$MAX_ATTEMPTS"
                                    
                                    # Try to connect to code-server via the actual URL
                                    HTTP_CODE=$(curl -k -s -w "%{{http_code}}" -o /dev/null "{url}/" 2>/dev/null || echo "000")
                                    echo "📊 HTTPS response code: $HTTP_CODE"
                                    
                                    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ] || [ "$HTTP_CODE" = "401" ]; then
                                        echo "✅ Code-server is responding via HTTPS!"
                                        READY=true
                                    else
                                        echo "⏳ Code-server not ready via HTTPS (HTTP: $HTTP_CODE), waiting 5 seconds..."
                                        sleep 5
                                    fi
                                done
                                
                                if [ "$READY" = false ]; then
                                    echo "❌ Code-server failed to become ready within timeout"
                                    echo "🔍 Final debugging info:"
                                    curl -k -v "{url}/" || true
                                    exit 1
                                fi
                                
                                echo "🎯 Code-server is ready via HTTPS, starting warmer in 10 seconds..."
                                sleep 10
                                
                                # Now install dependencies and run warmer
                                apk add --no-cache chromium nss freetype ca-certificates
                                
                                mkdir -p /app && cd /app
                                
cat > package.json << 'PACKAGE_EOF'
{{
  "name": "lightweight-browser-warmer",
  "version": "1.0.0",
  "dependencies": {{
    "playwright-core": "^1.40.0"
  }}
}}
PACKAGE_EOF

                                npm install
                                
cat > browser-warmer.js << 'WARMER_EOF'
{get_warmer_javascript(url)}
WARMER_EOF

                                echo "🚀 Starting code-server warmer..."
                                node browser-warmer.js
                            """],
                            env=[
                                client.V1EnvVar(
                                    name="CODE_SERVER_PASSWORD",
                                    value_from=client.V1EnvVarSource(
                                        secret_key_ref=client.V1SecretKeySelector(
                                            name="workspace-secret",
                                            key="password"
                                        )
                                    )
                                ),
                                client.V1EnvVar(name="CODE_SERVER_URL", value=url),
                                client.V1EnvVar(
                                    name="DOCKER_USERNAME",
                                    value_from=client.V1EnvVarSource(
                                        secret_key_ref=client.V1SecretKeySelector(
                                            name="dockerhub-secret",
                                            key="username",
                                            optional=True
                                        )
                                    )
                                ),
                                client.V1EnvVar(
                                    name="DOCKER_PASSWORD",
                                    value_from=client.V1EnvVarSource(
                                        secret_key_ref=client.V1SecretKeySelector(
                                            name="dockerhub-secret",
                                            key="password",
                                            optional=True
                                        )
                                    )
                                )
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": "50m", "memory": "150Mi"},
                                limits={"cpu": "200m", "memory": "400Mi"}
                            )
                        )
                    ],
                    restart_policy="Never",
                    image_pull_secrets=[
                        client.V1LocalObjectReference(name="dockerhub-secret")
                    ]
                )
            ),
            backoff_limit=2,
            active_deadline_seconds=1200
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


def _create_volumes(workspace_ids):
    """Create the volume definitions for the deployment"""
    return [
        client.V1Volume(
            name="workspace-data",
            empty_dir=client.V1EmptyDirVolumeSource()
        ),
        client.V1Volume(
            name="registry-storage",
            empty_dir=client.V1EmptyDirVolumeSource()
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
        )
    ]


def create_service(workspace_ids):
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
                    port=8444,
                    target_port=8444
                )
            ]
        )
    )
    app_config.core_v1.create_namespaced_service(workspace_ids['namespace_name'], service)
    logger.info(f"Created service in namespace: {workspace_ids['namespace_name']}")

def create_warmer_job(workspace_ids):
    """Create the warmer job that runs after the main deployment is ready"""
    warmer_job = create_smart_warmer_job(
        main_pod_name="code-server",
        workspace_ids=workspace_ids
    )
    
    app_config.batch_v1.create_namespaced_job(workspace_ids['namespace_name'], warmer_job)
    logger.info(f"Created warmer job in namespace: {workspace_ids['namespace_name']}")

def create_ingress(workspace_ids):
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
                                            number=8444
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
    app_config.networking_v1.create_namespaced_ingress(workspace_ids['namespace_name'], ingress)
    logger.info(f"Created ingress in namespace: {workspace_ids['namespace_name']}")