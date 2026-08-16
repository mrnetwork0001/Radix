# Deploying Radix on a fresh VPS

Target: Ubuntu 24.04, one box, Docker Compose. Only port 80 (optionally 443)
faces the internet; HydraDB never leaves the compose network.

## 1. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker compose version   # expect v2.x+
```

## 2. Clone

```bash
sudo mkdir -p /opt/radix && sudo chown $USER /opt/radix
git clone <your-radix-remote> /opt/radix
cd /opt/radix
```

## 3. Create the auth token and .env.prod

The repo ships a well-known dev token in `hydra/auth-token` — replace it.

```bash
openssl rand -hex 32 > hydra/auth-token          # 64 chars, ≥32-byte minimum
cp deploy/.env.prod.example deploy/.env.prod
# Edit deploy/.env.prod: set HYDRA_TOKEN to the exact contents of hydra/auth-token.
sed -i "s/^HYDRA_TOKEN=.*/HYDRA_TOKEN=$(cat hydra/auth-token)/" deploy/.env.prod
chmod 600 deploy/.env.prod hydra/auth-token
```

## 4. Build and start

Always run compose from the repo root with `--project-directory .`:

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml --project-directory . up -d --build
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml --project-directory . ps
```

Four services: `hydra` (database, internal), `backend` (API, internal),
`sentinel` (24/7 OSV watcher), `frontend` (nginx on :80).

## 5. Fill the graph: seed or ingest

The stack starts empty (namespace `radix-live`). Pick one:

**Ingest real repositories** (the point of the product):

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml --project-directory . \
  run --rm backend python scripts/ingest.py https://github.com/your-org/your-app
```

**Seed the demo world** instead (synthetic ecosystem, good for a first look):

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml --project-directory . \
  run --rm backend python scripts/seed_ecosystem.py
```

Both write to whatever `HYDRA_NAMESPACE` is set in `deploy/.env.prod`
(`radix-live` by default), which is the namespace the backend and sentinel
read. Re-running either is idempotent.

## 6. Verify

```bash
curl -s http://localhost/api/health
# {"status":"ok","hydra_ready":true,...,"seeded":true,"packages":<n>}
curl -sI http://localhost/ | head -1        # HTTP/1.1 200 OK
```

## 7. Logs

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml --project-directory . logs -f sentinel
```

One line per cycle, e.g.
`cycle complete: packages=390 advisories=12 malicious=1 applied=3 delta="+1 compromised, +2 versions windowed" ...`.
Backend and hydra logs the same way (`logs -f backend`, `logs -f hydra`).
Docker keeps them under `/var/lib/docker/containers/`; add a `logging:` block
or configure the daemon's log rotation if the box is long-lived.

## Optional: TLS with Caddy

Simplest path: keep the stack as-is, move the frontend off :80, and put Caddy
in front (it auto-provisions Let's Encrypt certs):

1. In `deploy/docker-compose.prod.yml`, change the frontend port mapping to
   `"127.0.0.1:8080:80"`.
2. `sudo apt-get install -y caddy`, then `/etc/caddy/Caddyfile`:

   ```
   your.domain.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```

3. `sudo systemctl reload caddy`.

Alternatively terminate TLS in the nginx container itself: mount certs, add a
`listen 443 ssl;` server block to `deploy/nginx.conf`, and uncomment the
`443:443` mapping in the compose file.

## Optional: run the sentinel bare via systemd

Instead of the compose `sentinel` service (disable it first, and uncomment the
loopback `ports:` block on the `hydra` service so 8443 is reachable from the
host): follow the install steps commented at the top of
`deploy/radix-sentinel.service`. Logs then live in
`journalctl -u radix-sentinel -f`.
