#!/usr/bin/env bash
# Server-side setup for the Aimo Agent Gateway on an Ubuntu VM (e.g. a Verda instance).
# Run as root from the app directory; deploy.sh does this for you. Safe to re-run:
# it updates dependencies, keeps data/ and .env, and restarts the service.
#
# With DOMAIN set (once; it is remembered in .env), Caddy serves the site over HTTPS on that name
# with a free Let's Encrypt certificate, and the app itself only listens on 127.0.0.1:8000.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=aimo-gateway
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "Missing $APP_DIR/.env (deploy.sh copies the API key from your computer)" >&2
  exit 1
fi

# Domain: given now (and remembered), or remembered from an earlier deploy
if [ -n "${DOMAIN:-}" ]; then
  sed -i '/^DOMAIN=/d' .env
  printf 'DOMAIN=%s\n' "$DOMAIN" >> .env
else
  DOMAIN="$(sed -n 's/^DOMAIN=//p' .env | tail -n1)"
fi
if [ -n "$DOMAIN" ]; then BIND=127.0.0.1; APP_PORT=8000; else BIND=0.0.0.0; APP_PORT=80; fi

echo "==> Waiting for first-boot setup to finish (cloud-init / automatic updates)"
command -v cloud-init >/dev/null && cloud-init status --wait >/dev/null 2>&1 || true

echo "==> System packages"
export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -o DPkg::Lock::Timeout=600 -qq)
for _ in 1 2 3; do "${APT[@]}" update && break || sleep 10; done
"${APT[@]}" install -y python3 python3-venv python3-pip ufw curl >/dev/null

echo "==> Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Configuration"
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

echo "==> systemd service ($SERVICE on $BIND:$APP_PORT)"
if [ -z "$DOMAIN" ] && systemctl is-active -q caddy 2>/dev/null; then
  systemctl disable --now -q caddy  # no domain any more: the app takes port 80 itself
fi
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Aimo Agent Gateway (Junction 2026)
After=network-online.target
Wants=network-online.target

[Service]
User=aimo
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn gateway.app:app --host $BIND --port $APP_PORT
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"

if [ -n "$DOMAIN" ]; then
  echo "==> HTTPS for $DOMAIN (Caddy, free Let's Encrypt certificate)"
  if ! command -v caddy >/dev/null; then
    if ! "${APT[@]}" install -y caddy >/dev/null 2>&1; then
      # not in the distro repositories: use Caddy's official repository
      "${APT[@]}" install -y debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
      curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
      "${APT[@]}" update
      "${APT[@]}" install -y caddy >/dev/null
    fi
  fi
  cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode zstd gzip
	reverse_proxy 127.0.0.1:$APP_PORT
}

# Anything else on plain HTTP (for example the bare IP address) goes to the HTTPS name
:80 {
	redir https://$DOMAIN{uri} permanent
}
EOF
  systemctl enable -q caddy
  systemctl restart caddy
fi

echo "==> Firewall: SSH, HTTP$([ -n "$DOMAIN" ] && echo ", HTTPS")"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
if [ -n "$DOMAIN" ]; then ufw allow 443/tcp >/dev/null; fi
ufw --force enable >/dev/null

echo "==> Waiting for the app to start (first start builds the catalog index)"
up=""
for _ in $(seq 1 90); do
  if curl -fs -o /dev/null "http://127.0.0.1:$APP_PORT/api/health"; then up=1; break; fi
  sleep 1
done
if [ -z "$up" ]; then
  echo "The app did not come up. Last log lines:" >&2
  journalctl -u "$SERVICE" -n 40 --no-pager >&2
  exit 1
fi
curl -fs "http://127.0.0.1:$APP_PORT/api/health"
echo

if [ -n "$DOMAIN" ]; then
  echo "==> Waiting for the HTTPS certificate"
  for _ in $(seq 1 60); do
    if curl -fs -o /dev/null "https://$DOMAIN/api/health"; then
      echo "    https://$DOMAIN is live"
      exit 0
    fi
    sleep 2
  done
  echo "WARNING: HTTPS is not ready yet. Caddy log:" >&2
  journalctl -u caddy -n 25 --no-pager >&2
  exit 1
fi
echo "==> Running"
