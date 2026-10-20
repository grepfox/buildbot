#!/bin/bash
# One-shot deploy: writes .env with your uid/paths, builds the image, starts the bot.
set -e
cd "$(dirname "$0")"
cat > .env <<ENV
UID=$(id -u)
GID=$(id -g)
USERNAME=$(id -un)
SRC_DIR=${SRC_DIR:-$HOME/y}
SRC_DIR_L=${SRC_DIR_L:-$HOME/l}
CCACHE_DIR=${CCACHE_DIR:-$HOME/.cache/ccache}
ENV
mkdir -p logs "${CCACHE_DIR:-$HOME/.cache/ccache}"
docker compose up -d --build
docker compose logs -f --tail=20
