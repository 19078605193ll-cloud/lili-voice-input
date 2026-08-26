FROM node:22-bookworm-slim AS web-builder
WORKDIR /workspace
COPY package.json package-lock.json ./
COPY packages/browser/package.json packages/browser/package.json
COPY apps/demo/package.json apps/demo/package.json
RUN npm ci
COPY tokens.css ./tokens.css
COPY scripts ./scripts
COPY packages/browser ./packages/browser
COPY apps/demo ./apps/demo
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/server
COPY server/pyproject.toml server/uv.lock server/README.md ./
COPY server/src ./src
RUN uv sync --frozen --no-dev
COPY --from=web-builder /workspace/packages/browser/dist /app/static/sdk
COPY --from=web-builder /workspace/apps/demo/dist /app/static/demo
ENV STATIC_ROOT=/app/static \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=9100
EXPOSE 9100
CMD ["uv", "run", "uvicorn", "lili_voice_input.main:app", "--host", "0.0.0.0", "--port", "9100"]
