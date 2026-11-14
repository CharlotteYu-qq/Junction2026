# Deploying to Verda

The gateway runs on one small **CPU instance** (Ubuntu VM). Verda's serverless containers are for
GPU inference, so they aren't a good fit for a web app.

Junction 2026 teams get Verda credits (Junction Platform → Hackerpacks → Verda). Building on Verda also
makes us eligible for **Best use of Verda**: document how we used Verda and submit it on the Junction Platform.

## Once: SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_verda -C verda
cat ~/.ssh/id_ed25519_verda.pub      # paste this into Verda: Keys -> SSH keys -> Add
```

## Once: create the instance (Verda console)

1. **Instances → Deploy instance**
2. Contract **On-demand** (billed in 10-minute steps while it exists). Don't pick Spot: Verda can stop it at any time.
3. Pick the **smallest CPU instance** (4 vCPU / 16 GB, about $0.03 per hour).
4. OS image: **Ubuntu 24.04** (22.04 also works).
5. SSH key: the `verda` key from above. Hostname: `aimo-gateway`.
6. **Deploy now**, wait until it's running, and copy its **IP address**.

## Deploy / update (from this repo, in Git Bash)

```bash
bash deploy/deploy.sh <IP>          # uses ~/.ssh/id_ed25519_verda by default
```

The script uploads the **last commit**, copies the Valio API key from your `.env` (first deploy only),
installs Python and the dependencies, downloads the challenge data, and starts a systemd service on port 80.
It also turns on the firewall (only SSH and HTTP open) and prints the URL plus the two sign-in passwords.

To update later, commit your changes and run the same command again. Data and orders on the server are kept.

## HTTPS on a name (optional, free)

[sslip.io](https://sslip.io) turns any name that contains the IP into that IP, so no domain purchase is needed:

```bash
DOMAIN=aimo.135-181-63-188.sslip.io bash deploy/deploy.sh 135.181.63.188
```

This installs Caddy, which gets a free Let's Encrypt certificate and renews it automatically. The app then only
listens on `127.0.0.1:8000`, and plain `http://<IP>` redirects to the HTTPS name. The domain is remembered on the
server, so later deploys don't need `DOMAIN=` again. A real domain works the same way: point an A record at the IP
and pass that name instead.

Current deployment: **https://aimo.135-181-63-188.sslip.io** (buyer portal; password in the team chat).

## Useful commands on the server

```bash
ssh -i ~/.ssh/id_ed25519_verda root@<IP>
systemctl status aimo-gateway            # is it running?
journalctl -u aimo-gateway -f            # live logs
grep _PASSWORD /opt/aimo-gateway/.env   # sign-in passwords
```

To change the password, edit `BUYER_PASSWORD` in `/opt/aimo-gateway/.env` and run `systemctl restart aimo-gateway`.

## Notes

- The site opens on the buyer sign-in (one account per customer, `BUYER_PASSWORD`). The server checks the
  customer on every API call.
  `/api/health` and `/.well-known/aimo-agent-gateway.json` stay open.
- Without `DOMAIN` it's plain HTTP on the IP address (see the HTTPS section above).
- **Delete the instance after Junction.** An on-demand instance keeps costing money until it is deleted.
