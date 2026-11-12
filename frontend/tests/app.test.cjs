// No npm dependencies: run with node --test frontend/tests/*.test.cjs.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const ring = [[-112, 39], [-111, 39], [-111, 40], [-112, 39]];
const fire = (id = 'A', fields = {}) => ({ id, name: `Fire ${id}`, county: 'Utah', acres: 10,
    containment: 20, latitude: 39, longitude: -111, ...fields });
const response = (data, headers = {}, status = 200) => ({ ok: status >= 200 && status < 300, status,
    headers: { get: key => headers[key] ?? null }, json: async () => data });
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
    constructor(id) {
        this.id = id; this.style = {}; this.dataset = {}; this.events = {};
        this.textContent = ''; this.innerHTML = ''; this.value = '';
        const classes = new Set();
        this.classList = { add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)), contains: name => classes.has(name),
            toggle: (name, force) => { const add = force ?? !classes.has(name); add ? classes.add(name) : classes.delete(name); return add; } };
    }
    addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
    emit(name, event = {}) { for (const fn of this.events[name] || []) fn({ target: this, ...event }); }
    querySelectorAll(selector) { if (selector === 'p') return this.paragraphs ||= [new Element(), new Element()]; return []; }
    closest() { return null; }
    contains() { return true; }
    scrollIntoView() {}
    appendChild() {}
}

class Group {
    constructor() { this.layers = []; this.events = {}; }
    addLayer(layer) { this.layers.push(layer); return this; }
    clearLayers() { this.layers = []; }
    eachLayer(fn) { this.layers.forEach(fn); }
    addTo(target) { target.addLayer(this); return this; }
    on(name, fn) { this.events[name] = fn; return this; }
    emit(name, event) { this.events[name]?.(event); }
    bindPopup(content) { this.content = content; return this; }
    openPopup() { this.openCount = (this.openCount || 0) + 1; return this; }
}

function harness({ fetch, storageBlocked = false, leaflet = true, clusters = true } = {}) {
    const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element(match[1])]));
    const document = new Element('document');
    Object.assign(document, { getElementById: id => elements.get(id) || null, createElement: () => new Element(),
        documentElement: new Element(), body: new Element(), querySelectorAll: () => [] });
    const window = new Element('window'); window.innerWidth = 1400;
    const timers = new Map(); let timerId = 0;
    const map = new Group();
    Object.assign(map, { panes: {}, flies: [], setView() { return this; },
        createPane(name) { return this.panes[name] = { style: {} }; }, getPane(name) { return this.panes[name]; },
        flyTo(...args) { this.flies.push(args); }, closePopup() {}, removeLayer(layer) { this.layers = this.layers.filter(item => item !== layer); },
        hasLayer(layer) { return this.layers.includes(layer); }, invalidateSize() {}, remove() {} });
    const control = () => ({ addTo(target) { this._map = target; this.onAdd?.(target); return this; }, remove() { this._map = null; } });
    control.zoom = control.layers = control;
    const point = (coords, options) => {
        assert.ok(coords.every(value => typeof value === 'number' && Number.isFinite(value)), 'Leaflet receives finite numeric coordinates');
        return Object.assign(new Group(), { coords, options });
    };
    const L = { map: () => map, layerGroup: () => new Group(), featureGroup: layers => Object.assign(new Group(), { layers }),
        markerClusterGroup: clusters ? () => new Group() : undefined,
        tileLayer: (url, options) => Object.assign(new Group(), { url, options }),
        control, DomUtil: { create: () => new Element() }, divIcon: options => options,
        marker: point, circleMarker: point, polygon: (rings, options) => Object.assign(new Group(), { rings, options }),
        geoJSON: () => new Group(), popup: () => ({ setLatLng() { return this; }, setContent(content) { this.content = content; return this; }, openOn() { return this; } }) };
    const context = vm.createContext({ document, window, AbortController, URLSearchParams,
        console: { error() {} }, alert: message => context.messages.push(message), messages: [],
        localStorage: { getItem() { if (storageBlocked) throw Error('blocked'); return null; }, setItem() { if (storageBlocked) throw Error('blocked'); } },
        fetch: fetch || (async url => response(url.endsWith('/incidents') ? [fire()] : url.endsWith('/perimeters') ? { type: 'FeatureCollection', features: [] } : [])),
        setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
        clearTimeout: id => timers.delete(id), setInterval: () => 0, ...(leaflet ? { L } : {}) });
    vm.runInContext(source, context);
    const run = code => vm.runInContext(code, context);
    return { context, run, state: run('state'), elements, map, L, timers,
        fireTimers: ms => { for (const [id, timer] of [...timers]) if (timer.ms === ms) { timers.delete(id); timer.fn(); } },
        boot: () => window.emit('DOMContentLoaded') };
}

function feeds(overrides = {}) {
    return async url => {
        const key = url.split('/').pop();
        if (key in overrides) return typeof overrides[key] === 'function' ? overrides[key]() : overrides[key];
        return response(key === 'incidents' ? [fire()] : key === 'perimeters' ? { type: 'FeatureCollection', features: [] } : [],
            { 'X-Data-Timestamp': new Date().toISOString() });
    };
}

test('independent network/HTTP/JSON/shape failures retain last good data and recover', async () => {
    const h = harness({ fetch: feeds() }); h.run('initMap()');
    await h.run('fetchIncidents()');
    const initialCards = h.elements.get('incident-cards').innerHTML;
    h.context.fetch = feeds({ smoke: () => { throw Error('network down'); }, aqi: response({}, {}, 503),
        hotspots: { ...response([]), json: async () => { throw Error('broken JSON'); } } });
    await h.run('fetchIncidents()');
    assert.equal(h.state.incidents.length, 1);
    assert.equal(h.elements.get('incident-cards').innerHTML, initialCards);
    assert.match(h.elements.get('cache-indicator').textContent, /3 feed/);
    assert.ok(h.state.feeds.get('smoke').error);
    for (const invalid of [response({}, {}, 502), response({ error: 'bad shape' }),
        { ...response([]), json: async () => { throw Error('bad JSON'); } }]) {
        h.context.fetch = feeds({ incidents: invalid });
        await h.run('fetchIncidents()');
        assert.equal(h.state.incidents[0].id, 'A');
        assert.equal(h.elements.get('incident-cards').innerHTML, initialCards);
        assert.match(h.elements.get('feed-status').textContent, /Wildfire incidents: showing stale/);
    }
    h.context.fetch = feeds(); await h.run('fetchIncidents()');
    assert.ok(!h.state.feeds.get('incidents').error);
    assert.ok(h.elements.get('feed-status').classList.contains('hidden'));
});

test('incidents render before a slow overlay and keep the current search on refresh', async () => {
    const slow = deferred();
    const h = harness({ fetch: feeds({ alerts: () => slow.promise }) }); h.run('initMap()');
    h.state.searchQuery = 'no match';
    const loading = h.run('fetchIncidents()'); await flush();
    assert.equal(h.state.incidents.length, 1);
    assert.equal(h.state.filteredIncidents.length, 0);
    assert.ok(h.elements.get('sidebar-loading').classList.contains('hidden'));
    slow.resolve(response([])); await loading;
});

test('failed first load followed by valid empty data restores the normal empty message', async () => {
    const h = harness({ fetch: feeds({ incidents: response({}, {}, 503) }) });
    await h.run('fetchIncidents()');
    assert.equal(h.elements.get('sidebar-empty').paragraphs[0].textContent, 'Wildfire data unavailable');
    h.context.fetch = feeds({ incidents: response([]) }); await h.run('fetchIncidents()');
    assert.equal(h.elements.get('sidebar-empty').paragraphs[0].textContent, 'No active incidents found');
});

test('stale headers on auxiliary feeds are disclosed even without a timestamp', async () => {
    const h = harness({ fetch: feeds({ smoke: response([], { 'X-Data-Stale': 'true' }),
        incidents: response([fire()], { 'X-Data-Timestamp': '<img src=x onerror=oops>' }) }) });
    await h.run('fetchIncidents()');
    assert.ok(h.state.feeds.get('smoke').stale);
    assert.match(h.elements.get('feed-status').textContent, /Smoke plumes: showing stale/);
    assert.match(h.elements.get('cache-indicator').title, /server snapshot is stale/);
    assert.equal(h.elements.get('cache-indicator').innerHTML, '');
    assert.equal(h.run('safetyDataCurrent()'), false);
});

test('malformed nested alerts render without crashes; failed refresh retains alerts and valid empty clears selection', async () => {
    const h = harness({ fetch: feeds({ alerts: response([{ city: null, centroid: [39, -111], risk_level: '2',
        fires: null, fema_alerts: [null, { event: 'Warning', rings: [] }] }]) }) });
    h.run('initMap()'); await h.run('fetchIncidents()');
    assert.match(h.elements.get('alert-cards').innerHTML, /Level 2: Set/);
    assert.ok(h.state.feeds.get('alerts').degraded);
    h.context.fetch = feeds({ alerts: () => { throw Error('offline'); } }); await h.run('fetchIncidents()');
    assert.equal(h.state.alerts.length, 1);
    h.run('selectAlert(0)');
    h.context.fetch = feeds({ alerts: response([]) }); await h.run('fetchIncidents()');
    assert.equal(h.state.alerts.length, 0); assert.equal(h.state.activeAlert, null);
    let popups = 0; h.L.popup = () => { popups++; throw Error('obsolete alert'); };
    h.fireTimers(1400); assert.equal(popups, 0);
});

test('late overlapping feed refresh cannot replace a newer snapshot', async () => {
    const old = deferred();
    const h = harness({ fetch: feeds({ incidents: () => old.promise }) });
    const first = h.run('fetchIncidents()'); await flush();
    h.context.fetch = feeds({ incidents: response([fire('B')]) }); await h.run('fetchIncidents()');
    old.resolve(response([fire('A')])); await first;
    assert.equal(h.state.incidents[0].id, 'B');
});

test('weather A/B race, stale failures and deselection cannot overwrite current telemetry', async () => {
    const h = harness(); h.run('initMap()');
    h.state.incidents = [fire('A'), fire('B')];
    const a = deferred(), b = deferred(); let calls = 0; let firstSignal;
    h.context.fetch = (url, options) => { calls++; if (calls === 1) firstSignal = options.signal; return calls === 1 ? a.promise : b.promise; };
    const first = h.run("selectIncidentById('A')");
    const second = h.run("selectIncidentById('B')");
    assert.ok(firstSignal.aborted);
    b.resolve(response({ station_id: 'B', wind_speed_mph: 7 })); await second;
    a.resolve(response({ station_id: 'A', wind_speed_mph: 99 })); await first;
    assert.equal(h.elements.get('tel-station-id').textContent, 'B');
    assert.equal(h.elements.get('tel-wind-speed').textContent, 7);
    const lateError = deferred(); h.context.fetch = () => lateError.promise;
    const pending = h.run("selectIncidentById('A')");
    h.run('deselectIncident()'); lateError.reject(Error('late failure')); await pending;
    assert.ok(h.elements.get('telemetry-error').classList.contains('hidden'));
    assert.ok(!h.elements.get('telemetry-empty').classList.contains('hidden'));
    assert.equal(h.elements.get('telemetry-retry-btn').onclick, null);
});

test('same-incident retry races use the latest request, and HTTP failures are retryable', async () => {
    const h = harness(); h.state.incidents = [fire()]; h.state.activeIncidentId = 'A';
    h.context.fetch = async () => response({}, {}, 503);
    await h.run('fetchWeatherTelemetry(state.incidents[0])');
    assert.match(h.elements.get('telemetry-error-msg').textContent, /503/);
    assert.equal(typeof h.elements.get('telemetry-retry-btn').onclick, 'function');
    const old = deferred(); h.context.fetch = () => old.promise;
    const first = h.run('fetchWeatherTelemetry(state.incidents[0])');
    h.context.fetch = async () => response({ station_id: 'new' });
    await h.run('fetchWeatherTelemetry(state.incidents[0])');
    old.reject(Error('late failure')); await first;
    assert.equal(h.elements.get('tel-station-id').textContent, 'new');
    assert.ok(h.elements.get('telemetry-error').classList.contains('hidden'));
});

test('request deadline aborts stalled network and stalled JSON body', async () => {
    for (const bodyStalls of [false, true]) {
        const h = harness({ fetch: (url, { signal }) => {
            const stalled = new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Error('aborted'))));
            return bodyStalls ? Promise.resolve({ ...response({}), json: () => stalled }) : stalled;
        } });
        const request = h.run("requestJson('/api/incidents', { timeoutMs: 5 })");
        const rejected = assert.rejects(request, /timed out/);
        await flush(); h.fireTimers(5); await rejected;
        assert.equal(h.timers.size, 0);
    }
});

test('invalid fields, numeric strings and hostile IDs do not corrupt HTML, stats or markers', async () => {
    const malicious = `bad'\"][onclick='oops']<img src=x>`;
    const h = harness({ fetch: feeds({ incidents: response([fire(malicious, { acres: '12.5', containment: '50', latitude: '39', longitude: '-111' }),
        fire('invalid', { name: null, county: {}, latitude: null, acres: 'NaN', containment: 150 }), null]) }) });
    h.run('initMap()'); await h.run('fetchIncidents()');
    assert.equal(h.state.markersMap.size, 1);
    assert.equal(h.elements.get('stat-total-acres').textContent, 13);
    assert.equal(h.elements.get('stat-avg-contain').textContent, '50%');
    assert.ok(h.state.feeds.get('incidents').degraded);
    const cards = h.elements.get('incident-cards').innerHTML;
    assert.ok(!/\n\s+onclick=/.test(cards)); assert.ok(!cards.includes('<img'));
    assert.ok(cards.includes('id="card-bad&#039;&quot;]'));
    await h.run("selectIncidentById('invalid')");
    assert.match(h.elements.get('telemetry-error-msg').textContent, /coordinates/);
    h.run('filterIncidents()');
});

test('missing weather values stay unknown; zero values remain valid; invalid humidity never sets CSS', () => {
    const h = harness(); h.context.weather = { wind_speed_mph: 0, wind_direction_deg: '0', temperature_f: 0, relative_humidity: '999', timestamp: 'bad' };
    h.run('renderWeatherTelemetry(weather)');
    assert.equal(h.elements.get('tel-wind-speed').textContent, 0);
    assert.equal(h.elements.get('tel-temp').textContent, '0°');
    assert.equal(h.elements.get('tel-wind-speed-metric').textContent, 'N/A');
    assert.equal(h.elements.get('wind-compass-needle').style.transform, 'rotate(180deg)');
    assert.equal(h.elements.get('tel-humidity-bar').style.width, '0%');
    assert.equal(h.elements.get('tel-obs-time').textContent, 'Unknown Time');
    h.run('renderWeatherTelemetry({})');
    assert.equal(h.elements.get('tel-wind-dir-deg').textContent, '(N/A)');
    assert.equal(h.elements.get('tel-temp').textContent, '--°');
});

test('malformed overlay records are isolated, null/string FRP works and failed replacement retains polygons', async () => {
    const h = harness({ fetch: feeds({ smoke: response([{ density: 'Heavy', rings: [ring] }, { rings: [] }]),
        hotspots: response([{ latitude: 39, longitude: -111, frp: null }, { latitude: '40', longitude: '-112', frp: '12.5' }, { latitude: 200 }]),
        aqi: response([{ latitude: 39, longitude: -111, color: '"><img src=x>' }]) }) });
    h.run('initMap()'); await h.run('fetchIncidents()');
    assert.equal(h.state.smokeLayerGroup.layers.length, 1);
    assert.equal(h.state.hotspotsLayerGroup.layers.length, 2);
    assert.match(h.state.hotspotsLayerGroup.layers[0].content, /N\/A MW/);
    assert.match(h.state.hotspotsLayerGroup.layers[1].content, /12.5 MW/);
    assert.ok(!h.state.aqiLayerGroup.layers[0].content.includes('<img'));
    const prior = h.state.smokeLayerGroup.layers[0];
    h.L.polygon = () => { throw Error('render failed'); };
    h.context.plumes = [{ rings: [ring] }];
    assert.throws(() => h.run('renderSmokePlumes(plumes)'), /render failed/);
    assert.equal(h.state.smokeLayerGroup.layers[0], prior);
    h.context.fetch = feeds({ smoke: response([{ rings: [] }]) }); await h.run('fetchIncidents()');
    assert.equal(h.state.smokeLayerGroup.layers[0], prior);
});

test('GeoJSON rejects malformed geometry and accepts valid Polygon/MultiPolygon', () => {
    const h = harness(); h.context.collection = { type: 'FeatureCollection', features: [
        { type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] } },
        { type: 'Feature', geometry: { type: 'MultiPolygon', coordinates: [[ring]] } },
        { type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[NaN, 39]]] } }] };
    const normalized = h.run('normalizePerimeters(collection)');
    assert.equal(normalized.data.features.length, 2); assert.ok(normalized.degraded);
});

test('blocked storage and missing Leaflet do not stop incidents or weather selection', async () => {
    const h = harness({ storageBlocked: true, leaflet: false }); h.boot(); await flush();
    assert.equal(h.state.incidents.length, 1); assert.equal(h.state.map, null);
    assert.match(h.elements.get('map-status').textContent, /Map unavailable/);
    h.elements.get('theme-toggle-btn').emit('click');
    h.context.fetch = async () => response({ station_id: 'available' });
    await h.run("selectIncidentById('A')");
    assert.equal(h.elements.get('tel-station-id').textContent, 'available');
});

test('missing cluster plugin falls back to plain markers and tile errors are disclosed', async () => {
    const h = harness({ clusters: false }); h.run('initMap()'); await h.run('fetchIncidents()');
    assert.equal(h.state.markersMap.size, 1);
    h.state.tileLayer.emit('tileerror');
    assert.match(h.elements.get('map-status').textContent, /tiles unavailable/);
    h.state.tileLayer.emit('loading'); h.state.tileLayer.emit('load');
    assert.ok(h.elements.get('map-status').classList.contains('hidden'));
    h.map.emit('mousemove', { latlng: { lat: 39, lng: -111 } });
    assert.equal(h.elements.get('map-coordinates').textContent, '39.0000° N, 111.0000° W');
});

test('explicit map configuration selects Carto; disabled, missing and nonboolean flags use cached standard OSM in both UI themes', () => {
    for (const config of [{ cartoEnabled: true }, { cartoEnabled: false }, undefined, { cartoEnabled: 'true' }, { cartoEnabled: 1 }]) {
        const h = harness(); h.state.map = h.map;
        if (config !== undefined) h.context.window.MAP_CONFIG = config;
        h.run('initTheme(); initMapActions(); updateMapTiles()');
        const carto = config?.cartoEnabled === true;
        for (const theme of ['light', 'dark']) {
            assert.equal(h.state.theme, theme);
            assert.equal(h.state.tileLayer.url, carto ? `/tiles/${theme === 'dark' ? 'dark_all' : 'light_all'}/{s}/{z}/{x}/{y}{r}.png` : 'https://tile.openstreetmap.org/{z}/{x}/{y}.png');
            assert.equal(h.state.tileLayer.options.maxZoom, carto ? 20 : 19);
            assert.match(h.state.tileLayer.options.attribution, /OpenStreetMap/);
            assert.equal(h.state.tileLayer.options.attribution.includes('CARTO'), carto);
            if (!carto) {
                assert.equal(h.state.tileLayer.options.referrerPolicy, 'strict-origin-when-cross-origin');
                assert.ok(!h.state.tileLayer.url.includes('?'), 'stable URL allows ordinary browser caching');
            }
            if (theme === 'light') {
                const previousLayer = h.state.tileLayer;
                h.elements.get('theme-toggle-btn').emit('click');
                assert.ok(h.context.document.documentElement.classList.contains('dark'));
                assert.ok(!h.map.layers.includes(previousLayer));
            }
        }
    }
    const scripts = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map(match => match[1]);
    assert.ok(scripts.indexOf('/map-config.js') >= 0);
    assert.ok(scripts.indexOf('/map-config.js') < scripts.indexOf('app.js'));
});

test('delayed incident and alert popups do not reopen after reset or a newer selection', async () => {
    const h = harness(); h.run('initMap()'); h.state.incidents = [fire()]; h.run('populateMapMarkers()');
    h.context.fetch = async () => response({ station_id: 'A' });
    await h.run("selectIncidentById('A')"); const marker = h.state.markersMap.get('A');
    h.run('deselectIncident()'); h.fireTimers(1400); assert.ok(!marker.openCount);
    h.state.alerts = [{ centroid: [39, -111], fires: [], fema_alerts: [] }];
    let popups = 0; h.L.popup = () => { popups++; throw Error('should not open'); };
    h.run('selectAlert(0)'); h.run('deselectIncident()'); h.fireTimers(1400); assert.equal(popups, 0);
});

test('unknown, stale, failed or aged safety feeds never assert a safe location', () => {
    const h = harness(); h.run('initMap()');
    const now = new Date().toISOString();
    for (const feed of [undefined, { loaded: true }, { loaded: true, stale: true, timestamp: now },
        { loaded: true, error: 'network', timestamp: now }, { loaded: true, degraded: true, timestamp: now },
        { loaded: true, timestamp: '2000-01-01' }]) {
        h.state.feeds.set('incidents', feed); h.state.feeds.set('alerts', feed);
        h.run("displayGeocodeResult(39, -111, 'Utah', { complete: true, inFema: false, inCorridor: false })");
        assert.match(h.map.layers.at(-1).content, /LOCATION RISK UNKNOWN/);
        assert.ok(!h.map.layers.at(-1).content.includes('SAFE ZONE'));
    }
    h.state.feeds.set('incidents', { loaded: true, timestamp: now }); h.state.feeds.set('alerts', { loaded: true, timestamp: now });
    h.run("displayGeocodeResult(39, -111, 'Utah', { complete: true })");
    assert.match(h.map.layers.at(-1).content, /does not establish safety/);
});

test('corridor uses exactly 180 degrees downwind with numeric strings', () => {
    const h = harness(); let bearing;
    h.context.turf = { point: coords => coords, destination: (point, miles, degrees) => { bearing = degrees; return { geometry: { coordinates: [-111, 40] } }; },
        lineString: coords => coords, buffer: shape => shape };
    h.context.incident = fire(); h.run("getWindDrivenCorridor(incident, '10', '0')"); assert.equal(bearing, 180);
    assert.equal(h.run('getCardinalDirectionJS(-90)'), 'W');
});

test('fresh feeds with omitted per-fire wind remain incomplete while retaining known radial matches', () => {
    const h = harness(); h.run('initMap()');
    const timestamp = new Date().toISOString();
    for (const key of ['incidents', 'alerts']) h.state.feeds.set(key, { loaded: true, timestamp });
    h.state.incidents = [fire()];
    let intersects = false;
    h.context.turf = { point: coords => coords, buffer: point => point,
        booleanPointInPolygon: () => intersects, distance: () => 1 };

    // A fire can be absent from all municipal alerts despite having real wind.
    let result = h.run('verifySpatialSafety(39, -111)');
    assert.equal(result.complete, false);
    assert.equal(result.inCorridor, false);
    h.context.safety = result;
    h.run("displayGeocodeResult(39, -111, 'Utah', safety)");
    assert.match(h.map.layers.at(-1).content, /LOCATION RISK UNKNOWN/);

    intersects = true;
    result = h.run('verifySpatialSafety(39, -111)');
    assert.equal(result.complete, false);
    assert.equal(result.inCorridor, true);
    assert.equal(result.threateningFires[0].name, 'Fire A');
    assert.equal(result.threateningFires[0].windSpeed, 'N/A');
    h.context.safety = result;
    h.run("displayGeocodeResult(39, -111, 'Utah', safety)");
    assert.match(h.map.layers.at(-1).content, /modeled fire threat area/);
    assert.match(h.map.layers.at(-1).content, /Data is stale or incomplete/);

    // Known calm wind supports the radial model; missing heading in wind does not.
    h.state.alerts = [{ fires: [{ id: 'A', wind_speed_mph: 0, wind_direction_deg: null }], fema_alerts: [] }];
    assert.equal(h.run('verifySpatialSafety(39, -111)').complete, true);
    h.state.alerts[0].fires[0].wind_speed_mph = 10;
    assert.equal(h.run('verifySpatialSafety(39, -111)').complete, false);
});

test('source directory matches configured service endpoints and distinguishes OpenAQ from the EPA AQI scale', () => {
    const sources = fs.readFileSync(path.join(__dirname, '../sources.html'), 'utf8');
    const backend = fs.readFileSync(path.join(__dirname, '../../backend/app/main.py'), 'utf8');
    for (const key of ['NIFC_API_URL', 'PERIMETERS_URL', 'SMOKE_URL', 'SMOKE_FALLBACK_URL', 'AQI_URL', 'CAP_URL', 'HOTSPOTS_URL']) {
        const match = backend.match(new RegExp(`^${key} = "([^"]+)"`, 'm'));
        assert.ok(match, `backend config for ${key}`);
        assert.ok(sources.includes(`href="${match[1]}"`), `source link for ${key}`);
    }
    assert.match(sources, /OpenAQ PM2.5 Monitor Feed/);
    assert.match(sources, /EPA 2024 PM2.5 breakpoints/);
    assert.ok(!sources.includes('US EPA AirNow Monitor Feed'));
    assert.ok(!sources.includes('FEMA IPAWS CAP Warnings'));
});

test('Utah geocoding rejects invalid/out-of-area coordinates and stale responses', async () => {
    const pending = deferred(); const h = harness({ fetch: () => pending.promise }); h.run('initMap()');
    const search = h.run("handleSearchSubmit('Utah address')"); h.run('deselectIncident()');
    pending.resolve(response([{ lat: '39', lon: '-111', display_name: 'Utah' }])); await search;
    assert.equal(h.map.flies.length, 0);
    for (const coords of [{ lat: 'NaN', lon: '-111' }, { lat: '51', lon: '-1' }]) {
        h.context.fetch = async url => { assert.match(url, /bounded=1/); return response([coords]); };
        await h.run("handleSearchSubmit('unmatched address')");
    }
    assert.equal(h.map.flies.length, 0);
});

test('FEMA spatial safety uses even-odd ring fill for disjoint shells, holes and nested islands', () => {
    const h = harness(); h.run('initMap()');
    const timestamp = new Date().toISOString();
    for (const key of ['incidents', 'alerts']) h.state.feeds.set(key, { loaded: true, timestamp });
    h.state.incidents = [];
    const box = (w, s, e, n) => [[w, s], [e, s], [e, n], [w, n], [w, s]];
    const shellA = box(-112.0, 39.0, -111.5, 39.5);
    const shellB = box(-111.0, 39.0, -110.0, 40.0);
    const holeB = box(-110.8, 39.2, -110.2, 39.8);
    const islandB = box(-110.6, 39.4, -110.4, 39.6);
    h.context.turf = {
        point: coords => coords,
        polygon: rings => rings[0],
        booleanPointInPolygon: ([lon, lat], ringCoords) => {
            const lons = ringCoords.map(c => c[0]), lats = ringCoords.map(c => c[1]);
            return lon >= Math.min(...lons) && lon <= Math.max(...lons) &&
                   lat >= Math.min(...lats) && lat <= Math.max(...lats);
        }
    };
    h.state.alerts = [{
        city: 'Multipart City', centroid: [39.5, -110.5], risk_level: 2, fires: [],
        fema_alerts: [{ event: 'Evacuation Warning', rings: [shellA, shellB, holeB, islandB] }]
    }];
    h.run('drawFemaPolygon(state.alerts[0])');
    assert.equal(h.state.activeFemaLayer.layers[0].options.fillRule, 'evenodd');

    // Second disjoint shell is matched (previously treated as a hole of shellA).
    let res = h.run('verifySpatialSafety(39.1, -110.9)');
    assert.equal(res.inFema, true); assert.equal(res.complete, true);
    // Inside holeB -> excluded (2 containing rings = even).
    res = h.run('verifySpatialSafety(39.3, -110.7)');
    assert.equal(res.inFema, false); assert.equal(res.complete, true);
    // Inside islandB -> included (3 containing rings = odd).
    res = h.run('verifySpatialSafety(39.5, -110.5)');
    assert.equal(res.inFema, true); assert.equal(res.complete, true);

    // Invalid rings mark analysis incomplete instead of silently passing.
    h.state.alerts[0].fema_alerts.push({ event: 'Broken', rings: [[[0, 0], [1, 1]]] });
    res = h.run('verifySpatialSafety(38.0, -113.0)');
    assert.equal(res.inFema, false); assert.equal(res.complete, false);
});
