/* Gedeelde keuze van controles (waterpunten, wegwerkzaamheden, verboden paden,
   regen) en de statuskaartjes in het resultaat. Gebruikt op alle pagina's. */
window.Checks = (function () {
  const STORE_KEY = "gpxw-checks";
  const DEFAULTS = { water: true, roadworks: false, legality: false, weather: false };
  // Checkbox → blok met instellingen dat alleen zichtbaar is als hij aan staat.
  const PANELS = { water: "water-wrap", roadworks: "roadworks-date-wrap", legality: "legality-wrap" };

  const el = (id) => document.getElementById(id);
  const checked = (id) => Boolean(el(id) && el(id).checked);
  const km = (v) => `${v.toFixed(1).replace(".", ",")} km`;

  function esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function load() {
    try {
      return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(STORE_KEY) || "{}") };
    } catch (err) {
      return { ...DEFAULTS };
    }
  }

  function save() {
    const state = {};
    Object.keys(DEFAULTS).forEach((id) => {
      if (el(id)) state[id] = el(id).checked;
    });
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({ ...load(), ...state }));
    } catch (err) {
      /* privévenster of opslag vol: dan onthouden we het gewoon niet */
    }
  }

  function anySelected() {
    return Object.keys(DEFAULTS).some(checked);
  }

  /* Herstel de vorige keuze, koppel de uitklapblokken en bewaak de knop. */
  function init(button) {
    const state = load();
    Object.keys(DEFAULTS).forEach((id) => {
      if (el(id)) el(id).checked = Boolean(state[id]);
    });
    const hint = document.createElement("p");
    hint.className = "hint checks-hint";
    hint.textContent = "Kies minimaal één controle.";
    if (button) button.insertAdjacentElement("afterend", hint);

    const update = () => {
      Object.entries(PANELS).forEach(([id, panel]) => {
        if (el(id) && el(panel)) el(panel).hidden = !el(id).checked;
      });
      const ok = anySelected();
      if (button) button.disabled = !ok;
      hint.hidden = ok;
    };
    document.querySelectorAll("input[data-check]").forEach((box) => {
      box.addEventListener("change", () => { save(); update(); });
    });
    update();
    Weather.init();
  }

  /* Parameters voor een JSON-verzoek (Strava/routeboek). */
  function params() {
    const roadworks = checked("roadworks");
    return {
      water: checked("water"),
      radius: Number(el("radius").value),
      source: el("source").value,
      roadworks: roadworks,
      ride_date: roadworks && el("ride_date") ? el("ride_date").value || null : null,
      legality: checked("legality"),
      ...Weather.params(),
    };
  }

  /* Zelfde parameters voor een multipart-formulier (upload). */
  function appendTo(formData) {
    const p = params();
    formData.append("water", p.water ? "true" : "false");
    formData.append("radius", String(p.radius));
    formData.append("source", p.source);
    if (p.roadworks) {
      formData.append("roadworks", "true");
      if (p.ride_date) formData.append("ride_date", p.ride_date);
    }
    if (p.legality) formData.append("legality", "true");
    Weather.appendTo(formData);
  }

  function busyText() {
    if (checked("roadworks")) return "Bezig… (de eerste controle op wegwerkzaamheden duurt ~10 seconden)";
    if (checked("water")) return "Bezig… (waterpunten ophalen kan even duren)";
    return "Bezig met controleren…";
  }

  /* Eén kaartje per uitgevoerde controle: [niveau, icoon, titel, tekst]. */
  function statuses(r) {
    const out = [];
    if (r.water_checked !== false) {
      const s = r.stats;
      if (!s.water_point_count) {
        out.push(["bad", "💧", "Waterpunten", `geen gevonden binnen ${r.radius_m} m`]);
      } else {
        const text = `${s.water_point_count} gevonden · langste stuk zonder water ${km(s.longest_gap_km)}`;
        out.push([s.warning ? "warn" : "ok", "💧", "Waterpunten", s.warning ? `${text} (vanaf km ${s.longest_gap_start_km.toFixed(0)})` : text]);
      }
    }
    if (r.roadworks_checked) {
      const n = (r.road_works || []).length;
      if (r.roadworks_error) out.push(["na", "⚠️", "Wegwerkzaamheden", r.roadworks_error]);
      else out.push([n ? "warn" : "ok", "⚠️", "Wegwerkzaamheden", n ? `${n} op de route (${r.roadworks_date})` : `geen op de route (${r.roadworks_date})`]);
    }
    if (r.legality_checked) {
      if (r.legality_error) {
        out.push(["na", "⛔", "Verboden paden", r.legality_error]);
      } else {
        const c = Legality.counts(r);
        out.push([c.forbidden ? "bad" : c.warning ? "warn" : "ok", "⛔", "Verboden paden", Legality.summary(r)]);
      }
    }
    if (r.weather_checked) {
      if (r.weather_error) {
        out.push(["na", "🌧️", "Regen", r.weather_error]);
      } else {
        const wet = (r.weather_segments || []).some((s) => !s.uncertain);
        out.push([wet ? "warn" : "ok", wet ? "🌧️" : "☀️", "Regen", Weather.summary(r).replace(" ☀️", "")]);
      }
    }
    return out;
  }

  /* HTML met statuskaartjes, plus "Alles in orde" als nergens iets speelt. */
  function cards(r) {
    const items = statuses(r);
    const allOk = items.length && items.every((i) => i[0] === "ok");
    return (
      `<div class="check-cards">` +
      (allOk ? `<div class="check-card all-ok">✅ <strong>Alles in orde</strong></div>` : "") +
      items.map(([level, icon, title, text]) =>
        `<div class="check-card ${level}"><span class="icon">${icon}</span>` +
        `<span><strong>${esc(title)}</strong><small>${esc(text)}</small></span></div>`
      ).join("") +
      `</div>`
    );
  }

  return { init, params, appendTo, anySelected, busyText, cards };
})();
