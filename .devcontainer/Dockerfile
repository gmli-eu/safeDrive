ARG CLAUDE_CODE_VERSION=latest
ARG GEMINI_CLI_VERSION=latest
ARG GIT_DELTA_VERSION=0.20.1
ARG ZSH_IN_DOCKER_VERSION=1.2.1

FROM node:24

# Install basic development tools and iptables/ipset
RUN apt-get update && apt-get install -y --no-install-recommends \
  less \
  git \
  procps \
  sudo \
  fzf \
  zsh \
  man-db \
  unzip \
  gnupg2 \
  gh \
  iptables \
  ipset \
  iproute2 \
  dnsutils \
  aggregate \
  jq \
  ripgrep \
  nano \
  vim \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Ensure default node user has access to /usr/local/share
RUN mkdir -p /usr/local/share/npm-global && \
  chown -R node:node /usr/local/share

ARG USERNAME=node

# Persist bash history.
RUN SNIPPET="export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  && mkdir /commandhistory \
  && touch /commandhistory/.bash_history \
  && chown -R $USERNAME /commandhistory

# Set `DEVCONTAINER` environment variable to help with orientation
ENV DEVCONTAINER=true

# Create workspace and config directories and set permissions
RUN mkdir -p /workspace /home/node/.claude /home/node/.gemini && \
  chown -R node:node /workspace /home/node/.claude /home/node/.gemini

WORKDIR /workspace

ARG GIT_DELTA_VERSION
RUN ARCH=$(dpkg --print-architecture) && \
  wget "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  sudo dpkg -i "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  rm "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb"

# Set up non-root user
USER node

# Install global packages
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=$PATH:/usr/local/share/npm-global/bin

# Install pnpm globally
RUN npm install -g pnpm
RUN pnpm -v

# Set the default shell to zsh rather than sh
ENV SHELL=/bin/zsh

# Set the default editor and visual
ENV EDITOR=nano
ENV VISUAL=nano

# Default powerline10k theme
ARG ZSH_IN_DOCKER_VERSION
RUN sh -c "$(wget -O- https://github.com/deluan/zsh-in-docker/releases/download/v${ZSH_IN_DOCKER_VERSION}/zsh-in-docker.sh)" -- \
  -p git \
  -p fzf \
  -a "source /usr/share/doc/fzf/examples/key-bindings.zsh" \
  -a "source /usr/share/doc/fzf/examples/completion.zsh" \
  -a "export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  -x

# Install Claude
ARG CLAUDE_CODE_VERSION
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# Install Gemini CLI (API key is read from /workspace/.env)
ARG GEMINI_CLI_VERSION
RUN npm install -g @google/gemini-cli@${GEMINI_CLI_VERSION}


# Install the Wendy CLI (deploys jetson-hub / atech-flasher to the Jetson)
ARG WENDY_VERSION=2026.11.13-154136
USER root
RUN ARCH=$(dpkg --print-architecture) && \
  wget -qO /tmp/wendy.tgz "https://github.com/wendylabsinc/wendy-agent/releases/download/${WENDY_VERSION}/wendy-cli-linux-${ARCH}-${WENDY_VERSION}.tar.gz" && \
  tar -xzf /tmp/wendy.tgz -C /tmp && \
  install -m 755 "/tmp/wendy-cli-linux-${ARCH}/wendy" /usr/local/bin/wendy && \
  rm -rf /tmp/wendy.tgz "/tmp/wendy-cli-linux-${ARCH}" && \
  mkdir -p /home/node/.wendy && chown node:node /home/node/.wendy && chmod 700 /home/node/.wendy

# Copy and set up firewall script
COPY init-firewall.sh /usr/local/bin/
USER root
RUN chmod +x /usr/local/bin/init-firewall.sh && \
  echo "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/node-firewall && \
  chmod 0440 /etc/sudoers.d/node-firewall
USER node