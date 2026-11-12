# Cleanup plan

Written 2026-11-12 from a read-through of the working tree. Nothing here has been started. The phases are ordered so each one makes the next safer; behaviour should not change in any of them.

## Starting state

- The whole reliability pass is **uncommitted on `main`**: 12 modified files (about 1,400 lines added and removed) and 8 untracked paths, including all of CI, `scripts/`, `docs/`, the Makefile and most of the tests.
- The GitHub Actions workflow has never run on GitHub.
- Frontend tests (23) and the public-file check pass locally. Backend tests were not run for this review because no `.venv` exists; the notes claim 72 passing.
- The code works but is concentrated in two very dense files: `backend/app/main.py` (855 lines) and `frontend/app.js` (1,759 lines).
- Seven markdown files describe the project's state, and they overlap and contradict each other.

## Phase 0 — Land what exists

1. Run `make setup && make check && make check-containers` to confirm the baseline.
2. Move the work to a branch and commit it in reviewable pieces, in this order: backend + tests (with `.gitignore`), infra (compose, nginx, Dockerfiles, Docker ignore files, `.env.example`), frontend + tests, tooling (Makefile, scripts, CI), docs. Infra goes before frontend because Nginx serves the `/map-config.js` that the frontend reads.
3. Push the branch and get the workflow green on the exact commit that will become `main`, then fast-forward.

Do this before any refactor, so the cleanup diffs are not mixed into a 1,400-line behaviour change.

## Phase 1 — Consolidate the documentation

| File | Problem | Action |
| --- | --- | --- |
| `NEXT_STEPS.md` (ignored) | Handoff log: opens with a cleanup note, then describes containers, a `/tmp` venv and agent panels that no longer exist | Extract the still-open follow-ups into one tracked `docs/open-issues.md` (or GitHub issues), then delete |
| `backend/REVIEW.md`, `frontend/REVIEW.md` (ignored) | Narrative of past fixes; test counts are stale (68 vs 72, 22 vs 23) and corrected by appended "resumed" paragraphs | Move each "Remaining limits" section into the open-issues list; delete the rest (git history covers it) |
| `docs/reliability-review.md` (ignored, but sits in tracked `docs/`) | Summarises the two files above; links to them | Delete, or fold a short "known limitations" section into the README |
| `README.md` | Structure tree lists `app/__init__.py` (does not exist) and the wrong root name, and omits `scripts/`, `docs/`, `Makefile`, `.github/`; test instructions appear twice (manual venv and `make`); stack list omits Turf; feature copy ("predictive", "premium") contradicts the caveats lower down | Rewrite the tree, keep only the `make` instructions, align the wording |
| `docs/development.md` | Accurate but written as prose about the review session | Trim to commands and rules |
| `screenshots/` | 8 tracked PNGs, none referenced from the README | Re-take one or two and embed them, or remove |

Afterwards, drop the now-unneeded entries from `.gitignore` (`/NEXT_STEPS.md`, `*/REVIEW.md`, `/docs/reliability-review.md`). Keep `/.codex/` and the other agent-state and credential entries.

## Phase 2 — Tooling guard rails

Status: done on 2026-11-12. Python versions needed no change: the image runs 3.11 and CI already tests 3.11 and 3.14. The HTML pages are deliberately left unformatted until Phase 4.

- Add `ruff` (lint + format) for Python and a formatter for JS/HTML/CSS, wired into `scripts/check.py` and CI. `app.js` has 66 lines over 140 characters; `main.py` relies on packed multi-assignment lines.
- Pin dependencies. `requirements.txt` has only lower bounds; `pydantic` is listed but never imported directly.
- Align Python versions: the Dockerfile runs 3.11, CI tests 3.11 and 3.14, local is 3.14. Pick the deployed version and one newer.
- Move backend tests to `backend/tests/` and add `backend/app/__init__.py`. Update the discovery paths in `scripts/check.py` and the mount in `scripts/check_containers.py` together.

## Phase 3 — Split the backend

`main.py` holds config, caching, the ArcGIS client, geometry validation, six feed routes, weather, boundary persistence and the alert engine. Suggested layout under `backend/app/`:

- `config.py` — URLs, TTLs, the Utah envelope
- `cache.py` — `cached()` and the entry type
- `arcgis.py` — `fetch_arcgis`, `feature_list`, pagination
- `geometry.py` — polygon validation, `threat_geometry`
- `feeds.py` — incidents, perimeters, hotspots, smoke, AQI
- `weather.py`, `boundaries.py`, `alerts.py`
- `main.py` — app, middleware, lifespan, route registration only

Specific things to fix while moving code:

- **Routes call other routes.** `get_alerts` calls `get_incidents` and `get_weather` with throwaway `Response` objects and reads staleness back out of their headers. Separate "load data + freshness" functions from the HTTP handlers.
- **Cache entries are untyped dicts** with keys added ad hoc (`lock`, `failure`, `retry_after`, `incomplete`, `failure_created_at`) next to a separate `LoadedData` dataclass. Replace both with one entry class.
- The incidents cache is named plain `cache`; the tests special-case that name. Rename to `incidents_cache`.
- The Utah envelope is defined twice (a Shapely `box` and a string in `spatial_params`), and the weather route has a third, wider hard-coded box.
- The UTC timestamp `strftime` expression is repeated five times.
- `set_data_headers` sets `Access-Control-Expose-Headers` by hand although the CORS middleware already does.
- `get_alerts.load()` is about 80 lines with nested closures; break it into incident-risk, CAP-overlay and freshness steps.
- A new `httpx.AsyncClient` is built per upstream call; share one via lifespan.
- `startup_event` is a leftover name from the old event decorator; `muni_boundaries` is a module global mutated with `global`.

Split `test_reliability.py` (855 lines, 5 classes) along the same module lines and merge the 29-line `test_alerts.py` into it. The tests patch module globals by name (`main.cache`, `main.httpx.AsyncClient`), so move tests and code together, one module per commit.

## Phase 4 — Split the frontend

- Break `app.js` into ES modules without adding a build step: state, API/normalisation, map layers, incidents list, telemetry, alerts, location search, UI chrome (theme, collapsibles, tooltips). The test harness loads `app.js` as one script into a `vm` with hand-written DOM doubles, so it has to change in the same step.
- Move tooltip copy (the `tooltipsData` block near line 1634) and other long HTML strings out of the logic.
- `index.html` and `sources.html` each carry the same 21-line inline Tailwind config plus duplicated theme logic. Extract to a shared file.
- Replace the Tailwind CDN script with a built or vendored stylesheet. It is the reason the CSP needs `'unsafe-inline'` and `'unsafe-eval'`.
- Pin and add SRI hashes for markercluster and Turf (`@turf/turf@6` floats); only Leaflet has them today. Consider vendoring all of them.
- Wind-corridor and cardinal-direction logic exists in both `app.js` and `main.py`. Decide whether the browser should ask the backend instead.

## Phase 5 — Infra odds and ends

- `nginx.conf.template` sends `Cache-Control: no-store` on everything, including static assets and proxied tiles. Revisit once assets are versioned.
- The public DNS resolvers (`8.8.8.8`, `1.1.1.1`) are hard-coded for the tile proxy.
- `docker-compose.yml` uses fixed `container_name`s, which blocks running two copies side by side.
- `/api/health` is liveness only; decide whether it should report boundary and feed state.

## Out of scope

The product follow-ups listed in `NEXT_STEPS.md` (stale-retention limits, CAP deduplication, NowCast AQI, national expansion) are feature work, not cleanup. Preserve them in the open-issues list during Phase 1 and schedule them separately.
