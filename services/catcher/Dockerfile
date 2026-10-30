# syntax=docker/dockerfile:1
# homie-catcher: Python 3.12 + Claude Code CLI 2.1.312 (native binary, signature-verified)
# + plane-mcp-server 0.2.10 (stdio). Multi-arch base; built for linux/arm64 in CI.

ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

# ---------------------------------------------------------------- Claude Code
# Official native release bucket. We verify: release-signing key fingerprint (from
# code.claude.com/docs/en/setup "Binary integrity"), the detached GPG signature on
# manifest.json, then the binary's SHA256 against the signed manifest.
FROM ${PYTHON_IMAGE} AS claude
ARG TARGETARCH
ARG CLAUDE_CODE_VERSION=2.1.312
ARG CLAUDE_KEY_FPR=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
RUN set -eu; \
    case "${TARGETARCH:-arm64}" in arm64) plat=linux-arm64 ;; amd64) plat=linux-x64 ;; *) echo "unsupported arch" >&2; exit 1 ;; esac; \
    base=https://downloads.claude.ai/claude-code-releases; \
    export GNUPGHOME=/dl/gnupg; mkdir -m 700 "$GNUPGHOME"; \
    curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o key.asc; \
    gpg --batch --import key.asc; \
    gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${CLAUDE_KEY_FPR}:"; \
    curl -fsSLO "$base/${CLAUDE_CODE_VERSION}/manifest.json"; \
    curl -fsSLO "$base/${CLAUDE_CODE_VERSION}/manifest.json.sig"; \
    gpg --batch --verify manifest.json.sig manifest.json; \
    sum=$(python3 -c "import json,sys; print(json.load(open('manifest.json'))['platforms'][sys.argv[1]]['checksum'])" "$plat"); \
    curl -fsSL "$base/${CLAUDE_CODE_VERSION}/$plat/claude" -o claude; \
    echo "$sum  claude" | sha256sum -c -; \
    chmod 0755 claude; \
    ./claude --version | grep -q "^${CLAUDE_CODE_VERSION} "

# ---------------------------------------------------------------- Python venvs
FROM ${PYTHON_IMAGE} AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-mcp.txt /src/
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /src/requirements.txt \
 && python -m venv /opt/plane-mcp \
 && /opt/plane-mcp/bin/pip install --no-cache-dir -r /src/requirements-mcp.txt \
 && /opt/plane-mcp/bin/python -c "import plane_mcp.server" \
 && test -x /opt/plane-mcp/bin/plane-mcp-server

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
RUN useradd --uid 10001 --create-home --home-dir /home/catcher --shell /usr/sbin/nologin catcher \
 && mkdir -p /data && chown 10001:10001 /data
COPY --from=claude /dl/claude /usr/local/bin/claude
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/plane-mcp /opt/plane-mcp
COPY catcher /app/catcher
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/catcher \
    CLAUDE_BIN=/usr/local/bin/claude \
    PLANE_MCP_BIN=/opt/plane-mcp/bin/plane-mcp-server \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    DB_PATH=/data/catcher.db
WORKDIR /app
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "catcher"]
