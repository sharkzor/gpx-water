/* Gecombineerd routeoverzicht: hoogteprofiel met waterpunten, werkzaamheden,
   verboden paden en regen op hun kilometerpositie. */
const Overview = (function () {
  "use strict";

  const W = 360, H = 180, PAD_L = 34, PAD_R = 8, PAD_T = 28, PAD_B = 24;
  const IMPACT = {
    closed: { label: "Afgesloten voor fietsers", color: "#dc2626" },
    possible: { label: "Mogelijk afgesloten (alle verkeer)", color: "#f59e0b" },
    hinder: { label: "Hinder of omleiding voor fietsers", color: "#f59e0b" },
  };

  function esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  const num = (v, d) => Number(v).toFixed(d === undefined ? 1 : d).replace(".", ",");

  /* "84, 89 en 107"; bij veel meldingen een afgekapte opsomming */
  function kmList(values) {
    const items = values.map((v) => Math.round(v));
    const unique = items.filter((v, i) => items.indexOf(v) === i);
    const shown = unique.slice(0, 6).map(String);
    const extra = unique.length - shown.length;
    if (extra > 0) return `${shown.join(", ")} (+${extra})`;
    if (shown.length > 1) return `${shown.slice(0, -1).join(", ")} en ${shown[shown.length - 1]}`;
    return shown[0] || "";
  }

  function chips(r) {
    const out = [];
    if (r.water_checked) {
      const n = r.water_points.length;
      out.push(["water", "💧", `${n} waterpunt${n === 1 ? "" : "en"}`]);
    }
    if (r.roadworks_checked && !r.roadworks_error) {
      const works = r.road_works || [];
      const closed = works.filter((w) => w.impact === "closed");
      if (!works.length) out.push(["ok", "⚠️", "Geen werkzaamheden"]);
      else {
        const prefix = closed.length === works.length ? "Werkzaamheden" : "Mogelijk werkzaamheden";
        out.push(["warn", "⚠️", `${prefix} bij km ${kmList(works.map((w) => w.along_route_km))}`]);
      }
    }
    if (r.legality_checked && !r.legality_error) {
      const segs = r.legality_segments || [];
      if (!segs.length) out.push(["ok", "🚫", "Geen verboden paden"]);
      else {
        const metres = segs.reduce((sum, s) => sum + s.length_m, 0);
        const length = metres >= 1000 ? `${num(metres / 1000)} km` : `${Math.round(metres)} m`;
        out.push(["warn", "🚫", `${segs.length} stuk${segs.length === 1 ? "" : "ken"} (${length}) bij km ${kmList(segs.map((s) => s.start_km))}`]);
      }
    }
    if (r.weather_checked && !r.weather_error) {
      const rain = r.weather_segments || [];
      if (!rain.length) out.push(["ok", "🌧️", "Droog verwacht"]);
      else out.push(["warn", "🌧️", `Regen bij km ${rain.map((s) => `${Math.round(s.start_km)}–${Math.round(s.end_km)}`).join(", ")}`]);
    }
    return out;
  }

  function niceStep(total) {
    for (const step of [1, 2, 5, 10, 20, 25, 50, 100]) {
      if (total / step <= 10) return step;
    }
    return 100;
  }

  function svg(r) {
    const total = r.stats.total_distance_km;
    if (!(total > 0)) return "";
    const plotW = W - PAD_L - PAD_R;
    const top = PAD_T, bottom = H - PAD_B;
    const x = (km) => PAD_L + Math.max(0, Math.min(1, km / total)) * plotW;

    const profile = (r.profile || []).filter((p) => p.length === 2);
    const hasProfile = profile.length >= 2;
    let yAt = () => (top + bottom) / 2;
    let area = "", line = "", axis = "";
    if (hasProfile) {
      const eles = profile.map((p) => p[1]);
      let lo = Math.min(...eles), hi = Math.max(...eles);
      if (hi - lo < 20) { const mid = (hi + lo) / 2; lo = mid - 10; hi = mid + 10; }
      const yTop = top + 14, yBottom = bottom - 8;
      const y = (ele) => yBottom - ((ele - lo) / (hi - lo)) * (yBottom - yTop);
      yAt = (km) => {
        if (km <= profile[0][0]) return y(profile[0][1]);
        for (let i = 1; i < profile.length; i++) {
          if (km <= profile[i][0]) {
            const [k0, e0] = profile[i - 1], [k1, e1] = profile[i];
            return y(k1 === k0 ? e1 : e0 + ((e1 - e0) * (km - k0)) / (k1 - k0));
          }
        }
        return y(profile[profile.length - 1][1]);
      };
      const pts = profile.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`);
      line = `M${pts.join(" L")}`;
      area = `${line} L${x(profile[profile.length - 1][0]).toFixed(1)},${bottom} L${x(profile[0][0]).toFixed(1)},${bottom} Z`;
      axis = `<text x="${PAD_L - 4}" y="${yTop + 4}" text-anchor="end" class="ov-tick">${Math.round(hi)} m</text>` +
        `<text x="${PAD_L - 4}" y="${yBottom + 4}" text-anchor="end" class="ov-tick">${Math.round(lo)} m</text>`;
    }

    let body = "";
    // Regen: blauwe band over de hele hoogte
    (r.weather_segments || []).forEach((s) => {
      const x1 = x(s.start_km), x2 = Math.max(x(s.end_km), x1 + 3);
      const title = `Regen km ${num(s.start_km)}–${num(s.end_km)}: ${s.label}`;
      body += `<g data-lat="${s.coordinates[0][0]}" data-lon="${s.coordinates[0][1]}" class="ov-hit">` +
        `<title>${esc(title)}</title>` +
        `<rect x="${x1.toFixed(1)}" y="${top}" width="${(x2 - x1).toFixed(1)}" height="${bottom - top}" fill="#3b82f6" opacity="${s.uncertain ? 0.12 : 0.25}"/>` +
        `<text x="${((x1 + x2) / 2).toFixed(1)}" y="${top - 6}" text-anchor="middle" class="ov-icon">🌧️</text></g>`;
    });

    body += hasProfile
      ? `<path d="${area}" fill="#bae6fd" opacity=".55"/><path d="${line}" fill="none" stroke="#0284c7" stroke-width="1.6" stroke-linejoin="round"/>`
      : `<line x1="${PAD_L}" x2="${W - PAD_R}" y1="${(top + bottom) / 2}" y2="${(top + bottom) / 2}" stroke="#94a3b8" stroke-width="2"/>` +
        `<text x="${W / 2}" y="${(top + bottom) / 2 + 16}" text-anchor="middle" class="ov-tick">geen hoogtegegevens in deze route</text>`;

    // Verboden paden: gekleurde balk onderaan, icoon bij het begin
    (r.legality_segments || []).forEach((s) => {
      const x1 = x(s.start_km), x2 = Math.max(x(s.end_km), x1 + 3);
      const color = s.severity === "forbidden" ? "#dc2626" : "#f59e0b";
      const title = `${s.label}${s.way_name ? " – " + s.way_name : ""} (km ${num(s.start_km)}–${num(s.end_km)})`;
      body += `<g data-lat="${s.coordinates[0][0]}" data-lon="${s.coordinates[0][1]}" class="ov-hit">` +
        `<title>${esc(title)}</title>` +
        `<rect x="${x1.toFixed(1)}" y="${bottom - 6}" width="${(x2 - x1).toFixed(1)}" height="6" fill="${color}" rx="2"/>` +
        `<text x="${x1.toFixed(1)}" y="${bottom - 10}" text-anchor="middle" class="ov-icon ov-small">🚫</text></g>`;
    });

    // Waterpunten: klein icoon op het profiel
    (r.water_points || []).forEach((wp) => {
      const px = x(wp.along_route_km);
      const title = `${wp.name || "Drinkwaterpunt"} (km ${num(wp.along_route_km)})`;
      body += `<g data-lat="${wp.lat}" data-lon="${wp.lon}" class="ov-hit"><title>${esc(title)}</title>` +
        `<text x="${px.toFixed(1)}" y="${(yAt(wp.along_route_km) - 3).toFixed(1)}" text-anchor="middle" class="ov-icon ov-small">💧</text></g>`;
    });

    // Werkzaamheden: stippellijn door het hele profiel plus waarschuwingsicoon
    (r.road_works || []).forEach((w) => {
      const px = x(w.along_route_km);
      const impact = IMPACT[w.impact] || IMPACT.hinder;
      const title = `${impact.label}: ${w.cause || "Wegwerkzaamheden"} (km ${num(w.along_route_km)})`;
      body += `<g data-lat="${w.lat}" data-lon="${w.lon}" class="ov-hit"><title>${esc(title)}</title>` +
        `<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="${top - 2}" y2="${bottom}" stroke="${impact.color}" stroke-width="1.5" stroke-dasharray="3 3"/>` +
        `<text x="${px.toFixed(1)}" y="${top - 6}" text-anchor="middle" class="ov-icon">⚠️</text></g>`;
    });

    const step = niceStep(total);
    let ticks = "";
    for (let km = 0; km <= total + 1e-6; km += step) {
      ticks += `<line x1="${x(km).toFixed(1)}" x2="${x(km).toFixed(1)}" y1="${bottom}" y2="${bottom + 4}" stroke="#94a3b8"/>` +
        `<text x="${x(km).toFixed(1)}" y="${bottom + 16}" text-anchor="middle" class="ov-tick">${km}</text>`;
    }
    ticks += `<text x="${W - PAD_R}" y="${H - 2}" text-anchor="end" class="ov-tick">km</text>`;

    const label = `Overzicht van de route over ${num(total)} km`;
    return `<svg viewBox="0 0 ${W} ${H}" class="ov-svg" role="img" aria-label="${esc(label)}">` +
      `<line x1="${PAD_L}" x2="${W - PAD_R}" y1="${bottom}" y2="${bottom}" stroke="#cbd5e1"/>` +
      axis + body + ticks + `</svg>`;
  }

  /* HTML-blok voor een resultaat; leeg als er niets te tonen valt. */
  function html(r) {
    const items = chips(r);
    if (!items.length) return "";
    const legend = items
      .map(([kind, icon, text]) => `<li class="ov-chip ov-${kind}"><span aria-hidden="true">${icon}</span> ${esc(text)}</li>`)
      .join("");
    return `<div class="overview"><ul class="ov-chips">${legend}</ul>${svg(r)}</div>`;
  }

  /* Popup voor een werkzaamheid op de kaart. */
  function worksPopup(w) {
    const impact = IMPACT[w.impact] || IMPACT.hinder;
    const rows = [`<strong>⚠️ ${esc(w.cause || "Wegwerkzaamheden")}</strong>`,
      `<span style="color:${impact.color};font-weight:600">${esc(impact.label)}</span>`,
      `km ${w.along_route_km.toFixed(1)} · ${Math.round(w.distance_to_route_m)} m van route`];
    if (w.authority) rows.push(esc(w.authority));
    if (w.start || w.end) rows.push(`Periode: ${esc(w.start || "?")} t/m ${esc(w.end || "?")}`);
    if (w.detour) rows.push(`<em>${esc(w.detour)}</em>`);
    return rows.join("<br>");
  }

  /* Klik op een icoon of balk: kaart naar die plek. */
  function bind(root, map) {
    if (!root || root.dataset.overviewBound) return;
    root.dataset.overviewBound = "1";
    root.addEventListener("click", (event) => {
      const target = event.target.closest && event.target.closest("[data-lat]");
      if (!target || !root.contains(target)) return;
      const lat = parseFloat(target.dataset.lat), lon = parseFloat(target.dataset.lon);
      if (Number.isFinite(lat) && Number.isFinite(lon)) map.setView([lat, lon], 16);
    });
  }

  return { html, bind, worksPopup, IMPACT };
})();
