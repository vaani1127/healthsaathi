#!/bin/sh
# Runs on the VM. Writes a compressed pg_dump of the production database to stdout. The nightly
# workflow encrypts it before it leaves the runner.
set -eu
URL="$(grep '^BACKUP_DATABASE_URL=' /opt/healthsaathi/.env | cut -d= -f2-)"
if [ -z "$URL" ]; then
  echo "BACKUP_DATABASE_URL is not set in /opt/healthsaathi/.env" >&2
  exit 1
fi
docker run --rm postgres:16-alpine pg_dump --format=custom --no-owner "$URL"
