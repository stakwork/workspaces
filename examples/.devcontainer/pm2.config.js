module.exports = {
  apps: [
    {
      name: "frontend",
      script: "rails s -b 0.0.0.0",
      cwd: "/workspaces/my-project",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "1G",
      env: {
        INSTALL_COMMAND: "sh /workspaces/my-project/.devcontainer/setup.sh",
        PORT: "3000"
      }
    },
    {
      name: "frontend_assets",
      script: "bin/webpack-dev-server",
      cwd: "/workspaces/my-project",
      instances: 1,
      interpreter: 'ruby',
      autorestart: true,
      watch: false,
      env: {
        INSTALL_COMMAND: "yarn install"
      }
    }
  ],
};
