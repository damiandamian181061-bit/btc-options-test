#!/bin/sh
# Start TimescaleDB with the user, password and database name taken from DATABASE_URL, so the
# password lives in one place (.env). Expects postgresql://USER:PASSWORD@HOST:PORT/DBNAME with a
# URL-safe password (letters and digits; the server's is 64 hex characters).
# The values only matter the first time, when PostgreSQL creates its data directory.
set -eu

: "${DATABASE_URL:?DATABASE_URL is not set}"

rest=${DATABASE_URL#*://}
userinfo=${rest%%@*}
path=${rest#*/}

POSTGRES_USER=${userinfo%%:*}
POSTGRES_PASSWORD=${userinfo#*:}
POSTGRES_DB=${path%%\?*}
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB
unset DATABASE_URL rest userinfo path

if [ -z "$POSTGRES_USER" ] || [ -z "$POSTGRES_PASSWORD" ] || [ -z "$POSTGRES_DB" ]; then
    echo "db-entrypoint: DATABASE_URL must look like postgresql://user:password@db:5432/name" >&2
    exit 64
fi

exec docker-entrypoint.sh "$@"
