# One image, two roles: `python -m portunus_gateway` (gateway) and `portunus-runner` (runner).
# Build context: repository root.
#
# Base images are overridable so anyone can build without a private registry:
# defaults are public Docker Hub tags. Optional private/regional mirrors, e.g.:
#   --build-arg PY_BASE=registry.example.com/base-python:3.12-slim
#   --build-arg NODE_BASE=registry.example.com/base-node:22-slim
# Package indexes (optional; empty keeps upstream defaults):
#   --build-arg PIP_INDEX=https://pypi.example.com/simple/
#   --build-arg NPM_REGISTRY=https://npm.example.com
#
# NODE_BASE must be glibc/Debian-based: the runtime stage copies `node`/`npm`
# into the Python image for the runner. Do not point NODE_BASE at an Alpine tag.

ARG NODE_BASE=node:22-slim
ARG PY_BASE=python:3.12-slim

FROM ${NODE_BASE} AS dashboard
WORKDIR /dashboard
ARG NPM_REGISTRY
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci ${NPM_REGISTRY:+--registry=${NPM_REGISTRY}}
COPY dashboard/ ./
RUN npm run build

FROM ${PY_BASE}
# node/npx for the runner: copied from the node stage (no apt, small).
COPY --from=dashboard /usr/local/bin/node /usr/local/bin/node
COPY --from=dashboard /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Fleet provider updates (rclone provision + skill rsync) need these on the worker.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates curl openssh-client rsync unzip \
    && rm -rf /var/lib/apt/lists/*
# Install uv via pip (avoids ghcr.io, which is slow/unreliable on some networks).
ARG PIP_INDEX
RUN pip install --no-cache-dir ${PIP_INDEX:+-i ${PIP_INDEX}} "uv==0.9.30" \
    && ln -sf /usr/local/bin/uv /usr/local/bin/uvx

# WITH_POSTGRES kept for compose/build compat; asyncpg is always in gateway deps.
ARG WITH_POSTGRES=0
WORKDIR /app
COPY packages/portunus_shared packages/portunus_shared
# gateway/uv.lock pins portunus_skill_bridge as a path dependency; the COPY was
# missing after fee5547, breaking clean-checkout builds (prod had a local hotfix).
COPY packages/portunus_skill_bridge packages/portunus_skill_bridge
COPY packages/portunus_worker packages/portunus_worker
COPY runner runner
COPY gateway gateway
COPY deploy/fleet-skills.sh deploy/fleet-skills.sh
COPY deploy/fleet-providers-update.sh deploy/fleet-providers-update.sh
COPY deploy/provision-rclone-shim.sh deploy/provision-rclone-shim.sh
COPY deploy/rclone-shim/shim deploy/rclone-shim/shim
RUN chmod 755 deploy/fleet-skills.sh deploy/fleet-providers-update.sh \
      deploy/provision-rclone-shim.sh deploy/rclone-shim/shim
# Re-declare so this layer sees the index (ARG scope is per-stage otherwise).
ARG PIP_INDEX
# uv 0.13 reads UV_DEFAULT_INDEX; keep UV_INDEX_URL as a compat alias.
ENV UV_DEFAULT_INDEX=${PIP_INDEX} \
    UV_INDEX_URL=${PIP_INDEX}
RUN cd gateway && uv sync --frozen --no-dev \
    && uv pip install --python .venv/bin/python -e ../runner \
    && uv pip install --python .venv/bin/python -e ../packages/portunus_worker
COPY --from=dashboard /dashboard/dist dashboard/dist

# Same uid for gateway and runner in compose; production uses separate users (see README).
RUN useradd --uid 10001 --create-home portunus \
    && mkdir -p /run/portunus /var/lib/portunus \
    && chown 10001:10001 /run/portunus /var/lib/portunus
ENV PATH="/app/gateway/.venv/bin:$PATH" \
    PORTUNUS_BIND=0.0.0.0:8080 \
    PORTUNUS_SOCKET=/run/portunus/runner.sock \
    PORTUNUS_DATABASE_URL=sqlite+aiosqlite:////var/lib/portunus/portunus.db
WORKDIR /app/gateway
USER 10001
EXPOSE 8080
CMD ["python", "-m", "portunus_gateway"]
