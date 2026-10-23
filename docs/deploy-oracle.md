# Deploying on Oracle Cloud Always Free

A free, always-on VM that runs the whole `docker-compose.yml` unchanged (Postgres, Redis, backend, bot, ngrok, backup).
All images and Python wheels used by the project have `aarch64` builds, so the free ARM machine works as is.

Oracle changes its free-tier terms from time to time: check the limits in the console before you rely on them.

## 1. Account

1. Sign up at <https://www.oracle.com/cloud/free/>. A bank card is required for identity verification; the free
   resources are not charged. Not every card passes (cards of some countries are rejected): try another one if it fails.
2. Pick the **home region** carefully, it cannot be changed later. Choose one close to the members. Free ARM capacity
   differs per region; if the machine below cannot be created, see "Troubleshooting".
3. Create only *Always Free* resources (they are labelled so in the console). Upgrading to "Pay As You Go" keeps the free
   limits, but anything above them is billed, so do not exceed them.

## 2. The VM

Compute → Instances → Create instance:

| Field | Value |
|---|---|
| Image | Canonical Ubuntu 22.04 (aarch64) |
| Shape | Ampere `VM.Standard.A1.Flex`, 2 OCPU, 12 GB RAM (the free pool is 4 OCPU / 24 GB in total) |
| Networking | the default VCN with a public subnet, *assign a public IPv4 address* |
| SSH keys | upload your public key, or let Oracle generate a pair and save the private key |
| Boot volume | 50 GB (the free pool is 200 GB in total) |

The project needs about 1 GB of RAM in practice, so 2 OCPU / 12 GB leaves half of the free pool unused.

**Firewall:** leave only port 22 open in the VCN security list (the default). Nothing else must be exposed:
compose publishes every port on `127.0.0.1`, and the Mini App is reached through the outgoing ngrok tunnel.

Connect: `ssh -i <private key> ubuntu@<public ip>`.

## 3. Docker

```bash
sudo apt-get update && sudo apt-get -y upgrade
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && newgrp docker
docker compose version     # the compose plugin is installed together with Docker
```

## 4. The project

```bash
git clone <repository url> aitu-bot && cd aitu-bot
cp .env.example .env
nano .env
```

Fill in `.env` (the whole list is in the README, "Configuration"):

| Variable | What to put |
|---|---|
| `BOT_TOKEN` | from @BotFather |
| `NGROK_AUTHTOKEN` | from <https://dashboard.ngrok.com/get-started/your-authtoken> |
| `NGROK_DOMAIN` | the free static domain from the ngrok dashboard (Universal Gateway → Domains), e.g. `my-club.ngrok-free.app`, so the Mini App link survives restarts |
| `INTERNAL_API_TOKEN` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, at least 32 characters |
| `INITIAL_ADMIN_TELEGRAM_IDS` | the Telegram ids of the first admins |
| `POSTGRES_PASSWORD` | a new password, and the same one inside `DATABASE_URL`; set it **before the first start**, Postgres applies it only when it creates the volume |
| `TIMEZONE` | `Asia/Almaty` by default |

Google Sheets backup (optional): copy the service-account key to `secrets/google-service-account.json`
(`scp -i <key> google-service-account.json ubuntu@<ip>:aitu-bot/secrets/`) and set `GOOGLE_SHEETS_ID`.

Start:

```bash
docker compose up -d --build     # the first build on ARM takes a few minutes
docker compose ps                # db, redis, backend must become "healthy"
docker compose logs -f backend bot
```

Check: `curl -s localhost:8001/health`, then send `/start` to the bot and open the Mini App from its menu button.

## 5. Running it for a long time

- **Restarts:** every service has `restart: unless-stopped` and Docker starts on boot, so the stack comes back after a
  reboot of the VM (`sudo systemctl is-enabled docker` should print `enabled`).
- **Updates:** `git pull && docker compose up -d --build`. Migrations run when the backend starts. Take a dump first:
  `docker compose exec backup sh /ops/pg_backup.sh once`.
- **Backups:** dumps go to `./backups` on the same disk. Copy them off the VM regularly, e.g. from your own computer:
  `scp -i <key> "ubuntu@<ip>:aitu-bot/backups/*.dump" .` (restore order: README, "Database backups"). The Google Sheets
  backup is a second copy of the future bookings.
- **Idle reclaim:** Oracle may reclaim Always Free instances that stay idle for a week (its published rule: CPU, network
  and, for ARM shapes, memory use all below 20%). This bot is a light load and can fall under that, so expect it to
  happen. Upgrading the account to "Pay As You Go" (the free resources stay free) is Oracle's documented way to avoid
  the reclaim; check the current rule in the Always Free documentation. A stopped machine is started again from the console.
- **Disk:** `docker system prune -f` removes old images after updates; logs are already rotated (3 × 10 MB per service).

## Troubleshooting

- **`Out of host capacity` when creating the ARM machine:** this is a capacity shortage in the region, not a problem
  with your account. Retry later (often in the morning), try another availability domain, or ask for a smaller shape
  (1 OCPU / 6 GB is enough).
- **The machine builds but the backend is not `healthy`:** `docker compose logs backend`. The usual causes are an empty or
  short `INTERNAL_API_TOKEN`, or a `DATABASE_URL` password that differs from `POSTGRES_PASSWORD`.
- **The Mini App does not open:** `docker compose logs ngrok`. A wrong `NGROK_AUTHTOKEN` or a domain that belongs to a
  different ngrok account are the usual causes; `WEBAPP_BASE_URL` in `.env` overrides the domain if you serve the app
  from your own host name instead.
- **Without Oracle's card check or capacity:** the same compose file runs on any machine with Docker, e.g. a home PC
  with ngrok or Cloudflare Tunnel in place of the `ngrok` service (set `WEBAPP_BASE_URL` to the tunnel's address).
