#!/usr/bin/env bash
# Ship this stack to the home server and start it.
#
#   ./deploy.sh                 # deploy to kota@server:~/distributed-task-queue
#   REMOTE=other ./deploy.sh    # different host
#
# Copies the working tree (never .git, never .env), leaves the remote .env
# alone if it already exists, then runs docker compose up -d --build.
#
# The API expects the monitoring stack's network to exist, because Prometheus
# scrapes it there. Bring that stack up first if the build complains about a
# missing external network.

set -euo pipefail

REMOTE="${REMOTE:-kota@server}"
REMOTE_DIR="${REMOTE_DIR:-distributed-task-queue}"

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "no .env in the working tree; the remote one will be used if present" >&2
fi

echo "packing working tree"
tar czf - \
  --exclude=.git \
  --exclude=.env \
  --exclude=.venv \
  --exclude=__pycache__ \
  --exclude='*.pyc' \
  --exclude='*.db' \
  . | ssh "$REMOTE" "mkdir -p \"\$HOME/$REMOTE_DIR\" && tar xzf - -C \"\$HOME/$REMOTE_DIR\""

echo "starting stack on $REMOTE"
ssh "$REMOTE" "cd \"\$HOME/$REMOTE_DIR\" && test -f .env || cp .env.example .env && docker compose up -d --build"

echo
ssh "$REMOTE" "cd \"\$HOME/$REMOTE_DIR\" && docker compose ps --format 'table {{.Name}}\t{{.Status}}'"
