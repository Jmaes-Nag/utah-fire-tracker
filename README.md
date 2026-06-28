// You should know, that this project was just about entirely vibecoded using Antigravity. This project is an experiment on using AI to create something.

---

# Utah Wildfire & Wind Tracking Dashboard

A modern, full-stack, Dockerized, state-wide wildfire and meteorological tracking dashboard for the state of Utah. It provides live incident metrics, predictive downslope fire threat corridors based on wind speed/direction, FEMA CAP warnings, NOAA HMS smoke plume tracks, and air quality monitor integrations.

Designed for emergency coordinators, researchers, and citizens alike, the application combines advanced spatial analysis (using Shapely in Python) with a premium, responsive multi-panel web interface.

---

## 🌟 Key Features

*   **🖥️ Premium 3-Panel Grid**: Desktop-first layout featuring collapsible sidebar lists (alerts & active incidents), a Leaflet map canvas in the center, and a meteorological telemetry dock on the right.
*   **🔮 Predictive "Cities At Risk" Alerts**: An in-memory analytics engine that reads active fire boundaries, translates threat projections downwind using NWS wind vectors (applying a base **5-mile** buffer + wind speed displacement), and alerts municipalities in the path (Level 1: Ready, Level 2: Set).
*   **🚨 FEMA CAP Integration**: Merges active Federal Emergency Warning CAP alerts (evacuations, wildfire warnings) from IPAWS and draws boundaries directly onto the map.
*   **💨 NOAA HMS Smoke Plumes**: Renders semi-transparent, multi-layered polygons representing Light (15% opacity), Medium (25%), and Heavy (40%) satellite-detected smoke tracks.
*   **🌱 EPA Air Quality Index**: Renders live PM2.5 monitoring stations synced from EPA AirNow via OpenAQ, calculating the US EPA AQI index and color-coding markers according to health thresholds.
*   **💡 Help Bubbles & Tooltips**: Floating frosted-glass help dialogs explaining meteorological physics, emergency levels, and PM2.5 soot concerns to non-technical users.
*   **🔍 Data Transparency Page**: A dedicated `/sources.html` catalog describing the formats, frequencies, and parameters of all referenced federal and state GIS servers.
*   **🛡️ API Cache Protection**: Implements a 15-minute in-memory cache on all ArcGIS and OpenAQ routes to ensure fast load times and shield public endpoints from rate limits.
*   **🌗 Adaptive Theme Engine**: Toggles between Dark Matter and Positron base tiles while seamlessly adapting styles in Tailwind CSS.

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
│   │   └── main.py            # FastAPI Application & Spatial Engines
│   ├── Dockerfile
│   ├── requirements.txt       # Python Dependencies (shapely, fastapi, httpx, etc.)
│   └── test_alerts.py         # Unit tests (wind vectors, spatial checks, AQI math)
├── frontend/
│   ├── app.js                 # Leaflet Map handlers, Tooltips, & API queries
│   ├── index.html             # Dashboard Skeleton
│   ├── sources.html           # Data Transparency Page
│   ├── styles.css             # Legend styling & transition overrides
│   ├── nginx.conf             # Nginx reverse-proxy configuration
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
docker compose up -d --build
```
This command compiles the custom backend and Nginx frontend images, downloads any missing base images, and launches the stack.

### 2. Access the Dashboard
Once the container startup logs show `Uvicorn running on http://0.0.0.0:8000`, open your browser and navigate to:
```text
http://localhost
```

---

## 🧪 Running Automated Tests

To verify spatial projections, coordinate translations, and AQI breakpoint math, execute the test suite inside the running backend container:
```bash
docker exec utah_wildfire_backend python /app/test_alerts.py
```

Expected output:
```text
Ran 3 tests in 0.188s
OK
```

---

## 📡 Spatial Feeds Referenced

The backend aggregates and sanitizes data from the following GIS servers:
1.  **NIFC Point Service**: [WFIGS Incident Locations](https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0)
2.  **NWS Forecast API**: [api.weather.gov](https://api.weather.gov)
3.  **FEMA Alerts Feed**: [CAP Alerts Service](https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/CAP_Alerts_Feed/FeatureServer/0)
4.  **NOAA Smoke Detection**: [HMS Smoke Polygons](https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Smoke_Detection_(v1)/FeatureServer/0)
5.  **EPA AirNow Network**: [OpenAQ PM2.5 Feed](https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/Air_Quality_PM25_Latest_Results/FeatureServer/0)
6.  **UGRC Boundary Portal**: [Utah Municipal Boundaries](https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/UtahMunicipalBoundaries/FeatureServer/0) (Downloaded automatically on startup to `backend/app/data/`)


---
## ⚖️ License
This project is open-source and available under the MIT License.