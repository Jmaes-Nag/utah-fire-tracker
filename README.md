This project began as an experiment built largely with AI assistance using Antigravity.

---

# Utah Wildfire & Wind Tracking Dashboard

A modern, full-stack, Dockerized, state-wide wildfire and meteorological tracking dashboard for the state of Utah. It provides live incident metrics, predictive downslope fire threat corridors based on wind speed/direction, FEMA CAP warnings, NOAA HMS smoke plume tracks, and air quality monitor integrations.

Designed for emergency coordinators, researchers, and citizens alike, the application combines advanced spatial analysis (using Shapely in Python) with a premium, responsive multi-panel web interface.

---

## 🌟 Key Features

*   **🖥️ Premium 3-Panel Grid**: Desktop-first layout featuring collapsible sidebar lists (alerts & active incidents), a Leaflet map canvas in the center, and a meteorological telemetry dock on the right.
*   **🔮 Modeled "Cities At Risk" Alerts**: An in-memory engine applies a **5-mile** buffer and a heuristic wind displacement to incident locations, then intersects municipalities (Level 1: Ready, Level 2: Set). This is a proximity estimate, not a validated fire-spread prediction.
*   **🚨 CAP Warning Integration**: Reads wildfire and evacuation warnings from the configured ArcGIS CAP feed and draws matched boundaries on the map.
*   **💨 NOAA HMS Smoke Plumes**: Renders semi-transparent, multi-layered polygons representing Light (15% opacity), Medium (25%), and Heavy (40%) satellite-detected smoke tracks.
*   **🌱 PM2.5 Monitoring**: Renders the configured OpenAQ ArcGIS station feed, estimates an index using EPA PM2.5 breakpoints, and color-codes markers. This calculation is not an official AirNow AQI or NowCast product.
*   **💡 Help Bubbles & Tooltips**: Floating frosted-glass help dialogs explaining meteorological physics, emergency levels, and PM2.5 soot concerns to non-technical users.
*   **🔍 Data Transparency Page**: A dedicated `/sources.html` catalog describing the formats, frequencies, and parameters of all referenced federal and state GIS servers.
*   **🛡️ Resilient Feed Loading**: Uses process-local caches, coalesced refreshes, last-good fallback with freshness headers, complete ArcGIS pagination, and independent frontend feed loading. Weather observations have a shorter cache lifetime.
*   **🌗 Adaptive Theme Engine**: Adapts the interface theme and uses Carto dark/light tiles when a key is configured. Without a key, it uses the standard OpenStreetMap basemap.

---

## 🛠️ Technology Stack

### Backend
*   **FastAPI & Uvicorn**: High-performance Python web framework and ASGI server.
*   **Shapely**: High-speed, lightweight spatial geometry engine for vector intersections.
*   **HTTPX**: Asynchronous client used to query remote GIS APIs in parallel.

### Frontend
*   **Tailwind CSS (v3)**: Modern utility-first CSS styling.
*   **Leaflet.js**: Lightweight open-source mapping engine.
*   **Leaflet.markercluster**: Groups dense hot spots into readable aggregates.

### Infrastructure & Operations
*   **Nginx (Alpine)**: Serves static frontend resources and proxies API requests.
*   **Docker & Docker Compose**: Simplifies deployment into isolated, reproducible containers.

---

## 📂 Project Structure

```text
Utah_fire_tracker/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py            # FastAPI app, CORS, startup, route registration
│   │   ├── config.py          # Service URLs and tuning constants
│   │   ├── cache.py           # Cached refresh with freshness headers
│   │   ├── arcgis.py          # ArcGIS feature-service client
│   │   ├── geometry.py        # Polygon validation and wind-corridor geometry
│   │   ├── feeds.py           # Incidents, perimeters, hotspots, smoke, AQI
│   │   ├── weather.py         # NWS observations
│   │   ├── boundaries.py      # Municipal boundary loading and recovery
│   │   ├── alerts.py          # Cities-at-risk engine and CAP warnings
│   │   └── util.py            # Shared helpers
│   ├── Dockerfile
│   ├── tests/                 # Spatial, cache, feed and API regression tests
│   ├── requirements.txt       # Pinned runtime dependencies (fastapi, httpx, shapely, uvicorn)
│   └── requirements-dev.txt   # Runtime dependencies plus the ruff linter/formatter
├── frontend/
│   ├── app.js                 # Leaflet Map handlers, Tooltips, & API queries
│   ├── index.html             # Dashboard Skeleton
│   ├── sources.html           # Data Transparency Page
│   ├── styles.css             # Legend styling & transition overrides
│   ├── nginx.conf.template    # Nginx reverse-proxy configuration
│   ├── tests/                 # Dependency-free frontend regression tests
│   ├── package.json           # Development tooling only (Prettier); no runtime npm dependencies
│   └── Dockerfile
├── docker-compose.yml         # Container Orchestration
└── .gitignore                 # Excludes local environments & downloaded GeoJSONs
```

---

## 🚀 Quick Start

### Prerequisites
*   [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Docker Compose)

### 1. Run the Application
From the project root directory, run:
```bash
cp .env.example .env
docker compose up -d --build
```
This command compiles the custom backend and Nginx frontend images, downloads any missing base images, and launches the stack.

### 2. Access the Dashboard
Once the container startup logs show `Uvicorn running on http://0.0.0.0:8000`, open your browser and navigate to:
```text
http://localhost:8080
```

Set `NWS_USER_AGENT` in `.env` to identify your deployment with a real contact address. Municipal boundaries persist in the `municipal_data` Docker volume. The frontend starts once the backend health endpoint responds; this health check reports process availability, not upstream feed completeness. Use the dashboard's feed status to see unavailable, incomplete or stale data.

Without `CARTO_API_KEY`, the map uses standard OpenStreetMap tiles in both interface themes. Their public service is best-effort and subject to the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/). Choose provider capacity before increasing traffic. A configured Carto key stays server-side.

---

## 🧪 Running Automated Tests

Run the backend tests from the repository root in a local virtual environment:
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-dev.txt
.venv/bin/python -m unittest discover -s backend -p 'test_*.py' -v
```

Run the frontend regression tests with Node.js 20 or later:
```bash
node --check frontend/app.js
node --test frontend/tests/*.test.cjs
```

For the normal development workflow, run `make setup` once, then `make check`. Run `make check-containers` for isolated Docker builds and HTTP/proxy checks with automatic cleanup. Local and CI checks share the same scripts; see [development checks and private-file handling](docs/development.md).

The regression suites use mocked public services and run without live feed availability. GitHub Actions runs both suites, builds the containers, validates the generated Nginx configuration, and checks served assets and inherited HTTP headers. Container-only checks require Docker to be running.

## Reliability and expansion review

The [development guide](docs/development.md) describes repeatable verification and local-file handling. The [continental U.S. assessment](docs/continental-us-plan.md) lays out a staged expansion plan. National coverage is not implemented in this revision. Detailed local review and handoff notes are intentionally ignored by Git.

The wind corridor remains a proximity heuristic rather than a validated fire-spread forecast. Its modeled Ready/Set labels are separate from official evacuation instructions. Missing or stale data does not establish that a location is safe. Municipal risk coverage also depends on successfully loaded municipal boundaries.

The current caches and refresh locks belong to one API process. Additional workers or replicas require shared snapshots and coordinated ingestion before they can offer consistent data and upstream protection. See the expansion assessment for details.

---

## 📡 Spatial Feeds Referenced

The backend aggregates and sanitizes data from the following GIS servers:
1.  **NIFC Point Service**: [WFIGS Incident Locations](https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0)
2.  **NWS Forecast API**: [api.weather.gov](https://api.weather.gov)
3.  **FEMA Alerts Feed**: [CAP Alerts Service](https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/CAP_Alerts_Feed/FeatureServer/0)
4.  **NOAA Smoke Detection**: [HMS Smoke Polygons](https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Smoke_Detection_(v1)/FeatureServer/0)
5.  **OpenAQ Monitoring Layer**: [PM2.5 Feed](https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/Air_Quality_PM25_Latest_Results/FeatureServer/0)
6.  **UGRC Boundary Portal**: [Utah Municipal Boundaries](https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/UtahMunicipalBoundaries/FeatureServer/0) (Downloaded automatically on startup to `backend/app/data/`)


---
## ⚖️ License
This project is open-source and available under the MIT License.
