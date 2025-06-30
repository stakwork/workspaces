# Use a default image for your main container, this is an example for a Ruby project but this could be Go, Java, etc.
FROM mcr.microsoft.com/devcontainers/ruby:3.1

# Install postgres client package and utilities
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get -y install --no-install-recommends postgresql wget sed

# Install gh command line (Github CLI)
RUN sudo mkdir -p -m 755 /etc/apt/keyrings \
        && out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        && cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && sudo apt update -y \
    && sudo apt install gh -y

# Install JavaScript dependencies
ARG NODE_VERSION=20.19.2
ARG YARN_VERSION=1.22.21
ENV PATH=/usr/local/node/bin:$PATH
RUN curl -sL https://github.com/nodenv/node-build/archive/master.tar.gz | tar xz -C /tmp/ && \
    /tmp/node-build-master/bin/node-build "${NODE_VERSION}" /usr/local/node && \
    npm install -g yarn@$YARN_VERSION && \
    rm -rf /tmp/node-build-master

# Ensure the PATH includes npm global bin directory for all users
RUN echo 'export PATH="/usr/local/node/bin:$PATH"' >> /etc/profile && \
echo 'export PATH="/usr/local/node/bin:$PATH"' >> /etc/bash.bashrc

# IMPORTANT: Install PM2 globally and ensure it's accessible
RUN npm install -g pm2 && \
    # Create symlink in /usr/local/bin to ensure it's always in PATH
    ln -sf /usr/local/node/bin/pm2 /usr/local/bin/pm2 && \
    # Verify installation
    pm2 --version
