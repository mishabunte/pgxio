#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v web_password="$PGXIO_WEB_PASSWORD" -f /example.sql
