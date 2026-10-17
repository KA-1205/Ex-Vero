# Ex-Vero — Deployment

Everything you need to run Ex-Vero yourself: clone it, run the full stack locally,
deploy it to a host, and keep your copy in sync with GitHub.

Ex-Vero has two independently deployable pieces:

- **The kernel** (edge nodes + cloud gateway + Qdrant hub + Ollama) — a set of Docker
  containers. This is the backend and it must run somewhere with a **persistent disk**
  (each edge node keeps an on-disk Qdrant Edge shard) and **WebSocket** support.
- **The dashboard** (`frontend/`) — a static React build. It is a thin renderer over the
  backend's REST + WebSocket contract, so it can be hosted anywhere, including Vercel.

> [!IMPORTANT]
> The cloud gateway is the fleet's **single sequencer** — it assigns a monotonic `seq` to
> every event from one in-process counter. Run **exactly one** gateway instance. Never
> deploy it as an auto-scaling/serverless function; two instances would hand out two
> independent sequence streams and corrupt the order the consensus fold depends on.

---

## 1. Clone

```bash
git clone https://github.com/KA-1205/Ex-Vero.git
cd Ex-Vero
```

**Prerequisites**

| Tool | Version | Needed for |
| --- | --- | --- |
| Docker + Compose | latest | the kernel (all services) |
| Python | 3.11 | running tests and the `tools/` scripts |
| Node.js | 18+ | the dashboard |

---

## 2. Run the full stack locally

```bash
# 1. start the kernel: Qdrant hub, gateway, Ollama, and three constrained edge nodes
docker compose -f docker/docker-compose.yml up --build

# 2. in a second terminal, start the dashboard
cd frontend && npm install && npm run dev
# open http://localhost:5173

# 3. first run only: pull the on-device answer model into Ollama
docker compose -f docker/docker-compose.yml exec ollama ollama pull qwen2.5:1.5b
```

| Service | URL | What it is |
| --- | --- | --- |
| Dashboard | <http://localhost:5173> | React UI |
| Edge node (`edge-tiny`) | <http://localhost:8000/docs> | FastAPI, interactive route docs |
| Edge node (`edge-standard`) | <http://localhost:8001/docs> | text + CLIP |
| Cloud gateway | <http://localhost:8088> | Sync + consensus fold |
| Qdrant | <http://localhost:6333/dashboard> | Hub; inspect the `facts` collection |
| Ollama | <http://localhost:11434> | Local answer model |

The three edge nodes are deliberately constrained, and each reports its **real** cgroup
limits:

| Container | Limit | Config |
| --- | --- | --- |
| `edge-tiny` | 1 CPU / 512 MB | `config/tiny-edge.yaml` (text only) |
| `edge-standard` | 2 CPU / 2 GB | full model set (text + CLIP) |
| `edge-large` | 4 CPU / 4 GB | full model set |

### Captured facts reach Qdrant only after a push

This is the offline-first design, not a bug. A capture is stored **on the device**;
nothing leaves until you push it.

```bash
curl -sX POST localhost:8000/devices/dev-01/push
```

Refresh the Qdrant dashboard and the point appears in `facts` (and in `fact_events`, the
append-only log). Device ids are `dev-01`…`dev-04` and `cam-01`…`cam-03`.

### Stop

```bash
docker compose -f docker/docker-compose.yml down       # keep data
docker compose -f docker/docker-compose.yml down -v    # wipe volumes
```

### Seed data

Everything you need to seed the fleet **ships in the repo** — no download required to start:

| File | Size | What it is |
| --- | --- | --- |
| `config/seed/disaster_messages_train.parquet` | 2.8 MB | 21,046 disaster messages, loaded by `tools/load_disaster_dataset.py` |
| `config/seed/gas_sensors_seed.jsonl` | 124 KB | derived gas-sensor facts |
| `config/seed/conflicts_*.jsonl`, `image_conflicts.jsonl` | small | consensus / conflict fixtures |
| `config/seed/images/` + `images_manifest.jsonl` | small | image-memory fixtures |

### Run in demo mode (frontend only)

If you want to explore the dashboard interface without launching the full kernel:

```bash
cd frontend && npm install && npm run dev
# open http://localhost:5173
```

The frontend includes a built-in mock simulation layer. Open <http://localhost:5173> — all pages render with realistic sample data. You can toggle between live backend and mock data by setting `USE_MOCKS = true` in `frontend/src/api/mock.ts`.

To verify and test the frontend:

```bash
cd frontend
npm test        # run Vitest unit test suite (devices, conflict reconnect, metrics)
npm run lint    # fast oxlint validation
npm run build   # typecheck (tsc -b) and Vite production bundle
```

The two **raw** source dumps are large and are deliberately **not committed**. Download them
only if you want to regenerate the derived seed files:

- **Gas sensor array under dynamic gas mixtures** — UCI Machine Learning Repository:
  <https://archive.ics.uci.edu/dataset/322/gas+sensor+array+under+dynamic+gas+mixtures>.
  Unpack `ethylene_methane.txt` (638 MB) to `config/seed/gas_sensor_raw.txt`, then:

  ```bash
  python tools/load_gas_dataset.py
  # or point it anywhere: GAS_SENSOR_FILE=/path/to/ethylene_methane.txt python tools/load_gas_dataset.py
  ```

- **Disaster Response Messages** — Hugging Face:
  <https://huggingface.co/datasets/community-datasets/disaster_response_messages>.
  The repo already ships the `.parquet`; to rebuild the JSONL from source:

  ```bash
  pip install datasets
  python dataset_download.py     # writes config/seed/disaster_response.jsonl
  ```

---

## 3. Deploy to a host

The kernel runs anywhere Docker runs with a persistent disk and WebSocket support. A single
small VM is enough for the whole fleet. On the host:

```bash
git clone https://github.com/KA-1205/Ex-Vero.git
cd Ex-Vero
cp env.example .env      # then edit it — see "Configuration" below
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
```

Open only what the dashboard needs, and keep the datastores private:

| Port | Purpose | Exposure |
| --- | --- | --- |
| `8000` | edge node (`edge-tiny`) | the dashboard + you |
| `8001` | edge node (`edge-standard`, text + CLIP) | the dashboard + you |
| `8088` | cloud gateway | keep private; the edge nodes reach it in-network |
| `6333` | Qdrant | keep private |
| `11434` | Ollama | keep private |
| `22` | SSH | your IP only |

> The gateway must run as a **single long-lived container** (see the callout at the top).
> When the dashboard is served over HTTPS, put a TLS terminator (Caddy, nginx, or a cloud
> load balancer) in front of `:8000`/`:8001` — browsers refuse to call an HTTP origin, or to
> open a `ws://` socket, from an HTTPS page.

### Configuration

Every override is an environment variable — none are baked into the images. The repo ships
[`env.example`](../env.example); copy it and edit:

```bash
cp env.example .env
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

Every line has a working default, so start empty and add only what you need:

```dotenv
# --- Edge nodes -------------------------------------------------------------
EDGE_HUB_URL=http://gateway:8088
EDGE_CONFIG_PATH=/app/config/tiny-edge.yaml
# ALLOWED_ORIGINS=https://my-dashboard.example
# OLLAMA_ENDPOINT=http://ollama:11434

# --- Cloud gateway / Qdrant hub ---------------------------------------------
QDRANT_URL=http://qdrant:6333
# QDRANT_API_KEY=
# GATEWAY_DEVICE_SECRETS=
# GATEWAY_ADMIN_TOKEN=
```

The most useful knobs:

| Variable | Service | Meaning |
| --- | --- | --- |
| `EDGE_CONFIG_PATH` | edge node | which vertical config to load, e.g. `/app/config/tiny-edge.yaml` |
| `EDGE_HUB_URL` | edge node | where to push; `http://gateway:8088` inside Compose |
| `EDGE_SHARD_DIR` | edge node | on-disk shard location (persist this volume) |
| `ALLOWED_ORIGINS` | edge node | comma-separated extra CORS origins (the dashboard's public URL) |
| `QDRANT_URL` / `QDRANT_API_KEY` | gateway | the hub it writes to (falls back to compose service `qdrant`) |
| `OLLAMA_ENDPOINT` | edge node | set only when Ollama is not on the same host |

> The **vertical is configuration, not code**: swap `config/disaster-response.yaml` for
> another file and the same kernel runs unchanged.

---

## 4. Deploy the dashboard

The dashboard is a static build (`npm run build` → `frontend/dist`). Host it anywhere.
The repository ships a Vercel configuration (`vercel.json`) that serves the frontend at
`/` and the gateway service at `/gateway/*`.

Point the build at your kernel with build-time variables:

```
VITE_API_BASE_URL=https://<your-kernel-host>      # or /node when using the proxy below
VITE_WS_BASE_URL=wss://<your-kernel-host>
```

> **Mixed content.** An HTTPS dashboard cannot call a plain-HTTP kernel, and a browser
> will not open a `ws://` socket from an HTTPS page. Either front the kernel with TLS
> (so you can use `https://` / `wss://`), or use Vercel's server-side proxy: the shipped
> `vercel.json` rewrites `/node/*` and `/thumbnails/*` to the kernel, and you set
> `VITE_API_BASE_URL=/node`. The proxy covers REST and assets, but **not** WebSocket
> streaming — for live updates, terminate TLS in front of the kernel.

See [Deploying to Vercel](../README.md#deploying-to-vercel) for the full wiring.

---

## 5. Keep your working tree in sync with GitHub

The team works from several branches and working trees. **Fetch before you start** so you
are never building on a stale tree:

```bash
# one-time: make fetch prune deleted branches
git config --global fetch.prune true

# before every work session
git switch main
git fetch --all --prune
git pull --ff-only
```

- `fetch --all --prune` updates every remote branch and removes refs for branches deleted
  on GitHub.
- `pull --ff-only` refuses to create a merge commit on a diverged `main` — it fails loudly
  instead of quietly merging, so you notice.
- Long-lived feature branches: `git fetch origin && git rebase origin/main` before opening
  a pull request.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Dashboard loads but shows no data | Browser blocked cross-origin calls, or the kernel is on another origin | Hard-refresh (`Ctrl+Shift+R`) |
| Qdrant dashboard empty | Nothing pushed yet | `POST /devices/{id}/push` |
| Answers slow or timing out | Ollama model not pulled | `docker compose ... exec ollama ollama pull qwen2.5:1.5b` |
| Gateway restarts in a loop | Qdrant not ready | It retries 5×, then stops loudly rather than silently dropping writes |
| Image capture fails | Edge image missing a multipart dependency | Ensure `python-multipart` is installed in the edge image |
