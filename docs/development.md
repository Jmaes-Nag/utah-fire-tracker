# Repeatable development checks

From the repository root, run:

```sh
make setup             # Create .venv and install backend dependencies once
make check             # Offline backend/frontend/script tests, public files, whitespace
make check-containers  # Image builds, Python 3.11 tests, Nginx/HTTP/API proxy checks
```

Python 3.11 or later and Node.js 20 or later are required. The container checks also require a running Docker engine. `PYTHON=/path/to/python make check` selects another interpreter; when `.venv` exists the backend runner uses that environment. The commands work without a configured Carto key or downloaded municipal data.

For a shorter feedback cycle, use `make check-backend`, `make check-frontend`, or `make check-public`. The Python entry points are available directly if Make is unavailable, for example `python3 scripts/check.py --suite frontend`.

GitHub Actions uses these same scripts. No deployment is performed. Backend tests use controlled upstream responses, and the container smoke check uses a synthetic municipal boundary and calls only `/api/health`. Container configuration is checked twice: without Carto, and with an obvious fake key that is never sent to Carto. This verifies configuration selection without relying on third-party service availability or real credentials.

The container runner creates unique container/image/network names, obtains unused localhost ports, waits for readiness, and removes its resources in `finally`, including after a failed assertion or Ctrl-C. It leaves existing application/review containers alone. If the Docker engine becomes unavailable during cleanup, it reports the name prefix to inspect. There is no need to repeatedly invent image tags, ports, Docker commands or manual cleanup lists.

To check an already running instance explicitly:

```sh
python3 scripts/check_frontend_http.py http://127.0.0.1:8080 --expected-carto true
```

This checks headers, assets, 404s and the selected provider flag. It does not establish that a provider returned real map imagery. A valid configured Carto key still needs an actual browser check in both themes; a provider can return an HTTP-200 placeholder image. The offline checks do not read `.env` or run live tile/feed requests.

## Public and local files

The repository intentionally keeps source, tests, example configuration and the continental-U.S. planning document public. `.gitignore` excludes environment variants, private keys, local AWS/Codex state, local review/handoff notes, browser network captures and generated reports. `.env.example` and named `.env.*.example` templates remain publishable and must contain placeholders only. Put new private notes, screenshots and diagnostic exports under `.local/`.

`make check-public` checks both tracked and new publishable files for recognizable credential patterns and warns if Git already tracks an ignored path. Findings show only filenames and categories, never credential values. This is a focused guard, not a comprehensive secret scanner or history audit. `.gitignore` does not remove already tracked content or make earlier commits private.

Docker has separate ignore files: frontend builds accept only the files served by Nginx and its configuration, and backend builds exclude downloaded boundaries, bytecode and local credential files. The backend downloads and validates municipal boundaries on first startup, then persists them in the configured Docker volume. Git ignore rules alone do not restrict Docker build contexts.
