#!/usr/bin/env bash
# Server-side setup for the Aimo Agent Gateway on an Ubuntu VM (e.g. a Verda CPU instance).
# Run as root from the app directory; deploy.sh does this for you. Safe to re-run:
# it updates dependencies, keeps data/ and .env, and restarts the service.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=aimo-gateway
PORT="${PORT:-80}"
cd "$APP_DIR"

echo "==> Waiting for first-boot setup to finish (cloud-init / automatic updates)"
command -v cloud-init >/dev/null && cloud-init status --wait >/dev/null 2>&1 || true

echo "==> System packages"
export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -o DPkg::Lock::Timeout=600 -qq)
for attempt in 1 2 3; do "${APT[@]}" update && break || sleep 10; done
"${APT[@]}" install -y python3 python3-venv python3-pip ufw curl >/dev/null

echo "==> Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Configuration"
if [ ! -f .env ]; then
  echo "Missing $APP_DIR/.env (deploy.sh copies the API key from your computer)" >&2
  exit 1
fi
if ! grep -q '^DEMO_PASSWORD=.\+' .env; then
  password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
  printf '\nDEMO_USER=aimo\nDEMO_PASSWORD=%s\n' "$password" >> .env
  echo "    generated a site password"
fi
chmod 600 .env

echo "==> Challenge data"
if [ ! -f data/valio/valio_aimo_junction26_product_data.json ]; then
  .venv/bin/python scripts/download_data.py
else
  echo "    already downloaded"
fi

echo "==> Service user"
id -u aimo >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin aimo
mkdir -p data/runtime
chown -R aimo:aimo "$APP_DIR"

echo "==> systemd service ($SERVICE, port $PORT)"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Aimo Agent Gateway (Junction 2026)
After=network-online.target
Wants=network-online.target

[Service]
User=aimo
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn gateway.app:app --host 0.0.0.0 --port $PORT
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"

echo "==> Firewall: only SSH and HTTP"
ufw allow 22/tcp >/dev/null
ufw allow "$PORT/tcp" >/dev/null
ufw --force enable >/dev/null

echo "==> Waiting for the app to start (first start builds the catalog index)"
for _ in $(seq 1 90); do
  if curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
    curl -fs "http://127.0.0.1:$PORT/api/health"
    echo
    echo "==> Running"
    exit 0
  fi
  sleep 1
done
echo "The app did not come up. Last log lines:" >&2
journalctl -u "$SERVICE" -n 40 --no-pager >&2
exit 1
