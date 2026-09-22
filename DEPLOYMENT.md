# GCP Deployment Guide

Deploy MAGEASY Replenishment Dashboard to Google Cloud Platform using **Compute Engine + IAP (Identity-Aware Proxy)**.

**Architecture:**
```
User (Google account)
    ↓ (HTTPS + Google login)
Google IAP (identity check)
    ↓
Load Balancer
    ↓
Compute Engine VM (e2-small)
    ├─ Docker: sp_etl (port 8000)
    ├─ Docker: sp_n8n (port 5678)
    └─ Persistent Disk mounted at /data
         └─ sp_api.duckdb
```

**Estimated cost:** ~US$20/month (VM + disk + LB + IAP is free)

---

## Phase 1: GCP Project Setup（30 min）

### 1.1 Sign in and create project
1. Go to https://console.cloud.google.com/ (log in with your MAGEASY Google account)
2. Top bar → **Select project** → **New project**
3. Project name: `mageasy-replenishment` (or similar)
4. Note down the **Project ID** (auto-generated, e.g. `mageasy-replenishment-472019`)

### 1.2 Enable billing
1. Left menu → **Billing** → Link a billing account
2. If new to GCP: you get **US$300 free credit for 90 days** (no auto-charge)

### 1.3 Enable required APIs
Left menu → **APIs & Services** → **Library**. Enable these one by one:
- Compute Engine API
- Cloud Resource Manager API
- IAP (Identity-Aware Proxy) API
- Secret Manager API
- Cloud Logging API

### 1.4 Save your Project ID
Write it down — you'll use it in every step below. Example: `mageasy-replenishment-472019`

---

## Phase 2: Compute Engine VM（20 min）

### 2.1 Create the VM
1. Left menu → **Compute Engine** → **VM instances** → **Create instance**
2. Configure:
   - **Name:** `replenishment-server`
   - **Region:** `asia-east1` (Taiwan) or `us-west1` (西岸，近 Amazon)
   - **Zone:** any (e.g. `asia-east1-a`)
   - **Machine type:** `e2-small` (2 vCPU, 2 GB) — enough for daily ETL + Dashboard
   - **Boot disk:**
     - OS: **Ubuntu 22.04 LTS**
     - Size: **20 GB** (system only)
   - **Firewall:** ✓ Allow HTTP traffic, ✓ Allow HTTPS traffic
3. Click **Create**

### 2.2 Add a persistent disk for data (Important!)
DuckDB file lives here. Separate from OS disk so you can snapshot/backup independently.

1. Left menu → **Compute Engine** → **Disks** → **Create disk**
2. Configure:
   - **Name:** `replenishment-data`
   - **Zone:** same as VM
   - **Disk type:** Balanced persistent disk (SSD, cheaper than SSD)
   - **Size:** **20 GB** (enough for years of data)
3. Click **Create**
4. Attach to VM: VM instances → click your VM → **Edit** → **Additional disks** → **Attach existing disk** → select `replenishment-data`

### 2.3 SSH into the VM
- Compute Engine → VM instances → click **SSH** button next to your VM
- A terminal opens in browser

### 2.4 Format and mount the data disk
Inside the VM SSH terminal:
```bash
# Find the disk
sudo lsblk
# Should see /dev/sdb (or nvme1n1) - 20GB unformatted

# Format (ONE-TIME ONLY — WILL WIPE DISK)
sudo mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard /dev/sdb

# Mount to /data
sudo mkdir -p /data
sudo mount -o discard,defaults /dev/sdb /data
sudo chmod a+w /data

# Auto-mount on reboot
echo "UUID=$(sudo blkid -s UUID -o value /dev/sdb) /data ext4 discard,defaults,nofail 0 2" | sudo tee -a /etc/fstab
```

---

## Phase 3: Install Docker and Deploy（30 min）

### 3.1 Install Docker + docker-compose
```bash
# Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# Log out and back in (close SSH, click SSH again)

# Verify
docker --version
docker compose version
```

### 3.2 Clone your code from GitHub
```bash
cd ~
git clone https://github.com/thedigiflip/awd-replenishment.git app
cd app
```

### 3.3 Adjust docker-compose.yml for production
Edit `docker-compose.yml` to mount `/data` (host) → `/data` (container):
```yaml
# Under etl service:
volumes:
  - /data:/data
# Under n8n service:
volumes:
  - /data/n8n:/home/node/.n8n
```
(Should already be like this, verify.)

### 3.4 Setup .env with Secret Manager (see Phase 4 first!)
Don't put credentials in .env yet. Use Google Secret Manager (Phase 4).

For now, create a placeholder .env with defaults, then we'll swap to Secret Manager.

---

## Phase 4: Secret Manager for credentials（20 min）

### 4.1 Store credentials as secrets
In Google Cloud Console → **Security** → **Secret Manager** → **Create secret**

Create these secrets (one per credential):
- `SP_API_REFRESH_TOKEN` — paste your NA refresh token
- `SP_API_REFRESH_TOKEN_EU` — paste your EU refresh token
- `SP_API_CLIENT_ID` — paste
- `SP_API_CLIENT_SECRET` — paste
- `ADS_CLIENT_ID`, `ADS_CLIENT_SECRET`, `ADS_REFRESH_TOKEN`, `ADS_PROFILE_ID`
- `N8N_USER`, `N8N_PASSWORD`

### 4.2 Grant VM access to secrets
1. Compute Engine → your VM → click name → note the **Service account** email
2. Secret Manager → each secret → **Permissions** → **Grant access**
3. Add the VM service account as `Secret Manager Secret Accessor`

### 4.3 Fetch secrets on VM startup
Inside VM SSH:
```bash
cd ~/app
# Install gcloud CLI (usually already installed on GCP VMs)
gcloud --version

# Write a script to pull secrets and generate .env
cat > pull-secrets.sh <<'EOF'
#!/bin/bash
set -e
PROJECT_ID=$(gcloud config get-value project)
cat > .env <<ENV
SP_API_REFRESH_TOKEN=$(gcloud secrets versions access latest --secret=SP_API_REFRESH_TOKEN --project=$PROJECT_ID)
SP_API_REFRESH_TOKEN_EU=$(gcloud secrets versions access latest --secret=SP_API_REFRESH_TOKEN_EU --project=$PROJECT_ID)
SP_API_CLIENT_ID=$(gcloud secrets versions access latest --secret=SP_API_CLIENT_ID --project=$PROJECT_ID)
SP_API_CLIENT_SECRET=$(gcloud secrets versions access latest --secret=SP_API_CLIENT_SECRET --project=$PROJECT_ID)
SP_API_MARKETPLACE_IDS=["ATVPDKIKX0DER","A1F83G8C2ARO7P","A1PA6795UKMFR9"]
ADS_CLIENT_ID=$(gcloud secrets versions access latest --secret=ADS_CLIENT_ID --project=$PROJECT_ID)
ADS_CLIENT_SECRET=$(gcloud secrets versions access latest --secret=ADS_CLIENT_SECRET --project=$PROJECT_ID)
ADS_REFRESH_TOKEN=$(gcloud secrets versions access latest --secret=ADS_REFRESH_TOKEN --project=$PROJECT_ID)
ADS_PROFILE_ID=$(gcloud secrets versions access latest --secret=ADS_PROFILE_ID --project=$PROJECT_ID)
DUCKDB_PATH=/data/sp_api.duckdb
N8N_USER=$(gcloud secrets versions access latest --secret=N8N_USER --project=$PROJECT_ID)
N8N_PASSWORD=$(gcloud secrets versions access latest --secret=N8N_PASSWORD --project=$PROJECT_ID)
ETL_LOG_LEVEL=INFO
ETL_BATCH_SIZE=50
ORDERS_LOOKBACK_DAYS=7
ENV
echo ".env generated"
EOF
chmod +x pull-secrets.sh
./pull-secrets.sh
cat .env | head -3  # verify (should show refresh_token starting with Atzr|...)
```

### 4.4 Start containers
```bash
docker compose up -d
# Watch logs
docker compose logs -f etl
```

Check http://<VM-external-IP>:8000/dashboard — you should see the dashboard.

---

## Phase 5: Load Balancer + IAP for HTTPS + Google login（40 min）

### 5.1 Reserve a static IP
1. VPC network → **IP addresses** → **Reserve external static IP**
2. Name: `replenishment-ip`
3. Type: Global (needed for LB)

### 5.2 Create OAuth consent screen
1. APIs & Services → **OAuth consent screen**
2. User type: **Internal** (只有 MAGEASY 員工能登入)
3. Fill in: App name, support email, developer email
4. Save

### 5.3 Set up HTTPS Load Balancer with IAP
1. Network services → **Load Balancing** → **Create load balancer**
2. Type: **Application Load Balancer (HTTP/S)**
3. From Internet to VMs, Global
4. Configure:
   - **Backend:** Create a new backend service pointing to your VM (port 8000)
     - Enable IAP here
   - **Frontend:** HTTPS
     - Certificate: **Google-managed** (auto-renew)
     - Domain: your subdomain (e.g. `dashboard.yourdomain.com`) — need to have a domain
     - Static IP: the one reserved above
   - **Host and path rules:** default (route all traffic to backend)
5. Create

### 5.4 Add authorized users to IAP
1. Security → **Identity-Aware Proxy** → find your backend service
2. **Add principal** → add MAGEASY team member emails
3. Role: **IAP-secured Web App User**

### 5.5 Point domain to LB IP
In your DNS provider (Cloudflare, GoDaddy, etc.):
- Create A record: `dashboard.yourdomain.com` → LB static IP
- Wait 5-15 min for DNS propagation

**Test:** Open `https://dashboard.yourdomain.com` → Google login → Dashboard

---

## Phase 6: Cronjobs and monitoring（20 min）

### 6.1 n8n on the same VM
n8n runs in docker-compose already. Access via `http://<VM-IP>:5678` (or put behind another LB backend on `/n8n` path).

### 6.2 Verify daily sync works
n8n workflow `AWD Replenishment — Daily ETL + Alert` should still be there. Verify cron `0 10 * * *` (10:00 UTC = 18:00 Taiwan).

### 6.3 Cloud Logging (already automatic)
GCP VMs auto-forward logs. Query in Logging → Logs Explorer:
```
resource.labels.instance_id="<your-VM-instance-id>"
severity>=ERROR
```

### 6.4 Backup automation
```bash
# Daily DuckDB snapshot
sudo crontab -e
# Add:
0 2 * * * cp /data/sp_api.duckdb /data/backups/sp_api_$(date +\%Y\%m\%d).duckdb
0 3 * * * find /data/backups -mtime +14 -delete
```

Or use GCP Persistent Disk snapshots (managed, better).

---

## Phase 7: Post-deploy checklist

- [ ] Rotate all SP-API credentials (since they were pasted in chat during dev)
- [ ] Verify Sales sync at n8n cron time (10:00 UTC)
- [ ] Verify Inventory sync succeeds for all 3 marketplaces
- [ ] Add all team members to IAP allowlist
- [ ] Set up billing alerts (Budgets & alerts) — alert at $30/month
- [ ] Document runbook for common ops (restart, backup restore, etc.)

---

## Rough timeline

| Phase | Duration | Blocking? |
|---|---|---|
| 1. Project setup | 30 min | No (can prep alone) |
| 2. VM + disk | 20 min | No |
| 3. Docker deploy | 30 min | No |
| 4. Secrets | 20 min | Need SP-API credentials refreshed first (recommend) |
| 5. LB + IAP | 40 min | Need domain name |
| 6. Cronjobs + backup | 20 min | No |
| **Total** | ~3 hours | (spread over 1-2 days is fine) |

---

## FAQ

**Q: Do I need a domain?**
Yes for HTTPS + IAP. Cheapest: buy one on Cloudflare (~$10/yr). Or use existing MAGEASY subdomain.

**Q: What if I don't have a domain yet?**
Skip Phase 5 initially. Access via `http://<VM-IP>:8000` with **firewall rule limiting to your office IP**. Add IAP later.

**Q: How do I upgrade the app?**
```bash
ssh into VM
cd ~/app
git pull
docker compose down
docker compose up -d --build
```

**Q: How do I restore from backup?**
```bash
docker compose down
cp /data/backups/sp_api_YYYYMMDD.duckdb /data/sp_api.duckdb
docker compose up -d
```
