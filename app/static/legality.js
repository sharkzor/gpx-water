/* Gedeelde weergave van de controle op verboden paden (upload- en Stravapagina). */
window.Legality = (function () {
  const STYLE = {
    forbidden: { color: "#dc2626", icon: "⛔", word: "verboden" },
    warning: { color: "#ea580c", icon: "❗", word: "let op" },
  };

  function esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function length(m) {
    return m >= 1000 ? `${(m / 1000).toFixed(1).replace(".", ",")} km` : `${Math.round(m)} m`;
  }

  function popup(seg) {
    const style = STYLE[seg.severity] || STYLE.warning;
    const rows = [`<strong>${style.icon} ${esc(seg.label)}</strong>`];
    if (seg.way_name) rows.push(esc(seg.way_name));
    rows.push(`km ${seg.start_km.toFixed(1)} t/m ${seg.end_km.toFixed(1)} · ${length(seg.length_m)}`);
    rows.push("<em>OpenStreetMap</em>");
    return rows.join("<br>");
  }

  /* Teken de stukken als dikke lijn over de route, met een marker op het begin. */
  function draw(result, layers) {
    (result.legality_segments || []).forEach((seg) => {
      const style = STYLE[seg.severity] || STYLE.warning;
      L.polyline(seg.coordinates, { color: style.color, weight: 8, opacity: 0.8 })
        .bindPopup(popup(seg)).addTo(layers);
      L.marker(seg.coordinates[0], {
        icon: L.divIcon({
          className: "legality-marker",
          html: `<div style="font-size:20px;line-height:20px;text-shadow:0 0 3px #fff">${style.icon}</div>`,
          iconSize: [20, 20],
          iconAnchor: [10, 10],
        }),
      }).bindPopup(popup(seg)).addTo(layers);
    });
  }

  function counts(result) {
    const segs = result.legality_segments || [];
    return {
      forbidden: segs.filter((s) => s.severity === "forbidden").length,
      warning: segs.filter((s) => s.severity === "warning").length,
    };
  }

  /* Korte samenvatting, bijvoorbeeld "1 verboden, 2 let op" of "geen". */
  function summary(result) {
    if (!result.legality_checked) return null;
    if (result.legality_error) return "niet beschikbaar";
    const c = counts(result);
    if (!c.forbidden && !c.warning) return "geen gevonden";
    const parts = [];
    if (c.forbidden) parts.push(`${c.forbidden} verboden`);
    if (c.warning) parts.push(`${c.warning} let op`);
    return parts.join(", ");
  }

  /* Vul een lijst; klikken zoomt naar het stuk. */
  function fillList(listEl, result, map) {
    listEl.innerHTML = "";
    (result.legality_segments || []).forEach((seg) => {
      const style = STYLE[seg.severity] || STYLE.warning;
      const li = document.createElement("li");
      li.innerHTML = `km ${seg.start_km.toFixed(1)} – ${style.icon} ${esc(seg.label)}` +
        `<small>${esc(seg.way_name || "naamloos pad")} · ${length(seg.length_m)}</small>`;
      li.style.cursor = "pointer";
      li.addEventListener("click", () => map.fitBounds(L.latLngBounds(seg.coordinates), { maxZoom: 17 }));
      listEl.appendChild(li);
    });
  }

  return { draw, summary, fillList, counts };
})();
