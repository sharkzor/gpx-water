/* Gedeelde regencontrole: invoer, kaartweergave en samenvatting (alle pagina's). */
window.Weather = (function () {
  const RAIN_COLOR = "#2563eb";

  function esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  const el = (id) => document.getElementById(id);
  const pad = (n) => String(n).padStart(2, "0");
  const hhmm = (iso) => iso.slice(11, 16);
  const mm = (v) => v.toFixed(1).replace(".", ",");
  const DAYS = ["zo", "ma", "di", "wo", "do", "vr", "za"];

  function dayTime(iso) {
    const d = new Date(iso.slice(0, 16));
    return `${DAYS[d.getDay()]} ${pad(d.getDate())}-${pad(d.getMonth() + 1)} ${hhmm(iso)}`;
  }

  function localInputValue(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  /* Checkbox toont de invoer; vertrektijd standaard het volgende kwartier. */
  function init() {
    const box = el("weather");
    const wrap = el("weather-wrap");
    const dep = el("departure");
    if (!box || !wrap || !dep) return;
    const now = new Date();
    now.setSeconds(0, 0);
    now.setMinutes(Math.ceil((now.getMinutes() + 1) / 15) * 15);
    dep.value = localInputValue(now);
    // Hele minuut, zodat de browser elke gekozen tijd als geldige stap ziet.
    dep.min = localInputValue(new Date(Date.now() - 3600 * 1000));
    const sync = () => { wrap.hidden = !box.checked; };
    box.addEventListener("change", sync);
    sync();
  }

  function enabled() {
    const box = el("weather");
    return Boolean(box && box.checked);
  }

  /* Parameters voor een JSON-verzoek (Strava/routeboek). */
  function params() {
    if (!enabled()) return { weather: false };
    return {
      weather: true,
      departure: el("departure").value || null,
      speed_kmh: Number(el("speed_kmh").value) || null,
    };
  }

  /* Zelfde parameters voor een multipart-formulier (upload). */
  function appendTo(formData) {
    const p = params();
    if (!p.weather) return;
    formData.append("weather", "true");
    if (p.departure) formData.append("departure", p.departure);
    if (p.speed_kmh) formData.append("speed_kmh", String(p.speed_kmh));
  }

  function popup(seg) {
    const icon = seg.uncertain ? "🌦️" : "🌧️";
    const rows = [`<strong>${icon} ${seg.uncertain ? "Mogelijk " + esc(seg.label.toLowerCase()) : esc(seg.label)} (tot ${mm(seg.max_mm_h)} mm/u)</strong>`];
    rows.push(`km ${seg.start_km.toFixed(0)} t/m ${seg.end_km.toFixed(0)} · rond ${hhmm(seg.start_time)}–${hhmm(seg.end_time)}`);
    if (seg.max_probability !== null && seg.max_probability !== undefined) {
      rows.push(`Kans op neerslag ${seg.max_probability}%`);
    }
    rows.push(`<em>${esc(seg.sources.join(", "))}</em>`);
    return rows.join("<br>");
  }

  function draw(result, layers) {
    (result.weather_segments || []).forEach((seg) => {
      if (!seg.coordinates || seg.coordinates.length < 2) return;
      L.polyline(seg.coordinates, {
        color: RAIN_COLOR, weight: 9, opacity: seg.uncertain ? 0.3 : 0.55,
        dashArray: seg.uncertain ? "4 8" : null,
      })
        .bindPopup(popup(seg)).addTo(layers);
      L.marker(seg.coordinates[0], {
        icon: L.divIcon({
          className: "rain-marker",
          html: `<div style="font-size:20px;line-height:20px;text-shadow:0 0 3px #fff">${seg.uncertain ? "🌦️" : "🌧️"}</div>`,
          iconSize: [20, 20],
          iconAnchor: [10, 10],
        }),
      }).bindPopup(popup(seg)).addTo(layers);
    });
  }

  /* Korte samenvatting, of null als er niet op regen is gecontroleerd. */
  function summary(result) {
    if (!result.weather_checked) return null;
    if (result.weather_error) return "niet beschikbaar";
    const segs = result.weather_segments || [];
    if (!segs.length) return "droog verwacht ☀️";
    const sure = segs.filter((s) => !s.uncertain);
    if (!sure.length) return "waarschijnlijk droog (kleine kans op regen)";
    const km = sure.reduce((acc, s) => acc + Math.max(1, s.end_km - s.start_km), 0);
    return `${sure.length} nat${sure.length === 1 ? " stuk" : "te stukken"} (~${Math.round(km)} km)`;
  }

  function colorFor(mmh, threshold) {
    if (mmh < threshold) return "#e5e7eb";
    if (mmh < 1) return "#93c5fd";
    if (mmh < 4) return "#3b82f6";
    return "#1e3a8a";
  }

  /* Uitgebreid blok: vertrek/aankomst, tijdlijnbalk en de natte stukken. */
  function panel(result) {
    if (!result.weather_checked) return "";
    if (result.weather_error) {
      return `<div class="weather-panel"><p class="warn-text">${esc(result.weather_error)}</p></div>`;
    }
    const samples = result.weather_samples || [];
    const segs = result.weather_segments || [];
    const threshold = 0.1;
    const bar = samples.map((s) =>
      `<span style="background:${colorFor(s.mm_h, threshold)}" title="km ${s.km.toFixed(0)} · ${hhmm(s.time)} · ${mm(s.mm_h)} mm/u${s.probability !== null && s.probability !== undefined ? ` · kans ${s.probability}%` : ""} · ${esc(s.source)}"></span>`
    ).join("");
    const rows = segs.map((s) =>
      `<li${s.uncertain ? ' class="muted"' : ""}>km ${s.start_km.toFixed(0)}–${s.end_km.toFixed(0)} · ${hhmm(s.start_time)}–${hhmm(s.end_time)} · ` +
      `${s.uncertain ? "mogelijk " : ""}${esc(s.label.toLowerCase())} tot ${mm(s.max_mm_h)} mm/u` +
      `${s.max_probability !== null && s.max_probability !== undefined ? ` (kans ${s.max_probability}%)` : ""}</li>`
    ).join("");
    const prob = result.weather_max_probability;
    return `<div class="weather-panel">` +
      `<p><strong>${segs.some((s) => !s.uncertain) ? "🌧️" : segs.length ? "🌦️" : "☀️"} ${esc(summary(result))}</strong><br>` +
      `<small>Vertrek ${esc(dayTime(result.weather_departure))} → aankomst ${esc(dayTime(result.weather_arrival))} ` +
      `bij ${mm(result.weather_speed_kmh)} km/u` +
      `${prob !== null && prob !== undefined ? ` · max. kans op neerslag ${prob}%` : ""}</small></p>` +
      `<div class="rain-bar">${bar}</div>` +
      `<div class="rain-scale"><span>${hhmm(result.weather_departure)}</span><span>${hhmm(result.weather_arrival)}</span></div>` +
      (rows ? `<ul class="rain-list">${rows}</ul>` : "") +
      (result.weather_note ? `<p class="warn-text"><small>${esc(result.weather_note)}</small></p>` : "") +
      `<p class="muted"><small>Verwachting van ${esc(hhmm(result.weather_issued))}; controleer vlak voor vertrek opnieuw.</small></p>` +
      `</div>`;
  }

  return { init, params, appendTo, enabled, draw, summary, panel };
})();
