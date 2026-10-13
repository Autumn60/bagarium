// Leaflet helpers for drawing bag tracks. Requires Leaflet (window.L).
(() => {
  const LAYER_KEY = "bagarium.mapLayer";
  const baseLayers = () => ({
    "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }),
    "地理院 標準": L.tileLayer("https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png", {
      maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">地理院タイル</a>',
    }),
    "地理院 写真": L.tileLayer("https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg", {
      maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">地理院タイル</a>',
    }),
  });
  const COLORS = ["#1f6feb", "#d1242f", "#1a7f37", "#bf8700", "#8250df", "#0598bc", "#cf222e", "#6e7781", "#e16f24", "#116329"];

  function createMap(el) {
    const map = L.map(el, { preferCanvas: true });
    const layers = baseLayers();
    let chosen = "OpenStreetMap";
    try { chosen = localStorage.getItem(LAYER_KEY) || chosen; } catch {}
    (layers[chosen] || layers["OpenStreetMap"]).addTo(map);
    L.control.layers(layers, null, { position: "topright" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    map.on("baselayerchange", e => { try { localStorage.setItem(LAYER_KEY, e.name); } catch {} });
    map.setView([35.68, 139.76], 5);
    return map;
  }

  async function fetchTrack(bagId, topic) {
    const url = `/api/bags/${bagId}/track` + (topic ? `?topic=${encodeURIComponent(topic)}` : "");
    const res = await fetch(url);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    return body;
  }

  function nearestIndex(points, latlng) {
    let best = 0, bestD = Infinity;
    points.forEach((p, i) => {
      const d = (p[0] - latlng.lat) ** 2 + (p[1] - latlng.lng) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  }

  function clock(startNs, offset) {
    if (startNs == null) return "";
    return new Date(startNs / 1e6 + offset * 1000).toLocaleTimeString();
  }

  // Draw a track; `popup(point, track)` returns popup HTML for a clicked point.
  function drawTrack(map, track, { color = COLORS[0], popup } = {}) {
    const group = L.featureGroup();
    if (!track.points.length) return group;
    const latlngs = track.points.map(p => [p[0], p[1]]);
    const line = L.polyline(latlngs, { color, weight: 4, opacity: 0.85 }).addTo(group);
    // a wide invisible line makes clicking easier
    const hit = L.polyline(latlngs, { color, weight: 16, opacity: 0 }).addTo(group);
    const dot = (p, fill, title) => L.circleMarker([p[0], p[1]], {
      radius: 6, color: "#fff", weight: 2, fillColor: fill, fillOpacity: 1,
    }).bindTooltip(title).addTo(group);
    dot(track.points[0], "#1a7f37", "start");
    dot(track.points[track.points.length - 1], "#d1242f", "end");
    if (popup) {
      const onClick = e => {
        const p = track.points[nearestIndex(track.points, e.latlng)];
        L.popup().setLatLng([p[0], p[1]]).setContent(popup(p, track)).openOn(map);
      };
      line.on("click", onClick);
      hit.on("click", onClick);
    }
    group.addTo(map);
    return group;
  }

  window.bagariumMap = { createMap, fetchTrack, drawTrack, clock, COLORS };
})();
