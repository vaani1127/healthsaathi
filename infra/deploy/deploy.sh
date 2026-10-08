#!/bin/sh
# Runs on the VM. Loads a new API image, migrates, switches over, and rolls back if the new
# version does not become healthy.
#
#   sh deploy.sh v0.1.0 /tmp/healthsaathi-api-v0.1.0.tar.gz
set -eu

TAG="$1"
IMAGE_TAR="$2"
APP_DIR=/opt/healthsaathi
COMPOSE="docker compose -f $APP_DIR/docker-compose.prod.yml"
HEALTH_URL="https://${API_DOMAIN:-$(grep '^API_DOMAIN=' "$APP_DIR/.env" | cut -d= -f2)}/api/v1/ready"

cd "$APP_DIR"
PREVIOUS="$(cat current_tag 2>/dev/null || true)"

echo "loading image $TAG"
gunzip -c "$IMAGE_TAR" | docker load
rm -f "$IMAGE_TAR"

echo "running migrations"
HS_TAG="$TAG" $COMPOSE --profile migrate run --rm migrate

echo "starting $TAG"
HS_TAG="$TAG" $COMPOSE up -d --remove-orphans api worker caddy

healthy=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 4
done

if [ "$healthy" -ne 1 ]; then
  echo "$TAG did not become healthy" >&2
  if [ -n "$PREVIOUS" ]; then
    # Migrations are additive (expand first, contract in a later release), so the previous
    # version still runs against the migrated schema.
    echo "rolling back to $PREVIOUS" >&2
    HS_TAG="$PREVIOUS" $COMPOSE up -d --remove-orphans api worker caddy
  fi
  exit 1
fi

echo "$TAG" >current_tag
# Keep the previous image for rollback, drop older ones.
docker image ls healthsaathi-api --format '{{.Tag}}' |
  grep -v -e "^$TAG\$" -e "^${PREVIOUS:-none}\$" |
  xargs -r -I{} docker image rm "healthsaathi-api:{}" >/dev/null 2>&1 || true
echo "deployed $TAG"
