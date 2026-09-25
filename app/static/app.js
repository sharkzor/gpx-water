/* Frontend voor GPX Drinkwaterpunten */
(function () {
  "use strict";

  const map = L.map("map").setView([52.1, 5.3], 7);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap-bijdragers",
  }).addTo(map);

  const layers = L.layerGroup().addTo(map);
  const form = document.getElementById("form");
  const statusEl = document.getElementById("status");
  const submitBtn = document.getElementById("submit");
  const results = document.getElementById("results");

  const waterIcon = L.divIcon({
    className: "water-marker",
    html: '<div style="font-size:22px;line-height:22px;text-shadow:0 0 3px #fff">💧</div>',
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });

  const worksIcon = L.divIcon({
    className: "works-marker",
    html: '<div style="font-size:22px;line-height:22px;text-shadow:0 0 3px #fff">⚠️</div>',
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });

  Checks.init(submitBtn);

  function setStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /* Extra vangnet naast de server: alleen http(s)-links klikbaar maken. */
  function safeUrl(value) {
    try {
      return Boolean(value) && ["http:", "https:"].includes(new URL(value).protocol);
    } catch (err) {
      return false;
    }
  }

  function popupHtml(wp) {
    const rows = [`<strong>💧 ${esc(wp.name || "Drinkwaterpunt")}</strong>`];
    rows.push(`km ${wp.along_route_km.toFixed(1)} · ${Math.round(wp.distance_to_route_m)} m van route`);
    if (wp.operator) rows.push(`Beheerder: ${esc(wp.operator)}`);
    if (wp.opening_hours) rows.push(`Open: ${esc(wp.opening_hours)}`);
    if (safeUrl(wp.website)) rows.push(`<a href="${esc(wp.website)}" target="_blank" rel="noopener noreferrer">website</a>`);
    rows.push(`<em>${esc(wp.source)}</em>`);
    return rows.join("<br>");
  }

  function worksPopup(w) {
    const rows = [`<strong>⚠️ ${esc(w.cause || "Wegwerkzaamheden")}</strong>`];
    rows.push(`km ${w.along_route_km.toFixed(1)} · ${Math.round(w.distance_to_route_m)} m van route`);
    if (w.authority) rows.push(esc(w.authority));
    if (w.start || w.end) rows.push(`Periode: ${esc(w.start || "?")} t/m ${esc(w.end || "?")}`);
    if (w.detour) rows.push(`<em>${esc(w.detour)}</em>`);
    return rows.join("<br>");
  }

  function renderRoadWorks(data) {
    const works = data.road_works || [];
    const wrap = document.getElementById("rw-list-wrap");
    works.forEach((w) => {
      // Het afgesloten stuk zelf, zodat je ziet of het echt op jouw route ligt.
      (w.lines || []).forEach((ln) => {
        L.polyline(ln, { color: "#f59e0b", weight: 7, opacity: 0.85, dashArray: "8 6" }).addTo(layers);
      });
      L.marker([w.lat, w.lon], { icon: worksIcon }).bindPopup(worksPopup(w)).addTo(layers);
    });

    const list = document.getElementById("rw-list");
    if (!list) return;
    list.innerHTML = "";
    works.forEach((w) => {
      const li = document.createElement("li");
      li.innerHTML = `km ${w.along_route_km.toFixed(1)} – ${esc(w.cause || "Wegwerkzaamheden")}` +
        `<small>${esc(w.authority || "")} · ${esc(w.start || "?")} t/m ${esc(w.end || "?")}</small>`;
      li.style.cursor = "pointer";
      li.addEventListener("click", () => map.setView([w.lat, w.lon], 16));
      list.appendChild(li);
    });
    if (wrap) wrap.hidden = works.length === 0;
  }

  function renderLegality(data) {
    const wrap = document.getElementById("lg-list-wrap");
    Legality.draw(data, layers);
    Legality.fillList(document.getElementById("lg-list"), data, map);
    if (wrap) wrap.hidden = !(data.legality_segments || []).length;
  }

  function renderWeather(data) {
    const box = document.getElementById("weather-result");
    if (box) box.innerHTML = Weather.panel(data);
    Weather.draw(data, layers);
  }

  function render(data) {
    layers.clearLayers();

    const line = L.polyline(data.route, { color: "#0284c7", weight: 4, opacity: 0.85 });
    line.addTo(layers);
    L.circleMarker(data.route[0], { radius: 6, color: "#16a34a", fillOpacity: 1 })
      .bindPopup("Start").addTo(layers);
    L.circleMarker(data.route[data.route.length - 1], { radius: 6, color: "#dc2626", fillOpacity: 1 })
      .bindPopup("Finish").addTo(layers);

    data.water_points.forEach((wp) => {
      L.marker([wp.lat, wp.lon], { icon: waterIcon }).bindPopup(popupHtml(wp)).addTo(layers);
    });
    map.fitBounds(line.getBounds(), { padding: [25, 25] });

    const s = data.stats;
    document.getElementById("check-cards").innerHTML = Checks.cards(data);
    document.querySelectorAll(".water-stat").forEach((node) => { node.hidden = !data.water_checked; });
    document.getElementById("stat-distance").textContent = `${s.total_distance_km.toFixed(1)} km`;
    document.getElementById("stat-count").textContent = s.water_point_count;
    document.getElementById("stat-avg").textContent =
      s.average_gap_km === null ? "–" : `${s.average_gap_km.toFixed(1)} km`;
    document.getElementById("stat-gap").textContent =
      s.longest_gap_km === null ? "–" : `${s.longest_gap_km.toFixed(1)} km`;
    document.getElementById("stat-source").textContent = data.source;

    const dl = document.getElementById("download");
    dl.href = `/api/download/${data.job_id}?name=${encodeURIComponent(data.filename)}`;
    dl.setAttribute("download", data.filename);
    dl.hidden = false;

    const list = document.getElementById("list");
    list.innerHTML = "";
    data.water_points.forEach((wp) => {
      const li = document.createElement("li");
      li.innerHTML = `km ${wp.along_route_km.toFixed(1)} – ${esc(wp.name || "Drinkwaterpunt")}` +
        `<small>${Math.round(wp.distance_to_route_m)} m van route</small>`;
      li.style.cursor = "pointer";
      li.addEventListener("click", () => map.setView([wp.lat, wp.lon], 16));
      list.appendChild(li);
    });
    document.getElementById("list-wrap").hidden = data.water_points.length === 0;
    renderRoadWorks(data);
    renderLegality(data);
    renderWeather(data);
    results.hidden = false;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fileInput = document.getElementById("file");
    if (!fileInput.files.length) {
      setStatus("Kies eerst een GPX-bestand.", true);
      return;
    }
    if (!Checks.anySelected()) {
      setStatus("Kies minimaal één controle.", true);
      return;
    }
    const body = new FormData();
    body.append("file", fileInput.files[0]);
    Checks.appendTo(body);

    submitBtn.disabled = true;
    setStatus(Checks.busyText());
    try {
      const response = await fetch("/api/process", { method: "POST", body });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Verwerking mislukt");
      render(payload);
      setStatus("");
      results.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
