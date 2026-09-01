// Application State
const state = {
    incidents: [],
    filteredIncidents: [],
    activeIncidentId: null,
    alerts: [],
    activeFemaLayer: null,
    smokeLayerGroup: null,
    aqiLayerGroup: null,
    hotspotsLayerGroup: null,
    perimetersLayerGroup: null,
    legendControl: null,
    map: null,
    tileLayer: null,
    markersClusterGroup: null,
    markersMap: new Map(), // Maps incident.id -> Leaflet Marker
    searchQuery: "",
    theme: localStorage.getItem("theme") || "light", // default to light
    leftCollapsed: window.innerWidth < 1024,
    rightCollapsed: window.innerWidth < 1024
};

// Utility to escape HTML, preventing XSS injection from external APIs
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// DOM Elements
const searchInput = document.getElementById("search-input");
const statTotalFires = document.getElementById("stat-total-fires");
const statTotalAcres = document.getElementById("stat-total-acres");
const statAvgContain = document.getElementById("stat-avg-contain");
const incidentCards = document.getElementById("incident-cards");
const sidebarLoading = document.getElementById("sidebar-loading");
const sidebarEmpty = document.getElementById("sidebar-empty");
const mapCoordinates = document.getElementById("map-coordinates");
const mapResetBtn = document.getElementById("map-reset-btn");
const themeToggleBtn = document.getElementById("theme-toggle-btn");
const themeToggleIcon = document.getElementById("theme-toggle-icon");

// Telemetry Elements
const telemetryEmpty = document.getElementById("telemetry-empty");
const telemetryLoading = document.getElementById("telemetry-loading");
const telemetryError = document.getElementById("telemetry-error");
const telemetryErrorMsg = document.getElementById("telemetry-error-msg");
const telemetryRetryBtn = document.getElementById("telemetry-retry-btn");
const telemetryData = document.getElementById("telemetry-data");

const telFireName = document.getElementById("tel-fire-name");
const telFireAcres = document.getElementById("tel-fire-acres");
const telFireCounty = document.getElementById("tel-fire-county");
const telFireCoords = document.getElementById("tel-fire-coords");
const windCompassNeedle = document.getElementById("wind-compass-needle");
const telWindDirCard = document.getElementById("tel-wind-dir-card");
const telWindDirDeg = document.getElementById("tel-wind-dir-deg");
const telWindSpeed = document.getElementById("tel-wind-speed");
const telWindSpeedMetric = document.getElementById("tel-wind-speed-metric");
const telTemp = document.getElementById("tel-temp");
const telTempMetric = document.getElementById("tel-temp-metric");
const telHumidityVal = document.getElementById("tel-humidity-val");
const telHumidityBar = document.getElementById("tel-humidity-bar");
const telStationId = document.getElementById("tel-station-id");
const telStationName = document.getElementById("tel-station-name");
const telObsTime = document.getElementById("tel-obs-time");

// Initialize Theme UI
function initTheme() {
    if (state.theme === "light") {
        document.documentElement.classList.remove("dark");
        themeToggleIcon.className = "fa-solid fa-moon text-sm";
    } else {
        document.documentElement.classList.add("dark");
        themeToggleIcon.className = "fa-solid fa-sun text-sm";
    }
}

// Update Map Tile Layer based on theme
function updateMapTiles() {
    if (state.tileLayer) {
        state.map.removeLayer(state.tileLayer);
    }
    
    // Choose CartoDB Dark Matter or Positron (light mode)
    const cartoApiKey = '__CARTO_API_KEY__';
    // Check that it doesn't start with the placeholder syntax so `sed` doesn't break our condition
    const apiKeyParam = (cartoApiKey && !cartoApiKey.startsWith('__CARTO')) ? `?key=${encodeURIComponent(cartoApiKey)}` : '';
    
    const url = state.theme === "dark" 
        ? `https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png${apiKeyParam}`
        : `https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png${apiKeyParam}`;
        
    state.tileLayer = L.tileLayer(url, {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        subdomains: 'abcd',
        maxZoom: 20
    }).addTo(state.map);
}

// Initialize Mapping Canvas
function initMap() {
    // Center of Utah
    const utahCenter = [39.3210, -111.0937];
    const defaultZoom = 7;

    state.map = L.map('map', {
        zoomControl: false,
        attributionControl: true
    }).setView(utahCenter, defaultZoom);

    // Create custom pane for smoke layers (z-index 350, behind other overlays at 400)
    state.map.createPane('smokePane');
    state.map.getPane('smokePane').style.zIndex = 350;

    // Custom Zoom Control Position
    L.control.zoom({
        position: 'bottomright'
    }).addTo(state.map);

    // Load initial tiles
    updateMapTiles();

    // Setup Perimeters Layer Group (so it renders underneath markers)
    state.perimetersLayerGroup = L.layerGroup().addTo(state.map);

    // Setup Marker Cluster Group
    state.markersClusterGroup = L.markerClusterGroup({
        showCoverageOnHover: false,
        zoomToBoundsOnClick: true,
        maxClusterRadius: 40
    });
    state.map.addLayer(state.markersClusterGroup);

    // Dynamic coordinates display on map move
    state.map.on('mousemove', function (e) {
        mapCoordinates.textContent = `${e.latlng.lat.toFixed(4)}° N, ${e.latlng.lng.toFixed(4)}° W`;
    });

    // Reset View Button Click Handler
    mapResetBtn.addEventListener('click', () => {
        state.map.flyTo(utahCenter, defaultZoom, { duration: 1.5 });
        deselectIncident();
        if (state.activeFemaLayer) {
            state.map.removeLayer(state.activeFemaLayer);
            state.activeFemaLayer = null;
        }
    });

    // Theme Toggle Button Click Handler
    themeToggleBtn.addEventListener('click', () => {
        if (state.theme === "dark") {
            state.theme = "light";
            document.documentElement.classList.remove("dark");
            themeToggleIcon.className = "fa-solid fa-moon text-sm";
        } else {
            state.theme = "dark";
            document.documentElement.classList.add("dark");
            themeToggleIcon.className = "fa-solid fa-sun text-sm";
        }
        localStorage.setItem("theme", state.theme);
        updateMapTiles();
    });

    // Setup Layer Groups
    state.smokeLayerGroup = L.layerGroup().addTo(state.map);
    state.aqiLayerGroup = L.layerGroup(); // Not enabled by default
    state.hotspotsLayerGroup = L.layerGroup(); // Not enabled by default

    // Layer Toggle Control
    const overlayMaps = {
        "<span class='text-xs font-semibold flex items-center gap-1.5 text-slate-700 dark:text-slate-200'><i class='fa-solid fa-draw-polygon text-slate-500'></i> Burn Perimeters</span>": state.perimetersLayerGroup,
        "<span class='text-xs font-semibold flex items-center gap-1.5 text-slate-700 dark:text-slate-200'><i class='fa-solid fa-circle-dot text-rose-500 animate-pulse'></i> Active Heat (Hotspots)</span>": state.hotspotsLayerGroup,
        "<span class='text-xs font-semibold flex items-center gap-1.5 text-slate-700 dark:text-slate-200'><i class='fa-solid fa-smog text-amber-500'></i> Smoke Plumes</span>": state.smokeLayerGroup,
        "<span class='text-xs font-semibold flex items-center gap-1.5 text-slate-700 dark:text-slate-200'><i class='fa-solid fa-wind text-emerald-500'></i> Air Quality Index</span>": state.aqiLayerGroup
    };
    L.control.layers(null, overlayMaps, { position: 'topright', collapsed: false }).addTo(state.map);

     // Map Legend Control
     state.legendControl = L.control({ position: 'bottomright' });
     state.legendControl.onAdd = function (map) {
         const div = L.DomUtil.create('div', 'info legend');
         div.innerHTML = `
             <h4>Map Layers Index <span class='help-tooltip-trigger cursor-pointer text-slate-400 hover:text-slate-650 dark:text-slate-500 dark:hover:text-slate-350 ml-1' data-tooltip='tooltip-smoke-aqi' title='What do these layers mean?'><i class='fa-solid fa-circle-question fa-xs'></i></span></h4>
            <div class="legend-section">
                <span class="block font-bold text-[9px] text-slate-400 dark:text-slate-555 uppercase tracking-wider mb-1">HMS Smoke Density</span>
                <div class="legend-item">
                    <span class="legend-color-box" style="background: #ef4444; opacity: 0.4;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Heavy Density</span>
                </div>
                <div class="legend-item">
                    <span class="legend-color-box" style="background: #f97316; opacity: 0.25;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Medium Density</span>
                </div>
                <div class="legend-item">
                    <span class="legend-color-box" style="background: #eab308; opacity: 0.15;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Light Density</span>
                </div>
            </div>
            <div class="legend-section border-t border-slate-200 dark:border-slate-800 pt-2 mt-2">
                <span class="block font-bold text-[9px] text-slate-400 dark:text-slate-555 uppercase tracking-wider mb-1">PM2.5 Air Quality (AQI)</span>
                <div class="legend-item">
                    <span class="legend-dot" style="background: #7f1d1d;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Hazardous (301+)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-dot" style="background: #a855f7;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Very Unhealthy (201-300)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-dot" style="background: #ef4444;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Unhealthy (151-200)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-dot" style="background: #f97316;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Sensitive Groups (101-150)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-dot" style="background: #eab308;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Moderate (51-100)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-dot" style="background: #10b981;"></span>
                    <span class="text-slate-650 dark:text-slate-350">Good (0-50)</span>
                </div>
            </div>
        `;
        return div;
    };
    state.legendControl.addTo(state.map);

    // Manage Legend visibility dynamically based on active overlays
    function updateLegendVisibility() {
        const hasSmoke = state.map.hasLayer(state.smokeLayerGroup);
        const hasAqi = state.map.hasLayer(state.aqiLayerGroup);
        
        if (hasSmoke || hasAqi) {
            if (!state.legendControl._map) {
                state.legendControl.addTo(state.map);
            }
            const sections = document.querySelectorAll(".legend-section");
            const smokeSec = sections[0];
            const aqiSec = sections[1];
            if (smokeSec) smokeSec.style.display = hasSmoke ? 'block' : 'none';
            if (aqiSec) aqiSec.style.display = hasAqi ? 'block' : 'none';
            
            // Toggle separator class
            if (aqiSec) {
                if (hasSmoke && hasAqi) {
                    aqiSec.classList.add("border-t", "pt-2", "mt-2");
                } else {
                    aqiSec.classList.remove("border-t", "pt-2", "mt-2");
                }
            }
        } else {
            state.legendControl.remove();
        }
    }
    
    state.map.on('overlayadd', updateLegendVisibility);
    state.map.on('overlayremove', updateLegendVisibility);
    updateLegendVisibility();
}

// Fetch Incidents and Layers from Backend API
async function fetchIncidents() {
    sidebarLoading.classList.remove("hidden");
    sidebarEmpty.classList.add("hidden");
    incidentCards.innerHTML = "";

    try {
        const [incidentsRes, alertsRes, smokeRes, aqiRes, perimetersRes, hotspotsRes] = await Promise.all([
            fetch("/api/incidents"),
            fetch("/api/alerts"),
            fetch("/api/smoke"),
            fetch("/api/aqi"),
            fetch("/api/perimeters"),
            fetch("/api/hotspots")
        ]);
        
        const failedFeeds = [];
        if (incidentsRes.status >= 400) failedFeeds.push("Wildfire Incidents");
        if (alertsRes.status >= 400) failedFeeds.push("FEMA CAP Alerts");
        if (smokeRes.status >= 400) failedFeeds.push("Smoke Plumes");
        if (aqiRes.status >= 400) failedFeeds.push("Air Quality (AQI)");
        if (perimetersRes.status >= 400) failedFeeds.push("Burn Perimeters");
        if (hotspotsRes.status >= 400) failedFeeds.push("Active satellite hotspots");

        if (incidentsRes.status >= 400) {
            throw new Error(`API returned HTTP ${incidentsRes.status} for incidents`);
        }
        
        state.incidents = await incidentsRes.json();
        state.filteredIncidents = [...state.incidents];
        
        // Read Cache Sync Time from Response Headers
        const syncTimestamp = incidentsRes.headers.get("X-Data-Timestamp");
        if (syncTimestamp) {
            try {
                const date = new Date(syncTimestamp);
                if (!isNaN(date.getTime())) {
                    const localFormatted = date.toLocaleString(undefined, {
                        year: 'numeric',
                        month: '2-digit',
                        day: '2-digit',
                        hour: '2-digit',
                        minute: '2-digit',
                        second: '2-digit',
                        hour12: true
                    });
                    document.getElementById("cache-indicator").innerHTML = `<i class="fa-solid fa-arrows-rotate text-emerald-500 animate-pulse"></i> Sync: ${localFormatted}`;
                } else {
                    document.getElementById("cache-indicator").innerHTML = `<i class="fa-solid fa-arrows-rotate text-emerald-500 animate-pulse"></i> Sync: ${syncTimestamp}`;
                }
            } catch (e) {
                console.error("Error formatting sync time:", e);
                document.getElementById("cache-indicator").innerHTML = `<i class="fa-solid fa-arrows-rotate text-emerald-500 animate-pulse"></i> Sync: ${syncTimestamp}`;
            }
            document.getElementById("cache-indicator").title = "Timestamp of backend NIFC API cache synchronization (local time)";
        } else {
            document.getElementById("cache-indicator").innerHTML = `<i class="fa-solid fa-check text-emerald-500"></i> Up-to-date`;
        }
        
        // Display partial feed failures if any occurred
        if (failedFeeds.length > 0) {
            const indicatorEl = document.getElementById("cache-indicator");
            if (indicatorEl) {
                indicatorEl.innerHTML = `<span class="text-rose-500 font-bold hover:underline flex items-center gap-1.5"><i class="fa-solid fa-triangle-exclamation animate-pulse"></i> Feed Outage (${failedFeeds.length})</span>`;
                indicatorEl.title = `The following services are currently experiencing issues: ${failedFeeds.join(", ")}. Using offline backup caches where available.`;
            }
        }
        
        try {
            if (alertsRes.status < 400) {
                state.alerts = await alertsRes.json();
            } else {
                console.error(`API returned HTTP ${alertsRes.status} for alerts`);
                state.alerts = [];
            }
        } catch (e) {
            console.error("Error parsing alerts response:", e);
            state.alerts = [];
        }

        // Parse and render Burn Perimeters
        try {
            if (perimetersRes && perimetersRes.status < 400) {
                const perimeters = await perimetersRes.json();
                renderPerimeters(perimeters);
            }
        } catch (e) {
            console.error("Error parsing burn perimeters:", e);
        }

        // Parse and render NOAA Smoke Plumes
        try {
            if (smokeRes.status < 400) {
                const plumes = await smokeRes.json();
                renderSmokePlumes(plumes);
            }
        } catch (e) {
            console.error("Error parsing smoke plumes:", e);
        }

        // Parse and render AQI Stations
        try {
            if (aqiRes.status < 400) {
                const stations = await aqiRes.json();
                renderAqiStations(stations);
            }
        } catch (e) {
            console.error("Error parsing AQI stations:", e);
        }
        
        // Parse and render Thermal Hotspots
        try {
            if (hotspotsRes.status < 400) {
                const hotspots = await hotspotsRes.json();
                renderHotspots(hotspots);
            }
        } catch (e) {
            console.error("Error parsing hotspots:", e);
        }
        
        updateStats();
        renderAlerts();
        renderIncidents();
        populateMapMarkers();
    } catch (error) {
        console.error("Error fetching incidents:", error);
        sidebarLoading.classList.add("hidden");
        sidebarEmpty.classList.remove("hidden");
        const headingP = sidebarEmpty.querySelector("p");
        if (headingP) headingP.textContent = "Database Connection Offline";
        const descP = sidebarEmpty.querySelectorAll("p")[1];
        if (descP) descP.textContent = "Could not fetch wildfire incidents from API.";
        
        const indicatorEl = document.getElementById("cache-indicator");
        if (indicatorEl) {
            indicatorEl.innerHTML = `<span class="text-rose-500 font-bold flex items-center gap-1.5"><i class="fa-solid fa-triangle-exclamation animate-pulse"></i> Offline</span>`;
            indicatorEl.title = "Cannot establish connection to the local tracker backend server.";
        }
    }
}

// Update Dashboard Statistics Card
function updateStats() {
    const totalFires = state.incidents.length;
    statTotalFires.textContent = totalFires;

    const totalAcres = state.incidents.reduce((sum, item) => sum + (item.acres || 0), 0);
    if (totalAcres >= 1000) {
        statTotalAcres.textContent = (totalAcres / 1000).toFixed(1) + "k";
    } else {
        statTotalAcres.textContent = Math.round(totalAcres);
    }
    statTotalAcres.title = `${Math.round(totalAcres).toLocaleString()} Acres`;

    // Filter out null/undefined containment values to calculate average
    const containmentVals = state.incidents
        .map(item => item.containment)
        .filter(val => val !== null && val !== undefined);
        
    if (containmentVals.length > 0) {
        const avgContain = containmentVals.reduce((sum, val) => sum + val, 0) / containmentVals.length;
        statAvgContain.textContent = Math.round(avgContain) + "%";
    } else {
        statAvgContain.textContent = "N/A";
    }
}

// Helper to switch visual class names for selected vs unselected cards
function setCardActiveState(cardEl, isActive) {
    if (isActive) {
        cardEl.classList.remove('border-slate-200', 'dark:border-slate-800', 'bg-slate-50', 'dark:bg-slate-950/70', 'hover:border-slate-350', 'dark:hover:border-slate-700');
        cardEl.classList.add('border-brand-orange', 'bg-orange-50/40', 'dark:bg-slate-800', 'ring-1', 'ring-brand-orange/40');
    } else {
        cardEl.classList.remove('border-brand-orange', 'bg-orange-50/40', 'dark:bg-slate-800', 'ring-1', 'ring-brand-orange/40');
        cardEl.classList.add('border-slate-200', 'dark:border-slate-800', 'bg-slate-50', 'dark:bg-slate-950/70', 'hover:border-slate-350', 'dark:hover:border-slate-700');
    }
}

// Render Fire Cards in the Sidebar
function renderIncidents() {
    sidebarLoading.classList.add("hidden");
    
    if (state.filteredIncidents.length === 0) {
        sidebarEmpty.classList.remove("hidden");
        incidentCards.innerHTML = "";
        return;
    }
    
    sidebarEmpty.classList.add("hidden");
    
    const cardsHtml = state.filteredIncidents.map(incident => {
        const isActive = state.activeIncidentId === incident.id;
        const containPercent = incident.containment !== null && incident.containment !== undefined ? incident.containment : 0;
        const containText = incident.containment !== null && incident.containment !== undefined ? `${incident.containment}%` : 'Uncontained';
        const acresText = incident.acres ? Math.round(incident.acres).toLocaleString() : 'Size Info N/A';
        
        // Format discovery date
        let dateStr = "N/A";
        if (incident.discovered) {
            const dateObj = new Date(incident.discovered);
            dateStr = dateObj.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        }

        return `
            <div 
                id="card-${incident.id}" 
                data-incident-id="${incident.id}"
                onclick="selectIncidentById('${incident.id}')"
                class="border rounded-xl p-4 transition-all duration-200 cursor-pointer shadow-md flex flex-col justify-between border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950/70 hover:border-slate-350 dark:hover:border-slate-700 text-slate-800 dark:text-slate-100"
            >
                <div class="flex justify-between items-start mb-1.5">
                    <h3 class="font-semibold text-slate-800 dark:text-slate-100 text-sm truncate max-w-[200px]" title="${escapeHtml(incident.name)}">
                        ${escapeHtml(incident.name)}
                    </h3>
                    <span class="text-[10px] px-2 py-0.5 rounded bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-brand-amber font-mono font-medium">
                        ${acresText} Ac
                    </span>
                </div>
                
                <p class="text-xs text-slate-500 dark:text-slate-400 flex items-center gap-1.5 mb-3">
                    <i class="fa-solid fa-map-pin text-[10px] text-slate-400 dark:text-slate-500"></i> ${escapeHtml(incident.county)} County
                </p>
                
                <!-- Containment Bar -->
                <div class="space-y-1 mb-2.5">
                    <div class="flex justify-between text-[9px] text-slate-400 dark:text-slate-500 font-bold uppercase tracking-wider">
                        <span>Suppression Status</span>
                        <span class="${containPercent > 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-slate-450 dark:text-slate-550'} font-mono">${containText}</span>
                    </div>
                    <div class="w-full bg-slate-200 dark:bg-slate-900 rounded-full h-1 overflow-hidden">
                        <div class="bg-gradient-to-r from-brand-orange to-emerald-400 h-full" style="width: ${containPercent}%"></div>
                    </div>
                </div>

                <div class="flex justify-between items-center text-[10px] text-slate-400 dark:text-slate-500 pt-2 border-t border-slate-250 dark:border-slate-900/60 font-mono">
                    <span>Cause: <strong class="text-slate-650 dark:text-slate-450">${escapeHtml(incident.cause)}</strong></span>
                    <span>Discovered: ${dateStr}</span>
                </div>
            </div>
        `;
    }).join("");
    
    incidentCards.innerHTML = cardsHtml;
    
    // Sync active classes after initial rendering
    if (state.activeIncidentId) {
        const activeCard = document.querySelector(`[data-incident-id="${state.activeIncidentId}"]`);
        if (activeCard) {
            setCardActiveState(activeCard, true);
        }
    }
}

// Populate Leaflet Map Markers
function populateMapMarkers() {
    // Clear existing markers
    state.markersClusterGroup.clearLayers();
    state.markersMap.clear();

    state.incidents.forEach(incident => {
        // Create custom DivIcon for flame badge
        const isLarge = incident.acres > 1000;
        const iconHtml = `
            <div class="pulse-ring ${isLarge ? 'pulse-ring-large' : ''}"></div>
            <div class="incident-badge ${isLarge ? 'incident-badge-large' : ''}">
                <i class="fa-solid fa-fire text-white animate-pulse"></i>
            </div>
        `;
        
        const customIcon = L.divIcon({
            className: 'pulse-marker',
            html: iconHtml,
            iconSize: isLarge ? [32, 32] : [24, 24],
            iconAnchor: isLarge ? [16, 16] : [12, 12]
        });

        const marker = L.marker([incident.latitude, incident.longitude], { icon: customIcon });

        // Bind interactive popup
        const popupContent = `
            <div class="p-1.5 font-sans select-none">
                <h4 class="font-bold text-slate-800 dark:text-slate-100 text-sm mb-1">${escapeHtml(incident.name)}</h4>
                <p class="text-xs text-slate-500 dark:text-slate-400 flex items-center gap-1.5"><i class="fa-solid fa-location-dot text-slate-400 dark:text-slate-500"></i> ${escapeHtml(incident.county)} County</p>
                <div class="grid grid-cols-2 gap-2 mt-3 pt-2.5 border-t border-slate-200 dark:border-slate-800 text-[10px] font-mono">
                    <div>
                        <span class="text-slate-450 dark:text-slate-550 block">SIZE</span>
                        <strong class="text-brand-orange text-xs">${Math.round(incident.acres).toLocaleString()} Ac</strong>
                    </div>
                    <div>
                        <span class="text-slate-450 dark:text-slate-550 block">CONTAINMENT</span>
                        <strong class="text-emerald-600 dark:text-emerald-400 text-xs">${incident.containment !== null && incident.containment !== undefined ? incident.containment + '%' : '0%'}</strong>
                    </div>
                </div>
            </div>
        `;
        marker.bindPopup(popupContent, { offset: [0, -4] });

        // Event: Click Marker
        marker.on('click', () => {
            selectIncidentById(incident.id, false); // Don't flyTo map again (Leaflet already centers/zooms on marker click)
        });

        state.markersClusterGroup.addLayer(marker);
        state.markersMap.set(incident.id, marker);
    });
}

// Select a Fire Incident
async function selectIncidentById(id, flyToMap = true) {
    // If clicking same card, do nothing
    if (state.activeIncidentId === id) return;

    // Deselect previous
    const prevActiveId = state.activeIncidentId;
    state.activeIncidentId = id;

    // Update active class on cards via classList
    if (prevActiveId) {
        const prevCard = document.querySelector(`[data-incident-id="${prevActiveId}"]`);
        if (prevCard) {
            setCardActiveState(prevCard, false);
        }
    }
    const newCard = document.querySelector(`[data-incident-id="${id}"]`);
    if (newCard) {
        setCardActiveState(newCard, true);
        // Scroll active card into view
        newCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    if (state.rightCollapsed) {
        state.rightCollapsed = false;
        if (window.innerWidth < 1024) {
            state.leftCollapsed = true;
        }
        updateSidebarLayout();
    }

    const incident = state.incidents.find(item => item.id === id);
    if (!incident) return;

    // Fly Map to coordinates
    const marker = state.markersMap.get(id);
    if (marker) {
        if (flyToMap) {
            state.map.flyTo([incident.latitude, incident.longitude], 11, { duration: 1.5 });
        }
        // Open map popup dynamically
        setTimeout(() => {
            marker.openPopup();
        }, flyToMap ? 1400 : 100);
    }

    // Load Weather Telemetry
    await fetchWeatherTelemetry(incident);
}

// Deselect Incident (Recenter state)
function deselectIncident() {
    if (!state.activeIncidentId) return;

    const activeCard = document.querySelector(`[data-incident-id="${state.activeIncidentId}"]`);
    if (activeCard) {
        setCardActiveState(activeCard, false);
    }

    // Close map popups
    state.map.closePopup();

    state.activeIncidentId = null;
    
    // Reset Telemetry panel to default empty state
    telemetryData.classList.add("hidden");
    telemetryError.classList.add("hidden");
    telemetryLoading.classList.add("hidden");
    telemetryEmpty.classList.remove("hidden");
}

// Retrieve Weather Telemetry from Backend
async function fetchWeatherTelemetry(incident) {
    telemetryEmpty.classList.add("hidden");
    telemetryData.classList.add("hidden");
    telemetryError.classList.add("hidden");
    telemetryLoading.classList.remove("hidden");

    // Map base fire info card immediately
    telFireName.textContent = incident.name;
    telFireAcres.textContent = `${Math.round(incident.acres).toLocaleString()} Acres`;
    telFireCounty.textContent = `${incident.county} County`;
    telFireCoords.textContent = `${incident.latitude.toFixed(4)}°, ${incident.longitude.toFixed(4)}°`;

    const weatherApiUrl = `/api/weather?latitude=${incident.latitude}&longitude=${incident.longitude}`;

    try {
        const response = await fetch(weatherApiUrl);
        
        if (!response.status_code && response.status >= 400) {
            throw new Error(`NWS Sensor API Offline (Status ${response.status})`);
        }
        
        const weather = await response.json();
        renderWeatherTelemetry(weather);
        
    } catch (error) {
        console.error("Error retrieving wind telemetry:", error);
        telemetryLoading.classList.add("hidden");
        telemetryError.classList.remove("hidden");
        telemetryErrorMsg.textContent = error.message || "Failed to locate active weather station grid coordinates.";
        
        // Attach retry handler
        telemetryRetryBtn.onclick = () => fetchWeatherTelemetry(incident);
    }
}

// Render Weather Telemetry in Right Panel
function renderWeatherTelemetry(weather) {
    telemetryLoading.classList.add("hidden");
    telemetryData.classList.remove("hidden");

    // Wind Speed values
    telWindSpeed.textContent = weather.wind_speed_mph !== null ? weather.wind_speed_mph : "N/A";
    telWindSpeedMetric.textContent = weather.wind_speed_kmh !== null ? `${weather.wind_speed_kmh} km/h` : "N/A";

    // Wind Direction values
    const deg = weather.wind_direction_deg;
    telWindDirCard.textContent = weather.wind_direction_cardinal || "N/A";
    telWindDirDeg.textContent = deg !== null ? `(${Math.round(deg)}°)` : "(0°)";

    // Rotate compass needle to point in the direction the wind is BLOWING.
    // The NWS degree is the direction *from* which the wind blows. 
    // To show where wind blows, we rotate to (degrees + 180).
    if (deg !== null) {
        const targetRotation = (deg + 180) % 360;
        windCompassNeedle.style.transform = `rotate(${targetRotation}deg)`;
    } else {
        windCompassNeedle.style.transform = 'rotate(0deg)';
    }

    // Temperature values
    telTemp.textContent = weather.temperature_f !== null ? `${Math.round(weather.temperature_f)}°` : "--°";
    telTempMetric.textContent = weather.temperature_c !== null ? `${Math.round(weather.temperature_c)}°C` : "--°C";

    // Humidity values
    const humidity = weather.relative_humidity;
    if (humidity !== null) {
        telHumidityVal.textContent = `${Math.round(humidity)}%`;
        telHumidityBar.style.width = `${humidity}%`;
    } else {
        telHumidityVal.textContent = "N/A";
        telHumidityBar.style.width = "0%";
    }

    // Station sensor info
    telStationId.textContent = weather.station_id || "N/A";
    telStationName.textContent = weather.station_name || "N/A";
    telStationName.title = weather.station_name || "";
    
    // Format timestamp
    if (weather.timestamp) {
        const dateObj = new Date(weather.timestamp);
        telObsTime.textContent = dateObj.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) + " " + dateObj.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    } else {
        telObsTime.textContent = "Unknown Time";
    }
}

// Render Alert Cards in the Sidebar
function renderAlerts() {
    const alertsSection = document.getElementById("alerts-section");
    const alertCards = document.getElementById("alert-cards");
    
    if (!alertsSection || !alertCards) return;
    
    if (state.alerts.length === 0) {
        alertsSection.classList.add("hidden");
        alertCards.innerHTML = "";
        return;
    }
    
    alertsSection.classList.remove("hidden");
    
    const cardsHtml = state.alerts.map((alert, index) => {
        const isSet = alert.risk_level === 2;
        const borderClass = isSet ? "border-l-brand-red dark:border-l-brand-red" : "border-l-brand-amber dark:border-l-brand-amber";
        const badgeColor = isSet ? "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/25" : "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/25";
        const badgeText = isSet ? "Level 2: Set" : "Level 1: Ready";
        
        const hasFema = alert.fema_alerts && alert.fema_alerts.length > 0;
        const femaBadge = hasFema 
            ? `<span class="text-[9px] px-1.5 py-0.5 rounded bg-rose-500/15 text-rose-500 border border-rose-500/30 font-bold uppercase tracking-wider animate-pulse flex items-center gap-1"><i class="fa-solid fa-triangle-exclamation"></i> FEMA</span>` 
            : "";
        
        // List fires threatening this city
        let firesText = "";
        if (alert.fires.length > 0) {
            firesText = alert.fires.map(f => {
                return `
                    <div class="text-[11px] text-slate-500 dark:text-slate-400 mt-1 flex justify-between">
                        <span>🔥 ${escapeHtml(f.name)}</span>
                        <span class="font-mono text-slate-400 dark:text-slate-500">${escapeHtml(f.distance_miles)} mi (${escapeHtml(f.wind_direction)} @ ${escapeHtml(f.wind_speed_mph)} mph)</span>
                    </div>
                `;
            }).join("");
        } else if (hasFema) {
            const firstFema = alert.fema_alerts[0];
            firesText = `
                <div class="text-[11px] text-slate-500 dark:text-slate-400 mt-1 italic">
                    ${escapeHtml(firstFema.event)}: ${escapeHtml(firstFema.headline || 'Active evacuation or warning area')}
                </div>
            `;
        }
        
        const femaPulseClass = hasFema ? "fema-pulse-card" : "";
        
        return `
            <div 
                id="alert-card-${index}" 
                class="border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/50 rounded-xl p-3 shadow-sm hover:shadow-md cursor-pointer transition-all duration-200 border-l-4 ${borderClass} ${femaPulseClass}"
                onclick="selectAlert(${index})"
            >
                <div class="flex justify-between items-start gap-2 mb-1.5">
                    <h3 class="font-bold text-slate-800 dark:text-slate-200 text-xs truncate max-w-[160px]" title="${escapeHtml(alert.city)}">
                        ${escapeHtml(alert.city)}
                    </h3>
                    <div class="flex gap-1 items-center">
                        ${femaBadge}
                        <span class="text-[9px] px-2 py-0.5 rounded border font-bold ${badgeColor}">
                            ${badgeText}
                        </span>
                    </div>
                </div>
                <div class="space-y-1">
                    ${firesText}
                </div>
            </div>
        `;
    }).join("");
    
    alertCards.innerHTML = cardsHtml;
}

// Select Alert click handler (Fly to centroid and show alert details)
function selectAlert(index) {
    const alert = state.alerts[index];
    if (!alert) return;
    
    const lat = alert.centroid[0];
    const lon = alert.centroid[1];
    
    // Close other incidents or popups
    deselectIncident();
    
    // Fly to city centroid
    state.map.flyTo([lat, lon], 12, { duration: 1.5 });
    
    // Draw FEMA alert overlay polygons
    drawFemaPolygon(alert);
    
    // Open Info Popup
    setTimeout(() => {
        let popupHtml = `
            <div class="p-2 font-sans select-none max-w-[260px]">
                <h4 class="font-bold text-slate-800 dark:text-slate-100 text-sm mb-1">🚨 ${escapeHtml(alert.city)}</h4>
                <p class="text-xs text-slate-500 dark:text-slate-400 mb-2 font-semibold">Threat Level: <span class="${alert.risk_level === 2 ? 'text-brand-red font-extrabold' : 'text-brand-amber font-bold'}">${escapeHtml(alert.risk_level_text)}</span></p>
        `;
        
        if (alert.fires.length > 0) {
            popupHtml += `
                <div class="mt-2 pt-2 border-t border-slate-200 dark:border-slate-800">
                    <p class="text-[10px] font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider mb-1">Threatening Wildfires</p>
            `;
            alert.fires.forEach(f => {
                popupHtml += `
                    <p class="text-xs text-slate-650 dark:text-slate-350 flex justify-between font-mono my-0.5">
                        <span>🔥 ${escapeHtml(f.name)}</span>
                        <span>${escapeHtml(f.distance_miles)} mi away</span>
                    </p>
                `;
            });
            popupHtml += `</div>`;
        }
        
        if (alert.fema_alerts && alert.fema_alerts.length > 0) {
            popupHtml += `
                <div class="mt-2 pt-2 border-t border-slate-200 dark:border-slate-800">
                    <p class="text-[10px] font-bold text-brand-red uppercase tracking-wider mb-1 flex items-center gap-1"><i class="fa-solid fa-triangle-exclamation"></i> Active FEMA Alerts</p>
            `;
            alert.fema_alerts.forEach(fa => {
                popupHtml += `
                    <p class="text-xs font-bold text-slate-700 dark:text-slate-200 mt-0.5">${escapeHtml(fa.event)}</p>
                    <p class="text-[10px] text-slate-550 dark:text-slate-400 leading-normal mt-0.5 max-h-[80px] overflow-y-auto custom-scrollbar">${escapeHtml(fa.instruction || fa.description || '')}</p>
                `;
            });
            popupHtml += `</div>`;
        }
        
        popupHtml += `</div>`;
        
        L.popup()
            .setLatLng([lat, lon])
            .setContent(popupHtml)
            .openOn(state.map);
    }, 1400);
}

// Draw FEMA alert polygon overlays on Leaflet
function drawFemaPolygon(alert) {
    if (state.activeFemaLayer) {
        state.map.removeLayer(state.activeFemaLayer);
        state.activeFemaLayer = null;
    }
    
    const layers = [];
    if (alert.fema_alerts && alert.fema_alerts.length > 0) {
        alert.fema_alerts.forEach(fa => {
            if (fa.rings) {
                // Swap coords [lon, lat] -> [lat, lon]
                const LeafletRings = fa.rings.map(ring => {
                    return ring.map(coord => [coord[1], coord[0]]);
                });
                
                const polyLayer = L.polygon(LeafletRings, {
                    color: '#ef4444',
                    weight: 2,
                    fillColor: '#ef4444',
                    fillOpacity: 0.25,
                    dashArray: '5, 5'
                });
                layers.push(polyLayer);
            }
        });
    }
    
    if (layers.length > 0) {
        state.activeFemaLayer = L.featureGroup(layers).addTo(state.map);
    }
}

// Render Burn Perimeters on Leaflet Map
function renderPerimeters(geojsonData) {
    if (state.perimetersLayerGroup) {
        state.perimetersLayerGroup.clearLayers();
    }
    if (!geojsonData) return;
    
    L.geoJSON(geojsonData, {
        style: function (feature) {
            return {
                color: "#2c2c2c",      // charcoal/scorched line border
                weight: 2,
                fillColor: "#111111",  // darker semi-transparent inner fill
                fillOpacity: 0.45,
                opacity: 0.8
            };
        },
        onEachFeature: function (feature, layer) {
            const props = feature.properties || {};
            const name = props.IncidentName || props.poly_IncidentName || props.incidentname || "Active Fire Perimeter";
            const acres = props.FeatureAcres || props.poly_FeatureAcres || props.featureacres || "Unknown";
            const comments = props.Comments || props.poly_Comments || "";
            
            let popupContent = `
                <div class="p-1.5 text-xs font-sans max-w-[200px]">
                    <strong class="text-slate-800 dark:text-slate-100 flex items-center gap-1.5 font-bold"><i class="fa-solid fa-draw-polygon text-slate-500"></i> Burn Perimeter</strong>
                    <div class="mt-2 text-[10px] space-y-1 font-mono">
                        <div>Name: <span class="font-bold text-slate-700 dark:text-slate-350">${escapeHtml(name)}</span></div>
                        <div>Acres: <span class="text-slate-500">${escapeHtml(acres)}</span></div>
            `;
            if (comments) {
                popupContent += `<div>Info: <span class="text-slate-450 text-[9px] block max-h-[60px] overflow-y-auto">${escapeHtml(comments)}</span></div>`;
            }
            popupContent += `
                    </div>
                </div>
            `;
            
            layer.bindPopup(popupContent);
        }
    }).addTo(state.perimetersLayerGroup);
}

// Draw NOAA Smoke Plumes on Leaflet Map
function renderSmokePlumes(plumes) {
    state.smokeLayerGroup.clearLayers();
    
    // Sort plumes ascending by density: Light -> Medium -> Heavy
    // This ensures denser/heavier plumes are drawn last (on top in the DOM),
    // receiving click/popup events first when they overlap.
    const densityPriority = { "Light": 1, "Medium": 2, "Heavy": 3 };
    const sortedPlumes = [...plumes].sort((a, b) => {
        return (densityPriority[a.density] || 0) - (densityPriority[b.density] || 0);
    });
    
    sortedPlumes.forEach(plume => {
        // Swap coordinate format for Leaflet: [[[lon1, lat1], [lon2, lat2], ...]] -> [[[lat1, lon1], [lat2, lon2], ...]]
        const LeafletRings = plume.rings.map(ring => {
            return ring.map(coord => [coord[1], coord[0]]);
        });
        
        // Style based on density
        let color = "#eab308"; // Light (yellow)
        let opacity = 0.15;
        let weight = 1;
        
        if (plume.density === "Medium") {
            color = "#f97316"; // Orange
            opacity = 0.25;
            weight = 1.5;
        } else if (plume.density === "Heavy") {
            color = "#ef4444"; // Red
            opacity = 0.40;
            weight = 2;
        }
        
        const poly = L.polygon(LeafletRings, {
            color: color,
            weight: weight,
            fillColor: color,
            fillOpacity: opacity,
            interactive: true,
            pane: 'smokePane'
        });
        
        poly.bindPopup(`
            <div class="p-1.5 text-xs font-sans">
                <strong class="text-slate-800 dark:text-slate-100 flex items-center gap-1.5 font-bold"><i class="fa-solid fa-smog text-amber-500 animate-pulse"></i> NOAA HMS Smoke Plume</strong>
                <div class="mt-2 text-[10px] space-y-1 font-mono">
                    <div>Density: <span class="font-bold text-slate-700 dark:text-slate-350">${escapeHtml(plume.density)}</span></div>
                    <div>Source: <span class="text-slate-500">${escapeHtml(plume.satellite)}</span></div>
                    <div>Observed: <span class="text-slate-500">${escapeHtml(plume.start)} UTC</span></div>
                </div>
            </div>
        `);
        
        state.smokeLayerGroup.addLayer(poly);
    });
}

// Plot AQI Stations on Leaflet Map
function renderAqiStations(stations) {
    state.aqiLayerGroup.clearLayers();
    
    stations.forEach(station => {
        const marker = L.circleMarker([station.latitude, station.longitude], {
            radius: 6,
            fillColor: station.color,
            color: "#ffffff",
            weight: 1.5,
            opacity: 0.85,
            fillOpacity: 0.9,
            interactive: true
        });
        
        const popupHtml = `
            <div class="p-1.5 font-sans select-none max-w-[200px]">
                <h4 class="font-bold text-slate-800 dark:text-slate-100 text-sm mb-0.5">${escapeHtml(station.name)}</h4>
                <p class="text-[10px] text-slate-450 dark:text-slate-500 font-medium">Source: ${escapeHtml(station.provider)}</p>
                
                <div class="mt-3 pt-2.5 border-t border-slate-200 dark:border-slate-800 flex justify-between items-center gap-4">
                    <div>
                        <span class="text-[9px] text-slate-450 dark:text-slate-550 block">PM2.5</span>
                        <strong class="text-xs text-slate-700 dark:text-slate-300 font-mono">${escapeHtml(station.pm25)} µg/m³</strong>
                    </div>
                    <div class="text-right">
                        <span class="text-[9px] text-slate-450 dark:text-slate-550 block">AQI INDEX</span>
                        <strong class="text-xs font-mono px-2 py-0.5 rounded" style="background: ${station.color}15; color: ${station.color}">${escapeHtml(station.aqi)}</strong>
                    </div>
                </div>
                <div class="mt-2.5 text-[9px] text-center py-1 rounded bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-800/80 font-bold uppercase tracking-wide" style="color: ${station.color}">
                    ${escapeHtml(station.tier)}
                </div>
            </div>
        `;
        
        marker.bindPopup(popupHtml, { offset: [0, -2] });
        state.aqiLayerGroup.addLayer(marker);
    });
}

// Plot active thermal hotspots (heat detection dots) on Leaflet Map
function renderHotspots(hotspots) {
    state.hotspotsLayerGroup.clearLayers();
    
    hotspots.forEach(spot => {
        // Style based on Fire Radiative Power (FRP)
        let color = "#eab308"; // Low intensity (yellow)
        let radius = 3;
        let weight = 0.5;
        
        if (spot.frp >= 100) {
            color = "#ef4444"; // Extremely intense heat (red)
            radius = 5.5;
            weight = 1.5;
        } else if (spot.frp >= 10) {
            color = "#f97316"; // Moderate heat (orange)
            radius = 4;
            weight = 1;
        }
        
        const marker = L.circleMarker([spot.latitude, spot.longitude], {
            radius: radius,
            fillColor: color,
            color: "#ffffff",
            weight: weight,
            opacity: 0.9,
            fillOpacity: 0.8,
            interactive: true
        });
        
        const popupHtml = `
            <div class="p-1.5 font-sans select-none max-w-[180px]">
                <strong class="text-slate-800 dark:text-slate-100 flex items-center gap-1.5 font-bold"><i class="fa-solid fa-circle-dot text-rose-500 animate-pulse"></i> Thermal Hotspot</strong>
                <p class="text-[9px] text-slate-450 dark:text-slate-550 font-medium">Satellite: ${escapeHtml(spot.satellite)} (${escapeHtml(spot.method)})</p>
                <div class="mt-2.5 pt-2 border-t border-slate-200 dark:border-slate-800 flex justify-between items-center">
                    <div>
                        <span class="text-[9px] text-slate-450 dark:text-slate-550 block">Heat Output (FRP)</span>
                        <strong class="text-xs font-mono text-rose-500">${spot.frp.toFixed(1)} MW</strong>
                    </div>
                </div>
            </div>
        `;
        
        marker.bindPopup(popupHtml, { offset: [0, -1] });
        state.hotspotsLayerGroup.addLayer(marker);
    });
}

// Hook up search filtering
searchInput.addEventListener("input", (e) => {
    state.searchQuery = e.target.value.toLowerCase().trim();
    
    state.filteredIncidents = state.incidents.filter(incident => {
        return incident.name.toLowerCase().includes(state.searchQuery) || 
               incident.county.toLowerCase().includes(state.searchQuery);
    });

    renderIncidents();
});

// Listener for Enter key on search input (submits query for geocoding / select alert)
searchInput.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
        const query = searchInput.value.trim();
        if (query) {
            await handleSearchSubmit(query);
        }
    }
});

// Helper: Convert degrees to cardinal direction in JavaScript
function getCardinalDirectionJS(degrees) {
    if (degrees === null || degrees === undefined) return "N/A";
    degrees = degrees % 360;
    const directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
    const idx = Math.round(degrees / 22.5) % 16;
    return directions[idx];
}

// Helper: Construct Turf.js polygon representing the wind-driven risk corridor for a fire
function getWindDrivenCorridor(incident, windSpeed, windDirDeg) {
    const firePoint = turf.point([incident.longitude, incident.latitude]);
    const BASE_BUFFER_MILES = 5;
    
    if (windSpeed && windDirDeg !== undefined && windDirDeg !== null) {
        // Downwind direction is wind direction + 180 degrees
        const downwindDeg = (windDirDeg + 185) % 360; // 180 vector shift
        
        // Shift length: wind_speed * 0.2 miles
        const shiftMiles = windSpeed * 0.2;
        
        // Project the downwind shifted point
        const shiftedPoint = turf.destination(firePoint, shiftMiles, downwindDeg, { units: 'miles' });
        
        // Create a corridor line and buffer it
        const line = turf.lineString([
            [incident.longitude, incident.latitude],
            shiftedPoint.geometry.coordinates
        ]);
        
        return turf.buffer(line, BASE_BUFFER_MILES, { units: 'miles' });
    } else {
        // Simple radial buffer of 5 miles if wind is calm or missing
        return turf.buffer(firePoint, BASE_BUFFER_MILES, { units: 'miles' });
    }
}

// Perform spatial verification loop checks
function verifySpatialSafety(lat, lon) {
    const pt = turf.point([lon, lat]);
    let inFema = false;
    let femaDetails = null;
    let inCorridor = false;
    let threateningFires = [];

    // 1. Check active FEMA Alert zones
    state.alerts.forEach(alert => {
        if (alert.fema_alerts) {
            alert.fema_alerts.forEach(fa => {
                if (fa.rings) {
                    try {
                        const poly = turf.polygon(fa.rings);
                        if (turf.booleanPointInPolygon(pt, poly)) {
                            inFema = true;
                            femaDetails = fa;
                        }
                    } catch (e) {
                        console.error("FEMA polygon check error:", e);
                    }
                }
            });
        }
    });

    // 2. Map fire IDs to their active wind vectors
    const fireWindMap = new Map();
    state.alerts.forEach(alert => {
        if (alert.fires) {
            alert.fires.forEach(f => {
                if (!fireWindMap.has(f.id)) {
                    fireWindMap.set(f.id, {
                        windSpeed: f.wind_speed_mph,
                        windDirDeg: f.wind_direction_deg
                    });
                }
            });
        }
    });

    // 3. Check distance and corridor intersection for all active fires
    state.incidents.forEach(incident => {
        const windData = fireWindMap.get(incident.id) || { windSpeed: 0, windDirDeg: null };
        try {
            const corridor = getWindDrivenCorridor(incident, windData.windSpeed, windData.windDirDeg);
            if (turf.booleanPointInPolygon(pt, corridor)) {
                inCorridor = true;
                const distance = turf.distance(pt, turf.point([incident.longitude, incident.latitude]), { units: 'miles' });
                threateningFires.push({
                    name: incident.name,
                    distance: distance.toFixed(1),
                    windSpeed: windData.windSpeed,
                    windDir: getCardinalDirectionJS(windData.windDirDeg)
                });
            }
        } catch (e) {
            console.error("Risk corridor check error:", e);
        }
    });

    return { inFema, femaDetails, inCorridor, threateningFires };
}

// Display geocode and safety analysis results
function displayGeocodeResult(lat, lon, displayName, safety) {
    let statusClass = "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/20";
    let statusText = "🟢 STATUS: SAFE ZONE";
    let statusDesc = "This location is currently outside active FEMA alert zones and wind-driven wildfire threat corridors.";
    
    if (safety.inFema) {
        statusClass = "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20 animate-pulse";
        statusText = "🔴 CRITICAL ALERT: FEMA ACTIVE ZONE";
        statusDesc = `<strong>Event:</strong> ${escapeHtml(safety.femaDetails.event)}<br/><strong>Instruction:</strong> ${escapeHtml(safety.femaDetails.instruction || safety.femaDetails.description || 'Follow local evacuation orders.')}`;
    } else if (safety.inCorridor) {
        statusClass = "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20";
        statusText = "🟠 WARNING: RISK CORRIDOR";
        let fireList = safety.threateningFires.map(f => `• ${escapeHtml(f.name)} (${escapeHtml(f.distance)} mi away, wind ${escapeHtml(f.windDir)} @ ${escapeHtml(f.windSpeed)} mph)`).join('<br/>');
        statusDesc = `Location is downwind of active fires:<br/>${fireList}`;
    }

    const popupHtml = `
        <div class="p-3 font-sans max-w-[280px]">
            <h4 class="font-bold text-slate-800 dark:text-slate-100 text-sm mb-1">📍 Search Result</h4>
            <p class="text-[10px] text-slate-500 dark:text-slate-400 mb-2 truncate" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</p>
            
            <div class="p-2 border rounded-lg text-xs leading-relaxed ${statusClass}">
                <div class="font-bold mb-1 uppercase tracking-wider text-[10px]">${statusText}</div>
                <div class="text-[11px]">${statusDesc}</div>
            </div>
        </div>
    `;

    // Drop marker on Leaflet map
    const searchMarker = L.marker([lat, lon], {
        icon: L.divIcon({
            className: 'search-result-marker',
            html: '<div class="flex h-5 w-5 items-center justify-center"><span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75"></span><span class="relative inline-flex h-3 w-3 rounded-full bg-blue-500 border border-white"></span></div>',
            iconSize: [20, 20],
            iconAnchor: [10, 10]
        })
    }).addTo(state.map);

    searchMarker.bindPopup(popupHtml, { offset: [0, -2] }).openPopup();
    
    // Auto-remove marker when popup closes
    searchMarker.on('popupclose', () => {
        state.map.removeLayer(searchMarker);
    });
}

// Search selection handler
async function handleSearchSubmit(query) {
    query = query.trim();
    if (!query) return;

    // 1. Check matches in alert city names
    const matchedAlertIndex = state.alerts.findIndex(alert => alert.city.toLowerCase() === query.toLowerCase());
    if (matchedAlertIndex !== -1) {
        selectAlert(matchedAlertIndex);
        return;
    }

    // 2. Check partial matches in alert city names
    const partialAlertIndex = state.alerts.findIndex(alert => alert.city.toLowerCase().includes(query.toLowerCase()));
    if (partialAlertIndex !== -1) {
        selectAlert(partialAlertIndex);
        return;
    }

    // 3. Check exact match in incident names
    const matchedIncident = state.incidents.find(incident => incident.name.toLowerCase() === query.toLowerCase());
    if (matchedIncident) {
        selectIncidentById(matchedIncident.id);
        return;
    }

    // 4. Check partial match in incident names
    const partialIncident = state.incidents.find(incident => incident.name.toLowerCase().includes(query.toLowerCase()));
    if (partialIncident) {
        selectIncidentById(partialIncident.id);
        return;
    }

    // 5. Query OpenStreetMap Nominatim Geocoding API
    try {
        const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=1`;
        const response = await fetch(url);
        if (!response.ok) throw new Error("Geocoding service unavailable");
        
        const data = await response.json();
        if (data && data.length > 0) {
            const lat = parseFloat(data[0].lat);
            const lon = parseFloat(data[0].lon);
            const displayName = data[0].display_name;
            
            // Recenter map
            state.map.flyTo([lat, lon], 13, { duration: 1.5 });
            
            // Run spatial verification check
            const safety = verifySpatialSafety(lat, lon);
            
            // Display safety result popup
            setTimeout(() => {
                displayGeocodeResult(lat, lon, displayName, safety);
            }, 1500);
        } else {
            alert(`No location found for query: "${query}"`);
        }
    } catch (err) {
        console.error("Geocoding failed:", err);
        alert("Failed to connect to geocoding service. Please try again.");
    }
}

// App Initialization entry point
window.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initMap();
    initTooltips();
    initCollapsibles();
    fetchIncidents();
});

// Collapsable Left Sidebar Panels
function initCollapsibles() {
    const toggleAlertsBtn = document.getElementById("toggle-alerts-btn");
    const alertCards = document.getElementById("alert-cards");
    const alertsCaret = document.getElementById("alerts-caret");
    
    if (toggleAlertsBtn && alertCards && alertsCaret) {
        toggleAlertsBtn.addEventListener("click", (e) => {
            // Prevent collapsing if clicking on the help icon trigger
            if (e.target.closest(".help-tooltip-trigger")) return;
            
            alertCards.classList.toggle("hidden");
            alertsCaret.classList.toggle("-rotate-90");
        });
    }
    
    const toggleIncidentsBtn = document.getElementById("toggle-incidents-btn");
    const incidentListContent = document.getElementById("incident-list-content");
    const incidentsCaret = document.getElementById("incidents-caret");
    
    if (toggleIncidentsBtn && incidentListContent && incidentsCaret) {
        toggleIncidentsBtn.addEventListener("click", () => {
            incidentListContent.classList.toggle("hidden");
            incidentsCaret.classList.toggle("-rotate-90");
        });
    }

    // Layout Toggle Buttons
    const toggleLeftBtn = document.getElementById("toggle-left-btn");
    const toggleRightBtn = document.getElementById("toggle-right-btn");
    
    if (toggleLeftBtn) {
        toggleLeftBtn.addEventListener("click", () => {
            state.leftCollapsed = !state.leftCollapsed;
            if (!state.leftCollapsed && window.innerWidth < 1024) {
                state.rightCollapsed = true;
            }
            updateSidebarLayout();
        });
    }
    
    if (toggleRightBtn) {
        toggleRightBtn.addEventListener("click", () => {
            state.rightCollapsed = !state.rightCollapsed;
            if (!state.rightCollapsed && window.innerWidth < 1024) {
                state.leftCollapsed = true;
            }
            updateSidebarLayout();
        });
    }

    // Set initial sidebar states
    updateSidebarLayout();

    // Window resize tracking
    let lastWidth = window.innerWidth;
    window.addEventListener("resize", () => {
        const currentWidth = window.innerWidth;
        if (currentWidth < 1024 && lastWidth >= 1024) {
            state.leftCollapsed = true;
            state.rightCollapsed = true;
            updateSidebarLayout();
        } else if (currentWidth >= 1024 && lastWidth < 1024) {
            state.leftCollapsed = false;
            state.rightCollapsed = false;
            updateSidebarLayout();
        }
        lastWidth = currentWidth;
    });
}

// Update DOM elements and classes based on collapsed states
function updateSidebarLayout() {
    const layout = document.getElementById("app-layout");
    const leftPanel = document.getElementById("left-panel");
    const rightPanel = document.getElementById("right-panel");
    const leftToggleIcon = document.getElementById("left-toggle-icon");
    const rightToggleIcon = document.getElementById("right-toggle-icon");
    
    if (!layout || !leftPanel || !rightPanel) return;

    if (state.leftCollapsed) {
        layout.classList.add("left-collapsed");
        leftPanel.classList.add("collapsed");
        if (leftToggleIcon) leftToggleIcon.className = "fa-solid fa-chevron-right text-sm transition-transform duration-300";
    } else {
        layout.classList.remove("left-collapsed");
        leftPanel.classList.remove("collapsed");
        if (leftToggleIcon) leftToggleIcon.className = "fa-solid fa-chevron-left text-sm transition-transform duration-300";
    }
    
    if (state.rightCollapsed) {
        layout.classList.add("right-collapsed");
        rightPanel.classList.add("collapsed");
        if (rightToggleIcon) rightToggleIcon.className = "fa-solid fa-chevron-left text-sm transition-transform duration-300";
    } else {
        layout.classList.remove("right-collapsed");
        rightPanel.classList.remove("collapsed");
        if (rightToggleIcon) rightToggleIcon.className = "fa-solid fa-chevron-right text-sm transition-transform duration-300";
    }
    
    // Trigger map size invalidate after transition finishes so Leaflet redraws correctly
    setTimeout(() => {
        if (state.map) {
            state.map.invalidateSize();
        }
    }, 300);
}

// Interactive Tooltips System
function initTooltips() {
    let tooltipEl = document.getElementById("global-tooltip-bubble");
    if (!tooltipEl) {
        tooltipEl = document.createElement("div");
        tooltipEl.id = "global-tooltip-bubble";
        tooltipEl.className = "tooltip-bubble";
        document.body.appendChild(tooltipEl);
    }
    
    const tooltipsData = {
        "tooltip-risk-alerts": `
            <div class="space-y-2 font-sans text-xs">
                <h4 class="font-bold text-brand-orange flex items-center gap-1.5 border-b border-slate-200 dark:border-slate-800 pb-1 mb-1.5"><i class="fa-solid fa-circle-exclamation"></i> Ready, Set, Go! Alerts</h4>
                <p class="text-[10px] text-slate-500 dark:text-slate-450 leading-normal">
                    Municipalities are flagged with alert tiers based on spatial intersections with projected wildfire threat zones:
                </p>
                <div class="space-y-1.5 text-[10px] leading-normal mt-2">
                    <div><span class="inline-block px-1 rounded bg-emerald-500/10 text-emerald-500 font-bold mr-1">Level 1: Ready</span> <strong>Be Prepared.</strong> A fire is nearby (within 5 miles) but wind vectors are moving it away. Prepare emergency supplies and make an evacuation plan.</div>
                    <div><span class="inline-block px-1 rounded bg-amber-500/10 text-amber-500 font-bold mr-1">Level 2: Set</span> <strong>Be Alert.</strong> Municipal boundaries intersect the projected path of active fire corridors driven by NWS winds. Evacuate voluntarily or pack bags to leave at a moment's notice.</div>
                    <div><span class="inline-block px-1 rounded bg-red-500/10 text-red-500 font-bold mr-1 animate-pulse">FEMA Active</span> <strong>High Alert.</strong> Official FEMA evacuation or hazard warning polygons overlap this town. Follow all instructions from emergency officials immediately.</div>
                </div>
            </div>
        `,
        "tooltip-weather-telemetry": `
            <div class="space-y-2 font-sans text-xs">
                <h4 class="font-bold text-blue-500 flex items-center gap-1.5 border-b border-slate-200 dark:border-slate-800 pb-1 mb-1.5"><i class="fa-solid fa-wind"></i> Weather & Wildfire Behavior</h4>
                <p class="text-[10px] text-slate-500 dark:text-slate-450 leading-normal">
                    Weather dictates how a fire acts. We track these values relative to active incidents:
                </p>
                <div class="space-y-1.5 text-[10px] leading-normal mt-2">
                    <div><strong>Wind Speed & Heading:</strong> Tells us where and how fast the fire will advance. Higher winds supply oxygen, accelerate ground travel, and loft burning embers to ignite new fires (spotting) miles downwind.</div>
                    <div><strong>Relative Humidity (RH):</strong> Measures moisture in the air. Dry air <strong>(RH &lt; 20%)</strong> dries out grasses and timber, making them highly combustible and significantly increasing rate of spread.</div>
                    <div><strong>Temperature:</strong> Elevated heat increases fuel drying speed and causes fires to burn with greater intensity.</div>
                </div>
            </div>
        `,
        "tooltip-smoke-aqi": `
            <div class="space-y-2 font-sans text-xs">
                <h4 class="font-bold text-amber-500 flex items-center gap-1.5 border-b border-slate-200 dark:border-slate-800 pb-1 mb-1.5"><i class="fa-solid fa-smog"></i> Smoke & Air Quality Info</h4>
                <p class="text-[10px] text-slate-500 dark:text-slate-450 leading-normal">
                    Fine soot particulate matter (PM2.5) in wildfire smoke is dangerous to inhale:
                </p>
                <div class="space-y-1.5 text-[10px] leading-normal mt-2">
                    <div><strong>Light/Medium Smoke:</strong> Hazy air. Fine for most, but may cause minor coughing or irritation for asthma sufferers.</div>
                    <div><strong>Heavy Smoke:</strong> Visible columns reaching ground levels. High risk of severe breathing issues. Avoid outdoor exertion.</div>
                    <div><strong>AQI Scale (EPA):</strong>
                        <ul class="list-disc list-inside space-y-0.5 pl-1">
                            <li><span class="text-emerald-500 font-semibold">0-50 (Good)</span>: Satisfactory air.</li>
                            <li><span class="text-amber-500 font-semibold">51-100 (Moderate)</span>: Acceptable quality.</li>
                            <li><span class="text-orange-500 font-semibold">101-150 (Sensitive)</span>: Health effects for weak lungs.</li>
                            <li><span class="text-red-500 font-semibold">151+ (Unhealthy)</span>: Outdoor restrictions for all.</li>
                        </ul>
                    </div>
                </div>
            </div>
        `,
        "tooltip-cache-freshness": `
            <div class="space-y-2 font-sans text-xs">
                <h4 class="font-bold text-slate-800 dark:text-slate-200 flex items-center gap-1.5 border-b border-slate-200 dark:border-slate-800 pb-1 mb-1.5"><i class="fa-solid fa-arrows-rotate text-emerald-500"></i> Data Freshness Disclosure</h4>
                <p class="text-[10px] text-slate-500 dark:text-slate-450 leading-normal">
                    This tracker aggregates public telemetry from federal and state agencies, which operates on separate cache intervals:
                </p>
                <div class="space-y-1.5 text-[10px] leading-normal mt-1.5 text-slate-650 dark:text-slate-400">
                    <div>• <strong>Wildfire Boundaries & Incident Logs:</strong> Cached locally for 15 minutes to preserve public API bandwidth.</div>
                    <div>• <strong>NWS Weather Stations:</strong> Observations update every 15 to 60 minutes depending on the physical station.</div>
                    <div>• <strong>NOAA Satellite Plumes:</strong> Updated periodically as satellite imaging tracks pass over Utah.</div>
                </div>
                <p class="text-[9px] text-slate-400 dark:text-slate-500 italic mt-1 leading-normal">
                    Because of these varying source schedules, local updates do not pivot minute-by-minute.
                </p>
            </div>
        `
    };
    
    let activeTrigger = null;

    function showTooltip(trigger, tooltipId) {
        const content = tooltipsData[tooltipId];
        if (!content) return;
        
        tooltipEl.innerHTML = content;
        tooltipEl.classList.add("active");
        activeTrigger = trigger;
        
        positionTooltip(trigger);
    }
    
    function hideTooltip() {
        tooltipEl.classList.remove("active");
        activeTrigger = null;
    }

    function positionTooltip(trigger) {
        if (!activeTrigger) return;
        const triggerRect = trigger.getBoundingClientRect();
        const tooltipRect = tooltipEl.getBoundingClientRect();
        
        let top = triggerRect.bottom + window.scrollY + 8;
        let left = triggerRect.left + window.scrollX - (tooltipRect.width / 2) + (triggerRect.width / 2);
        
        if (left < 10) left = 10;
        if (left + tooltipRect.width > window.innerWidth - 10) {
            left = window.innerWidth - tooltipRect.width - 10;
        }
        
        if (top + tooltipRect.height > window.innerHeight + window.scrollY - 10) {
            top = triggerRect.top + window.scrollY - tooltipRect.height - 8;
        }
        
        tooltipEl.style.top = `${top}px`;
        tooltipEl.style.left = `${left}px`;
    }
    
    document.addEventListener("mouseenter", (e) => {
        const trigger = e.target.closest(".help-tooltip-trigger");
        if (trigger) {
            const tooltipId = trigger.getAttribute("data-tooltip");
            showTooltip(trigger, tooltipId);
        }
    }, true);
    
    document.addEventListener("mouseleave", (e) => {
        const trigger = e.target.closest(".help-tooltip-trigger");
        if (trigger) {
            hideTooltip();
        }
    }, true);

    window.addEventListener("scroll", () => {
        if (activeTrigger) positionTooltip(activeTrigger);
    }, true);
    window.addEventListener("resize", () => {
        if (activeTrigger) positionTooltip(activeTrigger);
    });
}
