# 00 — ENVIRONMENT, HOST SIZING, AND PREREQUISITES

> **Read this first.** Nothing else in this build book will work until every check in §0.6 passes.

---

## 0.1 Where does this run: laptop or cloud?

**Answer: your laptop, if and only if it meets the bar below. Otherwise a single cloud VM. Do not use a managed/multi-node cloud setup — the assignment is explicitly "local Milvus", and Docker-socket introspection (a core feature of this control plane) requires the API and the containers on the same Docker host.**

### Decision table

| Your machine | Verdict | Action |
|---|---|---|
| ≥16 GB RAM, ≥4 physical cores, ≥25 GB free disk, x86_64 Linux or macOS | **Run locally.** | Go to §0.3 |
| ≥16 GB RAM, Apple Silicon (M1–M4) | **Run locally, with an arch caveat.** | Go to §0.3, then read §0.7 |
| ≥16 GB RAM, Windows | **Run locally under WSL2.** | Go to §0.4 |
| 8 GB RAM | **Marginal — will work but Milvus may OOM under the 50k-row demo.** | Run locally with `--rows 5000` only, and set Docker memory to 6 GB. Prefer cloud. |
| <8 GB RAM, or no admin rights, or corporate-locked Docker | **Use a cloud VM.** | Go to §0.5 |

### Memory budget (this is why 16 GB is the bar)

| Container | Idle RSS | Under demo load | Hard limit we set |
|---|---|---|---|
| `milvus-standalone` | ~1.2 GB | 3.5–5 GB | 6 GB |
| `milvus-etcd` | ~80 MB | ~250 MB | 1 GB |
| `milvus-minio` | ~200 MB | ~600 MB | 1 GB |
| `cp-postgres` | ~40 MB | ~150 MB | 512 MB |
| `cp-api` | ~180 MB | ~350 MB | 512 MB |
| `cp-dashboard` (nginx) | ~10 MB | ~20 MB | 128 MB |
| **Total** | **~1.7 GB** | **~6.5 GB** | **9.1 GB ceiling** |

Docker Desktop must be allocated **at least 8 GB**, ideally 10 GB. On native Linux there is no VM, so the host's free RAM is what matters.

Disk: Milvus image ~1.4 GB, MinIO ~180 MB, etcd ~70 MB, Postgres ~250 MB, plus ~3 GB of volume data after the demo. Budget **25 GB free**.

---

## 0.2 Fixed decisions about the host environment

| Item | Value | Non-negotiable because |
|---|---|---|
| Container runtime | Docker Engine ≥ 24.0 with Compose v2 | `depends_on: condition: service_completed_successfully` requires Compose v2.x; `docker compose` (space, not hyphen) is assumed everywhere |
| Docker socket path | `/var/run/docker.sock` | The control plane mounts this to read container state |
| Compose project name | `milvus-cp` | All container names, network name, and volume prefixes derive from it |
| Working directory | the repo root, always | Every relative path in this book (`./volumes`, `infra/…`) assumes it |
| Shell | `bash` (not sh, not zsh, for scripts) | Scripts use `set -euo pipefail`, arrays, and `[[ ]]` |
| Python | 3.12.x | `pymilvus` 2.6 wheels, and 3.13 has had C-extension gaps |
| Node | 20 LTS | Vite 5 requirement |

---

## 0.3 Host setup — macOS or Linux (local path)

Run these **in order**. Each block ends with a verification whose expected output is shown.

### Step 0.3.1 — Install Docker

**macOS:**
```bash
brew install --cask docker
open -a Docker
```
Then in Docker Desktop → Settings → Resources, set: **CPUs 4, Memory 10 GB, Swap 2 GB, Disk 40 GB**. Click Apply & Restart. This step is mandatory; the default 2 GB allocation will make Milvus crash-loop with no obvious error.

**Ubuntu / Debian:**
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
newgrp docker
```

**Verify:**
```bash
docker version --format '{{.Server.Version}}'      # expect >= 24.0.0
docker compose version --short                      # expect >= 2.20.0
docker info --format '{{.MemTotal}}'                # expect >= 8000000000 (8 GB in bytes)
docker run --rm hello-world                         # expect "Hello from Docker!"
```

### Step 0.3.2 — Install Python 3.12 and tooling

```bash
# macOS
brew install python@3.12 jq netcat

# Ubuntu
sudo apt-get install -y python3.12 python3.12-venv python3-pip jq netcat-openbsd postgresql-client
```

**Verify:**
```bash
python3.12 --version    # expect Python 3.12.x
jq --version            # expect jq-1.6 or later
psql --version          # expect psql (PostgreSQL) 14+ — client only, we don't run a host server
```

### Step 0.3.3 — Install Node 20

```bash
# macOS
brew install node@20
# Ubuntu
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

**Verify:** `node --version` → `v20.x.x`; `npm --version` → `10.x.x`

### Step 0.3.4 — Free the required ports

```bash
for p in 19530 9091 9000 9001 5432 8000 8080; do
  if lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
    echo "PORT $p IS IN USE:"; lsof -nP -iTCP:$p -sTCP:LISTEN
  else
    echo "port $p free"
  fi
done
```
Expected: seven lines of `port NNNN free`. **The most common real conflict is 5432** (a host Postgres) — if that is in use, either stop it (`brew services stop postgresql` / `sudo systemctl stop postgresql`) or change `POSTGRES_HOST_PORT` in `.env` to `55432` and adjust §0.6's verification accordingly.

---

## 0.4 Host setup — Windows

Do **not** run this stack in Windows containers or from PowerShell. Use WSL2:

```powershell
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2
```
Then in Docker Desktop → Settings → General, enable **"Use the WSL 2 based engine"**, and under Resources → WSL Integration enable your Ubuntu distro. Open the Ubuntu shell and follow §0.3 from Step 0.3.2 (Docker itself comes from Desktop). Clone the repo **inside the WSL filesystem** (`~/milvus-control-plane`), not under `/mnt/c/` — bind-mount performance on `/mnt/c` will make Milvus's etcd fsync latency fail its own health checks.

Create `%USERPROFILE%\.wslconfig`:
```ini
[wsl2]
memory=10GB
processors=4
swap=4GB
```
Then `wsl --shutdown` and reopen.

---

## 0.5 Host setup — cloud VM (fallback path)

Use **one** VM. Not EKS, not a managed vector DB. The control plane reads the local Docker socket; splitting hosts breaks that by design.

### Recommended instance

| Provider | Instance | vCPU / RAM | Disk | Approx cost |
|---|---|---|---|---|
| AWS | `t3.xlarge` | 4 / 16 GB | 40 GB gp3 | ~$0.166/hr |
| GCP | `e2-standard-4` | 4 / 16 GB | 40 GB pd-balanced | ~$0.134/hr |
| Azure | `Standard_D4s_v5` | 4 / 16 GB | 40 GB Premium SSD | ~$0.192/hr |
| Hetzner | `CPX41` | 8 / 16 GB | 240 GB | ~€0.036/hr |

Image: **Ubuntu 24.04 LTS**. Architecture: x86_64 (avoids the ARM caveat in §0.7).

### AWS provisioning, exactly

```bash
# 1. Security group — SSH only. Everything else reached through an SSH tunnel.
aws ec2 create-security-group \
  --group-name milvus-cp-sg \
  --description "Milvus control plane assignment"

MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress \
  --group-name milvus-cp-sg --protocol tcp --port 22 --cidr "${MYIP}/32"

# 2. Launch
aws ec2 run-instances \
  --image-id resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --instance-type t3.xlarge \
  --key-name YOUR_KEYPAIR \
  --security-groups milvus-cp-sg \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":40,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=milvus-cp}]'
```

**Do not open ports 19530, 9000, 9001, 5432, 8000, or 8080 to the internet.** Milvus, MinIO and Postgres all run with default credentials in this build. Reach them with a tunnel from your laptop:

```bash
ssh -N \
  -L 8080:localhost:8080 \
  -L 8000:localhost:8000 \
  -L 19530:localhost:19530 \
  -L 9091:localhost:9091 \
  -L 9001:localhost:9001 \
  -L 5432:localhost:5432 \
  ubuntu@<PUBLIC_IP>
```
Then `http://localhost:8080` in your browser works exactly as if it were local, and every command in this book runs unchanged.

### On the VM
Run §0.3.1 (Ubuntu block), §0.3.2, §0.3.3, then §0.6. Add swap as insurance:
```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Cost control:** `aws ec2 stop-instances --instance-ids <id>` when not working. Volumes persist; `./infra/deploy.sh up` restarts everything in ~3 minutes.

---

## 0.6 Preflight gate — all of these must pass before Doc 01

Run each; the expected result is stated. Do not proceed past a failure.

| # | Command | Expected |
|---|---|---|
| 1 | `docker version --format '{{.Server.Version}}'` | `24.x` or higher |
| 2 | `docker compose version --short` | `2.20` or higher |
| 3 | `docker info --format '{{.MemTotal}}'` | ≥ `8000000000` |
| 4 | `docker info --format '{{.NCPU}}'` | ≥ `4` |
| 5 | `df -h . \| tail -1 \| awk '{print $4}'` | ≥ `25G` |
| 6 | `test -S /var/run/docker.sock && echo ok` | `ok` |
| 7 | `docker run --rm -v /var/run/docker.sock:/var/run/docker.sock docker:24-cli docker ps` | a table, not a permission error |
| 8 | `python3.12 -c 'import sys; print(sys.version_info >= (3,12))'` | `True` |
| 9 | `node --version` | `v20.` |
| 10 | `jq --version` | any |
| 11 | port loop from §0.3.4 | all free |
| 12 | `docker pull milvusdb/milvus:v2.6.20 && docker image inspect milvusdb/milvus:v2.6.20 --format '{{.Architecture}}'` | `amd64` (or `arm64` on Apple Silicon — see §0.7) |

Check 7 is the one people skip and then lose an hour to. It proves a container can read the Docker socket, which is exactly what `cp-api` will do.

---

## 0.7 Apple Silicon caveat

`milvusdb/milvus` publishes multi-arch manifests, but arm64 builds have historically lagged and occasionally shipped without GPU/SIMD paths. Before committing to local ARM:

```bash
docker manifest inspect milvusdb/milvus:v2.6.20 | jq -r '.manifests[].platform | "\(.os)/\(.architecture)"'
```

- If `linux/arm64` is listed → proceed normally.
- If it is **not** listed → add to the `standalone` service in Compose:
  ```yaml
  platform: linux/amd64
  ```
  This runs Milvus under Rosetta 2 emulation. It works, but expect **2–4× slower** index builds and inserts. Reduce the demo to `--rows 5000` and note the emulation in your README's limitations section. If this bothers you, use the cloud path (§0.5) instead.

Record whichever branch applies in `docs/ARCHITECTURE.md` — a reviewer on different hardware will hit the other one.

---

## 0.8 Repository bootstrap — exact commands

```bash
mkdir -p ~/work && cd ~/work
mkdir milvus-control-plane && cd milvus-control-plane
git init -b main

# Directory skeleton — create every one of these now, empty.
mkdir -p \
  docs \
  infra/{milvus,postgres,nginx,lib,k8s} \
  control_plane/app/{db,schemas,repositories,adapters,services,jobs,api/routers,tests/{unit,integration,fixtures}} \
  control_plane/migrations/versions \
  ops \
  scripts \
  dashboard/src/{api,hooks,components,types} \
  volumes/{etcd,minio,milvus,postgres}

# Python package markers
find control_plane/app -type d -exec touch {}/__init__.py \;

# Volumes must never be committed
cat > .gitignore <<'EOF'
.env
volumes/
__pycache__/
*.pyc
.venv/
node_modules/
dist/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.log
results.json
EOF

git add -A && git commit -m "chore: repository skeleton"
```

**Verify:**
```bash
find . -type d -not -path './.git/*' | sort
```
Expected: 30 directories matching the tree above.

---

## 0.9 The `.env` file — every key, with its exact meaning

Create `.env.example` with **exactly** the following content, then `cp .env.example .env`. Every downstream document references these names; do not rename them.

```bash
# ─── Compose identity ─────────────────────────────────────────────
COMPOSE_PROJECT_NAME=milvus-cp
DOCKER_VOLUME_DIRECTORY=./volumes

# ─── Pinned image versions (never use :latest) ────────────────────
MILVUS_VERSION=v2.6.20
ETCD_VERSION=v3.5.25
MINIO_VERSION=RELEASE.2024-05-28T17-19-04Z
MINIO_MC_VERSION=RELEASE.2024-06-12T14-34-03Z
POSTGRES_VERSION=16-alpine
NGINX_VERSION=1.27-alpine

# ─── Host port bindings (change only on conflict) ─────────────────
MILVUS_HOST_PORT=19530
MILVUS_METRICS_HOST_PORT=9091
MINIO_API_HOST_PORT=9000
MINIO_CONSOLE_HOST_PORT=9001
POSTGRES_HOST_PORT=5432
CP_API_HOST_PORT=8000
DASHBOARD_HOST_PORT=8080

# ─── MinIO ────────────────────────────────────────────────────────
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
MINIO_BUCKET=milvus-bucket
MINIO_REGION=us-east-1

# ─── PostgreSQL ───────────────────────────────────────────────────
POSTGRES_USER=controlplane
POSTGRES_PASSWORD=controlplane
POSTGRES_DB=controlplane
POSTGRES_HOST=cp-postgres
POSTGRES_PORT=5432

# ─── Milvus endpoints as seen FROM INSIDE the compose network ─────
MILVUS_URI=http://milvus-standalone:19530
MILVUS_METRICS_URI=http://milvus-standalone:9091
MILVUS_CONNECT_TIMEOUT_S=3
MILVUS_RPC_TIMEOUT_S=5

# ─── Control-plane behaviour ──────────────────────────────────────
CP_LOG_LEVEL=INFO
CP_LOG_FORMAT=json
CP_HEALTH_INTERVAL_S=15
CP_SNAPSHOT_INTERVAL_S=60
CP_CACHE_TTL_S=5
CP_STALE_MAX_AGE_S=60
CP_BREAKER_FAIL_MAX=3
CP_BREAKER_RESET_S=30
CP_RETENTION_DAYS=7
CP_OVERVIEW_BUDGET_S=6
CP_EXPECTED_COMPONENTS=milvus-standalone,milvus-etcd,milvus-minio,cp-postgres
CP_DOCKER_SOCKET=/var/run/docker.sock
CP_SEED_CLUSTER_NAME=local-milvus-standalone

# ─── Dashboard build-time ─────────────────────────────────────────
VITE_API_BASE=/api/v1
VITE_POLL_INTERVAL_MS=5000
```

### Two rules about this file that will bite you if ignored

1. **`MILVUS_URI` uses the container DNS name `milvus-standalone`, not `localhost`.** `cp-api` runs inside the compose network. If you run the API on your host for debugging instead, override with `MILVUS_URI=http://localhost:19530` in your shell — do not edit `.env`.
2. **`POSTGRES_HOST=cp-postgres` for the same reason.** From your host, `psql` connects to `localhost:5432`; from `cp-api`, to `cp-postgres:5432`. Both are correct in their own context.

---

## 0.10 Where each thing physically lives on disk

| Path | Contents | Survives `make down`? | Survives `make destroy`? |
|---|---|---|---|
| `./volumes/etcd/` | Milvus metadata (collection definitions) | ✅ | ❌ |
| `./volumes/minio/` | Milvus segments, indexes, Woodpecker WAL | ✅ | ❌ |
| `./volumes/milvus/` | Milvus local state, RocksDB residue | ✅ | ❌ |
| `./volumes/postgres/` | All control-plane metadata | ✅ | ❌ |
| Docker images | ~2.2 GB | ✅ | ✅ (removed only by `docker image prune -a`) |

**Critical coupling:** etcd holds the collection *definitions* and MinIO holds the collection *data*. Deleting one and not the other leaves Milvus in a state where collections exist but return errors on read. `make destroy` must always remove both together — never hand-delete a single volume directory.

---

## Next

Proceed to **01_CONTAINERS.md**. Do not skip §0.6.
