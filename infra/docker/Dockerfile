# The optlab image: runs the recorder and health services (and later the other workers).
# Built by infra/dc from the repo root; .dockerignore keeps .env, data/ and .git out of it.

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.12.16-slim-trixie

# Same uid as the server's deploy user, so files written to data/ belong to deploy.
ARG APP_UID=1000
ARG APP_GID=1000

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies first (cached between builds), then the package itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Migrations, run by hand after a backup: infra/dc run --rm recorder alembic -c infra/alembic.ini upgrade head
COPY infra/alembic.ini ./infra/alembic.ini
COPY infra/migrations ./infra/migrations

RUN groupadd --gid "$APP_GID" optlab \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home --shell /usr/sbin/nologin optlab \
    && mkdir -p /app/data \
    && chown optlab:optlab /app/data
USER optlab

CMD ["optlab-recorder"]
