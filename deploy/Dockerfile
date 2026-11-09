# One image, two roles: `python -m portunus_gateway` (gateway) and `portunus-runner` (runner).
# Build context: repository root.

FROM node:22-slim AS dashboard
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim
# node/npx for the runner: copied from the official node image (no apt, small).
COPY --from=dashboard /usr/local/bin/node /usr/local/bin/node
COPY --from=dashboard /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ARG WITH_POSTGRES=0
WORKDIR /app
COPY packages/portunus_shared packages/portunus_shared
COPY runner runner
COPY gateway gateway
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
