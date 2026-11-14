# The optlab image: runs the recorder, health, jobs and dashboard services (and later the other
# workers). Built by infra/dc from the repo root; .dockerignore keeps .env, data/ and .git out.

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# rclone for the R2 backups: the binary from the official image, pinned by digest (1.75.2).
FROM rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5 AS rclone

FROM python:3.12.16-slim-trixie

# Same uid as the server's deploy user, so files written to data/ belong to deploy.
ARG APP_UID=1000
ARG APP_GID=1000

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies first (cached between builds), then the package itself. The dashboard group
# (Streamlit) is included: the dashboard service runs from the same image.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --group dashboard --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --group dashboard --no-editable

# Settings files (data-quality thresholds) and the Streamlit pages.
COPY configs ./configs
COPY apps/dashboard ./apps/dashboard

# Migrations, run by hand after a backup: infra/dc run --rm recorder alembic -c infra/alembic.ini upgrade head
COPY infra/alembic.ini ./infra/alembic.ini
COPY infra/migrations ./infra/migrations

RUN groupadd --gid "$APP_GID" optlab \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home --shell /usr/sbin/nologin optlab \
    && mkdir -p /app/data \
    && chown optlab:optlab /app/data
USER optlab

CMD ["optlab-recorder"]
