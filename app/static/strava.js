/* Strava-pagina: koppelen, routes kiezen en verwerken */
(function () {
  "use strict";

  const map = L.map("map").setView([52.1, 5.3], 7);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap-bijdragers",
  }).addTo(map);
  const layers = L.layerGroup().addTo(map);

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
  const colors = ["#0284c7", "#7c3aed", "#ea580c", "#16a34a", "#db2777"];

  const el = (id) => document.getElementById(id);
  const statusEl = el("status");

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  async function api(url, options) {
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Fout ${response.status}`);
    return payload;
  }

  async function loadStatus() {
    const s = await api("/api/strava/status");
    const connectBtn = el("connect-btn");
    const disconnectBtn = el("disconnect-btn");
    if (!s.configured) {
      el("connect-state").textContent = "Strava-integratie is niet geconfigureerd.";
      return;
    }
    if (s.connected) {
      const who = s.athlete && s.athlete.firstname
        ? `${s.athlete.firstname} ${s.athlete.lastname || ""}`.trim()
        : "je Strava-account";
      const via = s.mode === "env" ? " (vast token uit de configuratie)" : "";
      const scope = s.scope && s.scope.indexOf("read_all") === -1
        ? " Let op: zonder scope 'read_all' toont Strava alleen openbare routes."
        : "";
      el("connect-state").textContent = `Verbonden met ${who}${via}.${scope}`;
      connectBtn.hidden = true;
      disconnectBtn.hidden = s.mode === "env";
      el("route-box").hidden = false;
      await loadRoutes();
    } else {
      el("connect-state").textContent = s.oauth_available
        ? "Nog niet verbonden met Strava."
        : "Nog niet verbonden. Stel STRAVA_REDIRECT_URI in om te kunnen koppelen, " +
          "of vul STRAVA_REFRESH_TOKEN in om een bestaand token te gebruiken.";
      connectBtn.hidden = !s.oauth_available;
      disconnectBtn.hidden = true;
      el("route-box").hidden = true;
    }
  }

  let allRoutes = [];

  /* Toon alleen routes waarvan de naam de zoekterm bevat; behoudt aanvinkingen. */
  function filterRoutes() {
    const term = el("route-search").value.trim().toLowerCase();
    const items = Array.from(el("routes").querySelectorAll("li[data-name]"));
    let visible = 0;
    items.forEach((li) => {
      const match = !term || li.dataset.name.includes(term);
      li.hidden = !match;
      if (match) visible += 1;
    });
    el("route-count").textContent = term
      ? `(${visible} van ${allRoutes.length})`
      : `(${allRoutes.length})`;
  }

  async function loadRoutes() {
    const list = el("routes");
    list.innerHTML = "<li class='muted'>Routes ophalen…</li>";
    el("route-search").value = "";
    try {
      allRoutes = await api("/api/strava/routes");
      list.innerHTML = "";
      el("route-count").textContent = `(${allRoutes.length})`;
      if (!allRoutes.length) {
        list.innerHTML = "<li class='muted'>Geen routes gevonden in dit Strava-account.</li>";
        return;
      }
      allRoutes.forEach((route) => {
        const li = document.createElement("li");
        li.dataset.name = route.name.toLowerCase();
        const elevation = route.elevation_gain_m === null || route.elevation_gain_m === undefined
          ? ""
          : ` · ↑ ${Math.round(route.elevation_gain_m)} m`;
        li.innerHTML =
          `<label><input type="checkbox" value="${esc(route.id)}">` +
          `<span><strong>${esc(route.name)}</strong>` +
          `<small>${route.distance_km.toFixed(1)} km${elevation}` +
          `${route.private ? " · privé" : ""}</small></span></label>`;
        list.appendChild(li);
      });
    } catch (err) {
      list.innerHTML = "";
      setStatus(err.message, true);
      if (/verbonden|geweigerd/i.test(err.message)) {
        el("route-box").hidden = true;
        el("connect-btn").hidden = false;
        el("connect-state").textContent = "Koppeling verlopen, verbind opnieuw.";
      }
    }
  }

  el("route-search").addEventListener("input", filterRoutes);

  function selectedIds() {
    return Array.from(document.querySelectorAll("#routes input:checked")).map((i) => i.value);
  }

  el("select-all").addEventListener("click", () => {
    const boxes = Array.from(el("routes").querySelectorAll("li:not([hidden]) input"));
    const target = !boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = target; });
  });

  function renderResults(items) {
    layers.clearLayers();
    const list = el("result-list");
    list.innerHTML = "";
    const bounds = [];

    items.forEach((item, index) => {
      const li = document.createElement("li");
      if (item.error) {
        li.innerHTML = `<strong>${esc(item.name)}</strong><small class="error-text">${esc(item.error)}</small>`;
        list.appendChild(li);
        return;
      }
      const r = item.result;
      const color = colors[index % colors.length];
      const line = L.polyline(r.route, { color: color, weight: 4, opacity: 0.85 }).addTo(layers);
      bounds.push(line.getBounds());
      r.water_points.forEach((wp) => {
        L.marker([wp.lat, wp.lon], { icon: waterIcon })
          .bindPopup(`<strong>💧 ${esc(wp.name || "Drinkwaterpunt")}</strong><br>km ${wp.along_route_km.toFixed(1)} · ${Math.round(wp.distance_to_route_m)} m van route`)
          .addTo(layers);
      });
      (r.road_works || []).forEach((w) => {
        const rows = [`<strong>⚠️ ${esc(w.cause || "Wegwerkzaamheden")}</strong>`,
          `km ${w.along_route_km.toFixed(1)} · ${Math.round(w.distance_to_route_m)} m van route`];
        if (w.authority) rows.push(esc(w.authority));
        if (w.start || w.end) rows.push(`Periode: ${esc(w.start || "?")} t/m ${esc(w.end || "?")}`);
        if (w.detour) rows.push(`<em>${esc(w.detour)}</em>`);
        (w.lines || []).forEach((ln) => {
          L.polyline(ln, { color: "#f59e0b", weight: 7, opacity: 0.85, dashArray: "8 6" }).addTo(layers);
        });
        L.marker([w.lat, w.lon], { icon: worksIcon }).bindPopup(rows.join("<br>")).addTo(layers);
      });

      Legality.draw(r, layers);
      const lg = Legality.summary(r);

      li.innerHTML =
        `<strong style="border-left:4px solid ${color};padding-left:.4rem">${esc(item.name)}</strong>` +
        `<small>${r.stats.total_distance_km.toFixed(1)} km · ${r.stats.water_point_count} waterpunten · ` +
        `langste stuk zonder water ${r.stats.longest_gap_km.toFixed(1)} km · bron ${esc(r.source)}</small>` +
        (r.roadworks_checked && !r.roadworks_error
          ? `<small>⚠️ ${(r.road_works || []).length} wegwerkzaamheden op ${esc(r.roadworks_date)}</small>`
          : "") +
        (r.roadworks_error ? `<small class="warn-text">${esc(r.roadworks_error)}</small>` : "") +
        (lg !== null && !r.legality_error
          ? `<small${Legality.counts(r).forbidden ? ' class="warn-text"' : ""}>⛔ Verboden paden: ${esc(lg)}</small>`
          : "") +
        (r.legality_error ? `<small class="warn-text">${esc(r.legality_error)}</small>` : "") +
        (r.stats.warning ? `<small class="warn-text">${esc(r.stats.warning)}</small>` : "") +
        `<span class="dl">` +
        `<a href="/api/download/${item.original_job_id}?name=${encodeURIComponent(item.original_filename)}" download>origineel</a>` +
        `<a class="primary" href="/api/download/${r.job_id}?name=${encodeURIComponent(r.filename)}" download>⬇ ${esc(r.filename)}</a>` +
        `</span>`;
      list.appendChild(li);
    });

    if (bounds.length) {
      map.fitBounds(bounds.reduce((acc, b) => acc.extend(b), bounds[0].pad(0)), { padding: [25, 25] });
    }
    el("results").hidden = false;
  }

  el("disconnect-btn").addEventListener("click", async () => {
    await api("/api/strava/disconnect", { method: "POST" });
    el("results").hidden = true;
    layers.clearLayers();
    setStatus("Koppeling verbroken.");
    loadStatus();
  });

  // Datumkiezer alleen tonen als de controle is aangevinkt.
  const roadworksBox = el("roadworks");
  const dateWrap = el("roadworks-date-wrap");
  if (roadworksBox && dateWrap) {
    const sync = () => { dateWrap.hidden = !roadworksBox.checked; };
    roadworksBox.addEventListener("change", sync);
    sync();
  }

  el("refresh-routes").addEventListener("click", loadRoutes);

  el("process-btn").addEventListener("click", async () => {
    const ids = selectedIds();
    if (!ids.length) {
      setStatus("Selecteer minimaal één route.", true);
      return;
    }
    el("process-btn").disabled = true;
    setStatus(`Bezig met ${ids.length} route(s)… dit kan even duren.`);
    try {
      const items = await api("/api/strava/process", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          route_ids: ids,
          radius: Number(el("radius").value),
          source: el("source").value,
          roadworks: Boolean(roadworksBox && roadworksBox.checked),
          ride_date:
            roadworksBox && roadworksBox.checked && el("ride_date")
              ? el("ride_date").value
              : null,
          legality: Boolean(el("legality") && el("legality").checked),
        }),
      });
      renderResults(items);
      const ok = items.filter((i) => !i.error).length;
      setStatus(`Klaar: ${ok} van ${items.length} route(s) verwerkt.`, ok === 0);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      el("process-btn").disabled = false;
    }
  });

  const params = new URLSearchParams(window.location.search);
  if (params.get("error")) {
    setStatus(`Koppelen mislukt: ${params.get("error")}`, true);
  }
  loadStatus().catch((err) => setStatus(err.message, true));
})();
