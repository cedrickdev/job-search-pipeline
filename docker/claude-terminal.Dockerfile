FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.89

RUN groupadd --gid 10001 claude \
    && useradd --uid 10001 --gid claude --create-home --home-dir /workspace claude \
    && mkdir -p /workspace/.claude-config \
    && chown 10001:10001 /workspace/.claude-config \
    && npm install --global --no-audit --no-fund @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

COPY docker/claude-managed-settings.json /etc/claude-code/managed-settings.json

ENV CLAUDE_CONFIG_DIR=/workspace/.claude-config
WORKDIR /workspace
USER claude
CMD ["sleep", "infinity"]
