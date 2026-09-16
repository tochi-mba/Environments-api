# syntax=docker/dockerfile:1

FROM python:3.11-slim

# util-linux: unshare and setpriv (namespace tier); passwd: useradd/userdel (user tier);
# procps is convenient for debugging inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux passwd bash procps ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /srv/environments-api
COPY pyproject.toml uv.lock README.md ./
# The token exists only for this RUN, in git's process environment, never a layer.
# Without a secret, public sources are fetched anonymously.
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --frozen --no-dev

ENV ENVAPI_ROOT=/var/lib/envapi \
    ENVAPI_MIN_SANDBOX_TIER=user \
    PATH="/srv/environments-api/.venv/bin:$PATH"
VOLUME ["/var/lib/envapi"]
EXPOSE 8008

# Runs as root on purpose: the user and namespace tiers need it to create per-environment
# users and drop into them. Run the container with --privileged (or CAP_SYS_ADMIN plus an
# unconfined seccomp profile) to get the namespace tier; /health/ready reports the result.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"]
