# Aegis Edge — Frontend

React + Vite + TypeScript + Tailwind command console for the Aegis Edge
fleet, built to the spec in the repo's `frontend.md`. No component library —
a custom "tactical ops console" design system (near-black, monospace data,
hairline borders, no gradients/shadows).

## Stack

- React 19 + Vite + TypeScript
- Tailwind CSS (design tokens in `tailwind.config.js` match `frontend.md` §2)
- `react-router-dom` for the 4 top-level screens
- `recharts` installed and ready for the resource/latency charts called for
  in `frontend.md`'s assumptions (not yet wired into a chart — telemetry is
  currently rendered as a numeric strip; swap in a `<LineChart>` from
  `recharts` once real telemetry history is available)

## Getting started

```bash
npm install
cp .env.example .env   # point at your local edge-node backend, or leave as-is for mocks
npm run dev
```

## Structure

```
src/
  types/index.ts          # TS types mirroring backend.md §6's API contract
  api/
    client.ts             # REST + WS wrapper — the ONLY place that calls the backend
    mock.ts                # deterministic mock data (USE_MOCKS flag in client.ts)
  context/
    NetworkModeContext.tsx # global offline/degraded/full state (persistent top bar control)
  components/
    primitives.tsx         # Panel, ConnectivityPill, VerdictBadge, MonoValue, etc.
    TopBar.tsx              # nav + network simulator control
    device/                 # widgets composing the Device Console screen
  pages/
    FleetOverview.tsx        # 3.1 — landing screen, device grid
    DeviceConsole.tsx         # 3.2 / 3.2b / 3.3 — Q&A, decision feed, memory
                               #   browser, sync strip, resource strip, capture,
                               #   activity log, as tabs on one screen
    ConflictTheater.tsx       # 3.4 — demo centerpiece
    CommandDashboard.tsx      # 3.5 — cloud/trusted view
```

## Backend wiring

Every network call goes through `src/api/client.ts`. Right now
`USE_MOCKS = true` in `src/api/mock.ts`, so every screen renders against
deterministic fake data with no backend running. Flip that flag once
`backend.md`'s FastAPI service exposes the real endpoints — no page code
needs to change, since pages only ever import from `client.ts`.

Endpoints wired (per `backend.md` §6 / `frontend.md` §4):

| Endpoint | Used by |
| --- | --- |
| `GET /devices` | Fleet Overview |
| `WS /devices/{id}/events` | Decision Feed |
| `POST /devices/{id}/query` | Q&A panel |
| `POST /devices/{id}/capture` | Capture panel |
| `POST /network/mode` | Top bar network simulator |
| `WS /consensus/events` | Conflict Theater |
| `GET /cloud/state` | Command Dashboard |
| `GET /devices/{id}/telemetry` | Resource strip |
| `GET /devices/{id}/sync` | Sync strip |
| `GET /devices/{id}/activity` | Activity Log tab |

`GET /devices/{id}/memory` (Local Memory Browser) isn't named in
`backend.md`'s table yet — it's stubbed with mock data per `frontend.md` §4's
instruction to stub rather than block. Add the real endpoint when ready.

## Non-goals (per frontend.md §5)

No auth, no mobile-responsive polish, no generic settings/admin UI beyond
the network simulator.

## Next steps

This is the scaffold. Page-by-page visual design passes (per the repo's
build order step 9, "Conflict Theater last") come next — bring reference
designs and we'll implement them against this structure.
