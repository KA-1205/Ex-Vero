# Frontend refresh

Local branch: `frontend-update`
Base: `59a76e3` on `main`

## Presentation

Silver (`#E5E7E9`), muted petrol teal (`#236A70`), and warm champagne (`#D5C5AC`) replace the blue field-console palette. Contour graphics remain part of the identity. Gradients use restrained contrast and matte surfaces.

The landing page follows observations through memory and consensus. Numeric and Image dashboards share a workspace switcher, navigation, panel styles, and a motion toggle. The Image fleet query sits near the top. Device cards, console tabs, local memory, decision timelines, evidence capture, conflict comparison, and Reports use the same visual language. Sync and resource strips remain in normal document flow instead of covering console content.

## Motion

- Decorative signal traces travel over contour geometry.
- The memory illustration combines observation tiles into a record.
- The visual illustration uses a scan line; evidence previews scan on hover.
- Newly rendered decision entries and resolution results have short transitions.
- Map markers pulse briefly when their sync timestamp changes.
- The Motion control stores only a browser-local preference.
- Reduced-motion preferences disable animations and transitions.

Illustrations are decorative and hidden from assistive technology. They do not claim to represent measured telemetry or extra detected objects.

## Scope preserved

REST/WebSocket clients, mock data modules, network providers, types, action handlers, dependency manifests, backend implementation, and backend tests are unchanged. No generated dashboard images or sample-data substitutions are included. No deployment or remote branch mutation was performed.

## Validation

- Production build: passed (`npm run build`, including TypeScript checks).
- Frontend lint: exit code 0; five existing warnings also present on untouched `main`.
- Repository offline CI group: 63 passed, 1 skipped. One upstream deprecation warning.
- Browser review: landing, Numeric and Image overviews, device navigation, console tabs, memory filtering, conflict views, Reports, workspace switcher, and motion preference.
- Layout review: desktop at 1363px, tablet frame at 768px, mobile frame at 390px, and device console frame at 320px. No document-level horizontal overflow in those checks. Mobile menu keyboard activation passed.
- Backend, API clients, data types, contexts, and dependency manifests compare identically to the base commit.

The review used a separate disposable copy with the repository's existing sample data enabled. The deliverable keeps both production mock switches unchanged (`false`). No real fleet operations were triggered.

The broad model-dependent suite is not a passing check in this environment. It encountered unavailable embedding models and did not complete. The missing `BAAI/bge-small-en-v1.5` model failure was reproduced on untouched `main`. The offline CI group passed independently. OpenStreetMap tile access was blocked by the provider during browser review; the existing tile service and data behavior were preserved.

## Local verification

```sh
cd frontend
npm ci
npm run build
npm run lint
```

Run backend checks with the repository's pinned environment and locally available model files as appropriate. No commit dates were customized.
