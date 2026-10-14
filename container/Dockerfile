FROM swift:6.1-noble
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates gnupg jq python3.12 python3.12-venv python3-pip ripgrep \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && apt-get install -y nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*
# Claude Code is pinned to the version on the Mac (`claude --version`); bump it in a PR when upgrading.
RUN corepack enable && corepack prepare pnpm@11.5.1 --activate \
    && npm install -g @anthropic-ai/claude-code@2.1.299 @biomejs/biome \
    && pip3 install --break-system-packages uv ruff pytest
# Install scripts stay off for npm (pnpm 11 blocks them by default).
RUN npm config set ignore-scripts true -g
# UID 501 matches the nakomis user on Cere so the bind-mounted checkout is writable.
RUN useradd -m -u 501 -s /bin/bash agent
USER agent
WORKDIR /work
RUN git config --global user.name "nakomis-bau[bot]" \
    && git config --global user.email "nakomis-bau[bot]@users.noreply.github.com" \
    && git config --global credential.helper '!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f'
ENTRYPOINT []
