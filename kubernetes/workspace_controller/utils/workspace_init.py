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
        
        # Create logging directory with timestamps
        "mkdir -p /workspaces/logs",
        "LOGFILE=/workspaces/logs/init-$(date +%Y%m%d-%H%M%S).log",
        "exec 1> >(tee $LOGFILE)",
        "exec 2>&1",
        
        # Initialize status with more detail
        'echo "Initializing" > /workspaces/.init-status',
        "echo '[$(date)] Starting workspace initialization...'",
        
        # Save workspace IDs and config
        f"echo '{json.dumps(workspace_ids)}' > /workspaces/.workspace-ids",
        f"echo '{json.dumps(workspace_config)}' > /workspaces/.workspace-config",
        
        # Create working directory with proper permissions
        "mkdir -p /workspaces/workspace",
        "cd /workspaces/workspace",
        "chmod 755 /workspaces/workspace",

        # Install git-lfs if needed
        "if ! command -v git-lfs &> /dev/null; then",
        "    echo 'Installing git-lfs...'",
        "    apt-get update && apt-get install -y git-lfs",
        "    git lfs install",
        "fi",

        # Setup git config if PAT provided
        "if [ ! -z \"${GITHUB_TOKEN}\" ]; then",
        "    echo 'Configuring git credentials...'",
        "    git config --global credential.helper 'store --file=/workspaces/.git-credentials'",
        f"    echo 'https://${{GITHUB_TOKEN}}@github.com' > /workspaces/.git-credentials",
        "    chmod 600 /workspaces/.git-credentials",
        "    # Configure git to handle large files",
        "    git config --global http.postBuffer 524288000",
        "fi",
        
        # Enhanced clone function with better error handling and git-lfs support
        "function clone_repo() {",
        "    local url=$1",
        "    local branch=$2",
        "    local retries=3",
        "    local repo_name=$(basename $url .git)",
        "    local clone_opts=\"--recurse-submodules\"",
        "    ",
        "    echo \"[$(date)] Cloning repository $repo_name from $url${branch:+ branch $branch}...\"",
        "    ",
        "    # Check if repo already exists and is valid",
        "    if [ -d \"$repo_name/.git\" ]; then",
        "        echo \"Repository exists, checking status...\"",
        "        cd \"$repo_name\"",
        "        if git status &>/dev/null; then",
        "            echo \"Repository is valid, updating...\"",
        "            git fetch origin",
        "            if [ ! -z \"$branch\" ]; then",
        "                git reset --hard \"origin/$branch\"",
        "            else",
        "                git reset --hard origin/HEAD",
        "            fi",
        "            git submodule update --init --recursive",
        "            cd ..",
        "            return 0",
        "        fi",
        "        cd ..",
        "        echo \"Repository is invalid, removing...\"",
        "        rm -rf \"$repo_name\"",
        "    fi",
        "    ",
        "    for i in $(seq 1 $retries); do",
        "        echo \"Clone attempt $i of $retries...\"",
        "        if [ ! -z \"$branch\" ]; then",
        "            clone_opts=\"$clone_opts -b $branch\"",
        "        fi",
        "        if git clone $clone_opts \"$url\" 2>&1; then",
        "            cd \"$repo_name\"",
        "            # Setup LFS and pull files if needed",
        "            if [ -f \".gitattributes\" ] && grep -q \"filter=lfs\" .gitattributes; then",
        "                echo \"LFS detected, pulling LFS files...\"",
        "                git lfs pull",
        "            fi",
        "            cd ..",
        "            echo \"Successfully cloned $repo_name\"",
        "            return 0",
        "        else",
        "            echo \"Attempt $i failed, cleaning up...\"",
        "            rm -rf \"$repo_name\" || true",
        "            sleep $((5 * i))",
        "        fi",
        "    done",
        "    ",
        "    echo \"Failed to clone $repo_name after $retries attempts\"",
        "    return 1",
        "}"
    ]
    
    # Add repository cloning commands
    for i, (url, branch) in enumerate(zip(
        workspace_config.get('github_urls', []),
        workspace_config.get('github_branches', [])
    )):
        if workspace_config.get('github_pat'):
            # Use PAT if provided
            auth_url = url.replace(
                'https://',
                f'https://{workspace_config["github_pat"]}@'
            )
            init_script.append(f"clone_repo '{auth_url}' '{branch}'")
        else:
            init_script.append(f"clone_repo '{url}' '{branch}'")

    # Enhanced post-initialization steps
    init_script.extend([
        # Setup VS Code workspace with settings
        "if [ $(ls -1 | wc -l) -gt 1 ]; then",
        "    echo 'Creating multi-root workspace...'",
        "    echo '{' > workspace.code-workspace",
        "    echo '  \"folders\": [' >> workspace.code-workspace",
        "    first=true",
        "    for d in */; do",
        "        if [ \"$first\" = true ]; then",
        "            first=false",
        "        else",
        "            echo ',' >> workspace.code-workspace",
        "        fi",
        "        echo \"    {\\\"path\\\": \\\"$d\\\"}\" >> workspace.code-workspace",
        "    done",
        "    echo '  ],' >> workspace.code-workspace",
        "    echo '  \"settings\": {' >> workspace.code-workspace",
        "    echo '    \"files.autoSave\": \"afterDelay\",' >> workspace.code-workspace",
        "    echo '    \"editor.formatOnSave\": true' >> workspace.code-workspace",
        "    echo '  }' >> workspace.code-workspace",
        "    echo '}' >> workspace.code-workspace",
        "fi",
        
        # Improved dependency installation with parallel processing
        "echo 'Installing dependencies...'",
        "for d in */; do",
        "    (",  # Start subshell for parallel processing
        "    cd \"$d\"",
        "    if [ -f 'package.json' ]; then",
        "        echo \"[$(date)] Installing npm dependencies in $d...\"",
        "        if [ -f 'package-lock.json' ]; then",
        "            npm ci --silent || npm install --silent || echo \"Warning: npm install failed in $d\"",
        "        else",
        "            npm install --silent || echo \"Warning: npm install failed in $d\"",
        "        fi",
        "    fi",
        "    if [ -f 'requirements.txt' ]; then",
        "        echo \"[$(date)] Installing Python dependencies in $d...\"",
        "        python3 -m pip install --upgrade pip --quiet",
        "        pip install -r requirements.txt --quiet || echo \"Warning: pip install failed in $d\"",
        "    fi",
        "    if [ -f 'poetry.lock' ]; then",
        "        echo \"[$(date)] Installing Poetry dependencies in $d...\"",
        "        poetry install --no-interaction --quiet || echo \"Warning: poetry install failed in $d\"",
        "    fi",
        "    ) &",  # Run in background
        "done",
        "wait",  # Wait for all background processes
        
        # Workspace readiness checks
        "echo 'Performing workspace readiness checks...'",
        "ERROR_COUNT=0",
        "for d in */; do",
        "    cd \"$d\"",
        "    if [ -f 'package.json' ] && [ ! -d 'node_modules' ]; then",
        "        echo \"Warning: node_modules missing in $d\"",
        "        ((ERROR_COUNT++))",
        "    fi",
        "    if [ -f '.gitattributes' ] && grep -q \"filter=lfs\" .gitattributes; then",
        "        if ! git lfs ls-files | grep -q .; then",
        "            echo \"Warning: LFS files not properly pulled in $d\"",
        "            ((ERROR_COUNT++))",
        "        fi",
        "    fi",
        "    cd ..",
        "done",
        
        # Final status update
        "if [ $ERROR_COUNT -gt 0 ]; then",
        "    echo \"Workspace initialization completed with $ERROR_COUNT warnings\"",
        "else",
        "    echo 'Workspace initialization completed successfully'",
        "fi",
        'echo "Complete" > /workspaces/.init-status',
        "echo \"[$(date)] Initialization finished\" >> $LOGFILE",
        "touch /workspaces/.pool-workspace-initialized"
    ])
    
    return "\n".join(init_script)
