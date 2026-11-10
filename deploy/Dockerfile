# One image, two roles: `python -m portunus_gateway` (gateway) and `portunus-runner` (runner).
# Build context: repository root.
#
# Base images are overridable so anyone can build without a private registry:
# defaults are public Docker Hub tags. CN builders (same pattern as sibling
# products) pass SWR mirrors, e.g.:
#   --build-arg PY_BASE=swr.cn-north-4.myhuaweicloud.com/<org>/base-python:3.12-slim
#   --build-arg NODE_BASE=swr.cn-north-4.myhuaweicloud.com/<org>/base-node:22-slim
# Package indexes (optional; empty keeps upstream defaults). CN builders
# typically use the same indexes as sibling products:
#   --build-arg PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
#   --build-arg NPM_REGISTRY=https://registry.npmmirror.com
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
# Install uv via pip (avoids ghcr.io, which is slow/unreliable on some networks).
ARG PIP_INDEX
RUN pip install --no-cache-dir ${PIP_INDEX:+-i ${PIP_INDEX}} "uv==0.9.30" \
    && ln -sf /usr/local/bin/uv /usr/local/bin/uvx

ARG WITH_POSTGRES=0
WORKDIR /app
COPY packages/portunus_shared packages/portunus_shared
COPY runner runner
COPY gateway gateway
# Re-declare so this layer sees the index (ARG scope is per-stage otherwise).
ARG PIP_INDEX
# uv 0.13 reads UV_DEFAULT_INDEX; keep UV_INDEX_URL as a compat alias.
ENV UV_DEFAULT_INDEX=${PIP_INDEX} \
    UV_INDEX_URL=${PIP_INDEX}
RUN cd gateway && uv sync --frozen --no-dev \
    && uv pip install --python .venv/bin/python -e ../runner \
    && if [ "$WITH_POSTGRES" = "1" ]; then uv pip install --python .venv/bin/python asyncpg; fi
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
