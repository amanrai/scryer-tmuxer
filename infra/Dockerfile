# syntax=docker/dockerfile:1
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV NVM_DIR=/root/.nvm
ENV PATH=$NVM_DIR/versions/node/v22/bin:$PATH

# Layer 1: system packages — rebuild only if this list changes
RUN apt-get update && apt-get install -y \
    tmux \
    python3 \
    python3-pip \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Layer 2: NVM + Node — rebuild only if NVM version or Node version changes
RUN curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash \
    && . $NVM_DIR/nvm.sh \
    && nvm install 22 \
    && nvm use 22

# Layer 3: npm global packages — rebuild only if package list changes
RUN . $NVM_DIR/nvm.sh \
    && npm install -g --force \
        @anthropic-ai/claude-code \
        @openai/codex \
        @google/gemini-cli

COPY infra/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

# Layer 4: Python deps — rebuild only if requirements.txt changes
COPY api/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install -r requirements.txt

# Layer 5: application code — rebuilds on every code change
COPY api/ .

EXPOSE 5678

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "server.py"]
