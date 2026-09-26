# Aegis Edge — Frontend Spec

**Role of this doc:** hand this straight to a coding agent to build the demo UI. It only covers the UI layer — it consumes the backend's REST/WebSocket contract defined in `backend.md` and renders it. No model/AI logic lives here.

**Assumptions made (flag if you want these changed):**
- React + Vite + TypeScript + Tailwind, no component library (custom design system, see Section 2 for why).
- One web app with a "device switcher," simulating N physical edge devices from one browser for demo convenience — each device is a real independent backend process, the frontend just lets you flip between watching them.
- Recharts or a plain `<canvas>` for the couple of live charts we need (latency/memory) — keep it light, we don't need a charting library for anything else.

---

## 1. What this UI has to prove, live, in front of judges

Every screen exists to make one of these five moments visible and undeniable. If a screen doesn't serve one of these, cut it.

1. Multiple devices answering instantly, fully offline.
2. A live decision log narrating *why* each new fact was kept local, queued, synced now, or dropped — in plain language, not a JSON dump.
3. Two devices disagreeing about the same fact while offline, then converging into one trusted answer with a visible confidence/consensus score the moment they reconnect.
4. A safety-critical fact jumping the sync queue ahead of routine ones on a deliberately degraded connection.
5. Real resource numbers (latency, memory, CPU) running on the small target device, on screen, not claimed in slides.

---

## 2. Design direction: tactical ops console, not "AI product"

Explicitly avoid: rounded glass cards, purple-to-blue gradients, soft drop shadows, sparkly/particle backgrounds, generic chat-bubble UI, a chatbot icon. That aesthetic reads as "wrapped a model in a template" and judges have seen it fifty times this week.

Instead, build toward a **field command console**: the kind of interface you'd trust in an actual incident response room — dense, legible, fast, a little brutalist.

- **Palette:** near-black base (`#0B0D0F`), off-white text (`#E8E9EA`), one hot accent for urgent/critical states (`#FF4433` — alert red), one cool accent for "verified/synced" states (`#3DDC84` or amber `#F5A623` for "pending," pick one warm + one cool, not five colors). No blue-purple AI gradient anywhere.
- **Typography:** a monospace or semi-mono face for all data, IDs, timestamps, scores (e.g. `JetBrains Mono` or `IBM Plex Mono`), a plain grotesk (e.g. `Inter`) for prose/labels. Numbers and logs in mono make it read as instrumentation, not marketing.
- **Shape language:** sharp corners (0–2px radius max), thin 1px hairline borders instead of shadows for separation, real grid alignment. Think radar/HUD panels, not cards.
- **Motion:** minimal and functional only — a log line slides in, a status pill flips color, a sync progress bar fills. No decorative animation.
- **Density:** favor showing more real data over whitespace. This is an ops tool, not a landing page.

---

## 3. Screens

### 3.1 Fleet Overview (landing screen)
- Grid of device tiles (one per simulated edge device — paramedic tablet / kiosk / Pi node). Each tile: device name, connectivity state (OFFLINE / DEGRADED / ONLINE, color-coded), local memory count shown as `used/cap` (e.g. "340/500 points" — makes the working-set eviction from `backend.md` §3.5 visible, not silent), last-sync time, a mini sparkline of recent decision-engine activity.
- Global **network simulator control** (persistent, top bar): buttons for `Offline / Cellular (degraded) / Wi-Fi (full)` — this is the single most-used control during the demo, make it big and obvious.
- Clicking a tile opens Device Console (3.2).

### 3.2 Device Console (per-device, the main working screen)
- **Left: Instant Q&A panel.** A query box; answers render with the retrieved memory it came from and a latency counter (ms) prominently shown next to every answer — this number is a demo prop, style it like a stopwatch readout.
- **Center: Live Decision Feed.** Streaming list, newest on top, one row per new fact the device just captured. Each row: content preview (text or image thumbnail), verdict badge (`KEPT LOCAL` / `QUEUED` / `SYNCED NOW` / `REJECTED`), and a one-line plain-English reason (e.g. "98% similar to entry 4m ago — discarded as redundant," "urgency 0.91 — pushed immediately despite degraded link"). This feed is the single most important widget in the whole product — it's what makes the AI's judgment legible instead of a black box.
- **Right: Local Memory Browser.** Searchable/filterable list of everything currently held on this device, with a badge for `local_only` (PII-redacted / never leaves device) vs `synced` vs `pending`. Every row expands to show *why* it is in that state — the decision reason, the sync verdict, and the activity entries that touched it.
- **Sync Status strip** (directly under the memory browser, fed by `GET /devices/{id}/sync`): pending counts split by priority as a small stacked bar — `URGENT n / ROUTINE n / HELD n` — plus last attempt / last success, consecutive failures, and bytes-on-the-wire for the last push with the network mode it ran under. The priority split is what makes demo moment 4 legible: you should *see* one urgent item sitting ahead of a queue of routine ones.
- **Bottom bar:** resource telemetry strip — live CPU %, RAM MB, on-device model load time — pinned and always visible on this screen, especially when the active device is the constrained hardware target.

### 3.2b Activity Log (system activity, as its own tab)
The problem statement names "system activity" as a thing to inspect, and it does not fall out of any other screen. A tab on the Device Console, fed by `GET /devices/{id}/activity` and streamed live:

- One row per event, newest first, filterable by kind: `capture` / `decision` / `push_attempt` / `push_result` / `pull_result` / `consensus` / `retraction` / `error` / `mode_change`.
- Each row: timestamp (mono), kind badge, and a one-line plain-English detail — "pushed 4 points, 2 failed, 18.2 KB, mode=degraded."
- **This is the tab that makes the "why is it in this state" question answerable without narrating.** If a judge picks a random fact and can land on its full history here in two clicks, the R4 acceptance line is met by construction.

### 3.3 Capture Panel (multimodal input)
- Text field + a camera/image upload control (photo of hazard, damage, equipment — whatever fits the demo's data). On submit, show the embedding + decision verdict appear in the Live Decision Feed within under a second — this is the visible link between "I just did something" and "the system judged it," which is the whole point of the product.
- Image entries show a thumbnail in every list/feed view, never just a filename.

### 3.4 Conflict Theater (the demo centerpiece — build this carefully)
- A split-screen: two device consoles side by side, both offline, both editing the *same* fact (e.g. same location's status) with clearly different values shown on each side.
- A single "Reconnect" button that triggers both to come online simultaneously.
- On reconnect: an animated merge — show both incoming values, each device's trust/corroboration weight, and the resulting consensus value with a confidence score, plus one line explaining the resolution ("Zone-C hazard confirmed — 3 corroborating devices, LWW favored device B, confidence 0.87"). This is the 20 seconds that should make judges sit up — don't rush the build on this screen, everything else is secondary to it.

### 3.5 Command Dashboard (the "cloud" view)
- One aggregated map/list view showing the merged, trusted, cloud-side picture across all devices — this is what a dispatcher/manager would actually look at. Only shows facts that passed the trust/validation gate; explicitly demonstrate that unverified or rejected facts never appear here (tie back to the "ghost context" tombstone guarantee — show a retracted fact disappearing cleanly, not lingering).

---

## 4. Data contract expectations from backend

The frontend is a thin renderer over:
- `GET /devices` — fleet state for 3.1
- `WS /devices/{id}/events` — the live decision-feed stream for 3.2/3.3 (verdict, reason string, payload preview, timestamp)
- `POST /devices/{id}/query` — instant Q&A, returns answer + latency + source memory ids
- `POST /devices/{id}/capture` — submit text or image fact
- `POST /network/mode` — global network simulator control
- `WS /consensus/events` — conflict-resolution events for 3.4
- `GET /cloud/state` — the merged trusted picture for 3.5
- `GET /devices/{id}/telemetry` — CPU/RAM/latency stream for the resource strip
- `GET /devices/{id}/sync` — sync status for the 3.2 sync strip (pending-by-priority, last attempt/success, bytes on wire)
- `GET /devices/{id}/activity` — system activity for the 3.2b Activity Log tab

If any field named above doesn't exist yet in the backend, stub it with mock data rather than blocking — the demo narrative matters more than wiring order.

---

## 5. Non-goals
- No user accounts/auth flow — not what's being judged.
- No mobile-responsive polish — this runs on a laptop screen-share.
- No settings/config UI beyond the network simulator — hardcode demo scenarios instead of building a generic admin panel.
