# Ex-Vero — Frontend Specification

**Role of this document:** the UI layer only. It consumes the REST/WebSocket contract in
`backend.md` §10 and renders it. **No model or policy logic lives in the browser** — the
frontend is a thin renderer over the backend. Read `00-problem-statement.md` and
`backend.md` first.

**Mental model for the UI:** show the *notepad* (each device's local memory + instant
answers) and the *walkie-talkie* (the fleet reporting, disputing, and converging). If a
screen doesn't make one of those legible, cut it.

---

## 1. Stack and assumptions

- **React + Vite + TypeScript + Tailwind, no component library** (custom design system —
  see §2 for why). Qdrant Edge has no browser binding; the UI talks to each edge node
  over HTTP/WS.
- **One web app with a device switcher** simulating N devices from one browser. Each
  device is a real independent backend process; the switcher only changes which one you
  watch.
- **Charts:** a plain `<canvas>` or Recharts for the two live charts (latency, memory).
  Nothing heavier.

**What we expect:** every field the UI shows is named in the backend contract (§10 of
`backend.md`). If a field doesn't exist yet, stub it with clearly-labeled mock data and
open a task — never invent numbers that look real.

---

## 2. Design direction — tactical ops console, not "AI product"

**Why:** judges have seen fifty gradient-glass chatbot UIs this week. A field-command
console reads as instrumentation you'd trust in an incident room, and it makes the data
the hero.

**Explicitly avoid:** rounded glass cards, purple-to-blue gradients, soft drop shadows,
particle backgrounds, chat-bubble UI, a chatbot icon.

- **Palette:** near-black base `#0B0D0F`, off-white text `#E8E9EA`, one hot accent for
  critical `#FF4433`, one cool accent for verified/synced `#3DDC84` (amber `#F5A623` for
  pending). No AI gradient anywhere.
- **Type:** monospace for all data, IDs, timestamps, scores (`JetBrains Mono` / `IBM Plex
  Mono`); a plain grotesk (`Inter`) for prose/labels.
- **Shape:** sharp corners (0–2px), 1px hairline borders instead of shadows, real grid
  alignment. HUD panels, not cards.
- **Motion:** functional only — a log line slides in, a status pill flips, a sync bar
  fills. No decorative animation.
- **Density:** favor more real data over whitespace. It's an ops tool.

---

## 3. Screens — what each is for and what it must prove

### 3.1 Fleet Overview (landing)
- **Purpose:** see the whole fleet and control connectivity.
- Grid of device tiles: name, connectivity state (OFFLINE / DEGRADED / ONLINE,
  color-coded), local memory `used/cap` (e.g. "340/500"), last-sync time, a mini
  sparkline of recent decision activity.
- Persistent top-bar **network simulator** control: `Offline / Degraded / Full` — the
  most-used control in the demo; make it big.
- **We expect:** clicking a tile opens the Device Console; the memory `used/cap` makes
  eviction visible.

### 3.2 Device Console (per device — the main working screen)
- **Left — Instant Q&A (RAG):** a query box; the answer renders with the retrieved
  source facts it used, a prominent `latency_ms` stopwatch readout, and a badge showing
  **which path served it (OFFLINE / ONLINE)**. This is the goal-8 surface.
- **Center — Live Decision Feed:** streaming, newest on top, one row per captured fact:
  content preview (text or image thumbnail), verdict badge (`KEPT LOCAL / QUEUED /
  SYNCED / REJECTED`), and a one-line plain-English reason ("98% similar to a report 4m
  ago — discarded as redundant"). The most important widget: it makes the AI's judgment
  legible.
- **Right — Local Memory Browser:** searchable/filterable list of everything on the
  device, each row badged `local_only` / `synced` / `pending`, expandable to show *why*
  it's in that state (decision reason, sync verdict, activity entries).
- **Sync Status strip** (from `GET /devices/{id}/sync`): pending counts as a stacked bar
  `URGENT n / ROUTINE n / HELD n`, last attempt/success, consecutive failures, and
  bytes-on-wire for the last push with the mode it ran under. Makes urgent-jumps-queue
  visible.
- **Bottom — Resource telemetry strip:** live CPU %, RAM MB, model load time, from
  `GET /telemetry` — real numbers, labeled as the emulated constrained target.

### 3.2b Activity Log (tab on the Device Console)
- **Purpose:** the "why is this fact in this state?" answer, without narration.
- From `GET /devices/{id}/activity`, streamed live: one row per event, newest first,
  filterable by kind (`capture / decision / push_attempt / push_result / pull_result /
  consensus / retraction / error / mode_change`), each with timestamp (mono), kind badge,
  and a plain detail ("pushed 4 points, 2 failed, 18.2 KB, mode=degraded").
- **We expect:** a judge lands on any fact's full history in two clicks → goal 4 met.

### 3.3 Capture Panel (multimodal input)
- **Purpose:** show the link between "I did something" and "the system judged it."
- Text field + image upload (hazard/damage/equipment photo). On submit, the verdict
  appears in the Decision Feed in under a second. Image entries show a thumbnail
  everywhere, never a filename.

### 3.4 Conflict Theater (the centerpiece — build carefully)
- **Purpose:** the 20 seconds that make judges sit up — the walkie-talkie moment.
- Split screen: two (or three) device consoles side by side, all offline, all holding
  **different values for the same fact** (e.g. same zone's hazard status).
- A single **Reconnect** button flips the global network mode so all come online at once.
- On reconnect: an animated merge showing each device's incoming value, its
  trust/corroboration weight, and the resulting consensus value with a confidence score,
  plus one line of explanation ("Zone-C hazard CONFIRMED — 3 corroborating devices,
  confidence 0.87" or "DISPUTED — 2 vs 1, showing both"). A conflict-injector control
  and a "rogue device" toggle make it reproducible on demand.

### 3.5 Command Dashboard (the cloud view)
- **Purpose:** what a dispatcher sees — the merged, trusted picture from `GET
  /cloud/state`.
- Only facts past the trust gate appear; explicitly show a retracted fact disappearing
  cleanly (the no-ghost guarantee) and a `DISPUTED` fact showing both values. Trust
  scores per device, visibly moving.

---

## 4. Data contract — the routes the backend sends

**Full request/response shapes live in [`API.md`](API.md).** Build the frontend against
those shapes; the backend team implements them to match. Base URL `http://localhost:8000`,
`{id}` selects a device. The query/answer routes work identically in every network mode.

### 4.1 Routes by screen

| Screen | Routes it consumes |
|---|---|
| 3.1 Fleet Overview | `GET /devices` · `GET /network/mode` · `POST /network/mode` |
| 3.2 Device Console — header | `GET /devices/{id}` |
| 3.2 — Instant Q&A (RAG) | `POST /devices/{id}/query` → `{ answer, answer_path, model, latency_ms, sources[], results[] }` |
| 3.2 — Live Decision Feed | `WS /devices/{id}/events` (frames `{type:"decision"|"activity"}`) · `GET /devices/{id}/feed` (history) |
| 3.2 — Local Memory Browser | `GET /devices/{id}/memory` (filters: `q`, `sync_state`, `modality`, `zone`) · `GET /devices/{id}/memory/{point_id}` (why-it's-in-this-state) |
| 3.2 — Sync Status strip | `GET /devices/{id}/sync` → pending-by-priority, last push bytes/mode, failures |
| 3.2 — Resource strip | `GET /devices/{id}/telemetry` → real CPU/RAM/latency, `target_label` |
| 3.2b Activity Log | `GET /devices/{id}/activity` (filter `kind`) · live via `WS /devices/{id}/events` |
| 3.3 Capture Panel | `POST /devices/{id}/capture` (JSON for text, `multipart/form-data` for image) |
| 3.4 Conflict Theater | `WS /consensus/events` (frames `ConsensusEvent`) · `POST /demo/inject-conflict` · `POST /devices/{id}/rogue` · `POST /network/mode` (the single Reconnect flip) |
| 3.5 Command Dashboard | `GET /cloud/state` (trusted facts + `device_trust`) · `POST /devices/{id}/retract/{point_id}` |
| Evidence overlays | `GET /benchmark/resolver-vs-lww` · `GET /benchmark/recall` |

### 4.2 Rules
- Render exactly what the contract defines — never invent numbers that look real. If a
  field isn't implemented yet, show clearly-labeled mock data and open a task.
- WebSocket frames are additive: append `decision`/`activity`/`consensus` frames as they
  arrive; the REST `GET` variants are for initial load and history.
- `answer_path` (`offline` / `online` / `extractive`) drives the path badge on the Q&A
  panel — this is the goal-8 proof, so make it prominent.

---

## 5. Non-goals
- No user accounts / auth flow.
- No mobile-responsive polish — this runs on a laptop screen-share.
- No settings/config UI beyond the network simulator — hardcode demo scenarios instead
  of a generic admin panel.
