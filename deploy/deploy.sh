#!/usr/bin/env bash
# Deploy (or update) the last commit to a server over SSH. Run from the repo, in Git Bash / macOS / Linux:
#
#   bash deploy/deploy.sh <server-ip> [path/to/ssh-key]
#
# The repo is private, so the code is uploaded with `git archive` instead of cloned on the server.
# Only AIMO_API_KEY, AIMO_API_BASE_URL and ANTHROPIC_API_KEY are copied from your local .env (over SSH).
set -euo pipefail

HOST="${1:?usage: bash deploy/deploy.sh <server-ip> [path/to/ssh-key]}"
KEY="${2:-$HOME/.ssh/id_ed25519_verda}"
APP_DIR=/opt/aimo-gateway
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "root@$HOST")

cd "$(git rev-parse --show-toplevel)"
if [ ! -f .env ]; then
  echo "No .env found: copy .env.example to .env and add AIMO_API_KEY first." >&2
  exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Note: you have uncommitted changes. Only the last commit ($(git rev-parse --short HEAD)) is deployed."
fi

echo "==> Uploading $(git rev-parse --short HEAD) to $HOST:$APP_DIR"
"${SSH[@]}" "mkdir -p $APP_DIR"
git archive --format=tar HEAD | "${SSH[@]}" "tar -x -C $APP_DIR"

if ! "${SSH[@]}" "test -f $APP_DIR/.env"; then
  echo "==> Copying the API key to the server (first deploy only)"
  grep -E '^(AIMO_API_KEY|AIMO_API_BASE_URL|ANTHROPIC_API_KEY)=' .env | "${SSH[@]}" "umask 077 && cat > $APP_DIR/.env"
fi

echo "==> Running setup on the server"
"${SSH[@]}" "bash $APP_DIR/deploy/setup.sh"

echo
echo "Site:  http://$HOST/"
"${SSH[@]}" "grep -E '^DEMO_(USER|PASSWORD)=' $APP_DIR/.env" | sed 's/^DEMO_USER=/Login: /; s/^DEMO_PASSWORD=/Password: /'
