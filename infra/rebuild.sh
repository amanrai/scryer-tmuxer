#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

FORCE=${1:-}

docker compose down --remove-orphans

if [ "$FORCE" = "--force" ]; then
    DOCKER_BUILDKIT=1 docker compose build --no-cache
else
    DOCKER_BUILDKIT=1 docker compose build
fi

docker compose up -d
