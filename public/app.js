/* Ontario Parks roofed-accommodation finder: front end */

const $ = (s) => document.querySelector(s);
const AVAIL = {
  0: ["c-av", "Available"],
  1: ["c-un", "Booked"],
  2: ["c-cl", "Not operating"],
  3: ["c-cl", "Not reservable"],
  4: ["c-cl", "Closed for season"],
  5: ["c-cl", "n/a"],
  6: ["c-cl", "n/a"],
  7: ["c-pa", "Partial"],
  8: ["c-hd", "Held in cart"],
};

let map, markers = {}, lastPayload = null;

/* ---------------- filters ---------------- */

function filters() {
  const types = [...document.querySelectorAll("#f-types input:checked")]
    .map((c) => c.value);
  const p = new URLSearchParams();
  p.set("nights", $("#f-nights").value || 2);
  if (types.length && types.length < 4) p.set("types", types.join(","));
  if ($("#f-capacity").value) p.set("capacity", $("#f-capacity").value);
  if ($("#f-start").value) p.set("start", $("#f-start").value);
  if ($("#f-end").value) p.set("end", $("#f-end").value);
  const arrive = document.querySelector('#f-arrive input:checked');
  if (arrive && arrive.value !== "any") p.set("arrive", arrive.value);
  if ($("#f-loose").checked) p.set("loose", "1");
  const park = $("#f-park").value.trim();
  if (park) p.set("park", park);
  return p;
}

/* If every type is unchecked nothing can match, which is a confusing
   empty state. Treat "none checked" as "all checked". */
function guardTypes() {
  const boxes = [...document.querySelectorAll("#f-types input")];
  if (!boxes.some((b) => b.checked)) boxes.forEach((b) => (b.checked = true));
}

/* ---------------- map ---------------- */

function bucket(n) {
  if (!n) return 0;
  if (n < 10) return 1;
  if (n < 50) return 2;
  return 3;
}

function initMap() {
  map = L.map("map", { zoomControl: true }).setView([48.4, -84.0], 5);
  // Plain OSM tiles: no API key, no sign-up, and they suit the light theme
  // as-is (the old dark build inverted them in CSS).
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 18,
  }).addTo(map);
}

function drawMarkers(parks) {
  Object.values(markers).forEach((m) => map.removeLayer(m));
  markers = {};

  parks.forEach((p) => {
    if (p.lat == null || p.lon == null) return;
    const b = bucket(p.total);
    const size = p.total ? Math.min(34, 18 + Math.log10(p.total + 1) * 5) : 13;
    const colors = ["#c7c7cc", "#ff3b30", "#ff9500", "#34c759"];
    // log scale: counts span 1..1500+, so linear sizing swamps the map

    const icon = L.divIcon({
      className: "",
      html:
        `<div class="pin" style="width:${size}px;height:${size}px;` +
        `background:${colors[b]};opacity:${p.total ? 1 : 0.6}">` +
        `${p.total || ""}</div>`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    });

    const m = L.marker([p.lat, p.lon], { icon, zIndexOffset: p.total })
      .addTo(map);

    const types = Object.entries(p.byType)
      .map(([k, v]) => `${k}: ${v}`).join("<br>") || "nothing matching";
    m.bindPopup(
      `<b>${esc(p.name)}</b><br>` +
      `<span style="color:#6e6e73">${p.unitCount} roofed unit(s)</span><br><br>` +
      `<b>${p.total}</b> matching stay(s)` +
      (p.earliest ? `<br>earliest ${p.earliest}` : "") +
      `<br><br>${types}` +
      (p.total ? `<br><button onclick="openPark('${p.id}')">View dates</button>` : "")
    );
    if (p.total) m.on("dblclick", () => openPark(p.id));
    markers[p.id] = m;
  });
}

/* ---------------- sidebar ---------------- */

function ago(seconds) {
  if (seconds == null) return "";
  const m = Math.round(seconds / 60);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderList(payload) {
  const withAny = payload.parks.filter((p) => p.total);
  const blocked = payload.blocked || [];
  let note = "";
  if (blocked.length) {
    const need = Math.min(...blocked.map((b) => b.minNights));
    note =
      `<div class="hint">${blocked.length} park(s) have free nights that are ` +
      `too short to book at ${$("#f-nights").value} night(s). ` +
      `Shortest that would work: <b>${need}</b>. ` +
      `<a href="#" id="bumpnights">Try ${need} nights</a></div>`;
  }
  $("#summary").innerHTML =
    `<b>${payload.total}</b> stays · <b>${withAny.length}</b> parks with ` +
    `availability · window ${payload.start} → ${payload.end}` + note;
  const bump = $("#bumpnights");
  if (bump) {
    bump.onclick = (e) => {
      e.preventDefault();
      $("#f-nights").value = Math.min(...blocked.map((b) => b.minNights));
      refresh();
    };
  }

  const box = $("#results");
  if (!payload.parks.length) {
    box.innerHTML = `<div class="empty">No parks match.</div>`;
    return;
  }
  box.innerHTML = payload.parks.map((p) => {
    const b = bucket(p.total);
    const types = Object.keys(p.byType).join(", ") || `${p.unitCount} units`;
    const blockedNote = (!p.total && p.blockedMinNights)
      ? ` · needs ${p.blockedMinNights}+ nights` : "";
    return (
      `<div class="parkrow ${p.total ? "" : "zero"}" data-id="${p.id}">` +
      `<i class="dot d${b}"></i>` +
      `<span class="nm"><b>${esc(p.name)}</b>` +
      `<small>${esc(types)}${p.earliest ? " · from " + p.earliest : ""}` +
      `${blockedNote}</small></span>` +
      `<span class="count">${p.total || "–"}</span>` +
      `<svg class="chev" width="7" height="12" viewBox="0 0 7 12" fill="none" ` +
      `stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
      `stroke-linejoin="round"><path d="M1 1l5 5-5 5"/></svg></div>`
    );
  }).join("");

  box.querySelectorAll(".parkrow").forEach((row) => {
    row.onclick = () => {
      const id = row.dataset.id;
      const p = payload.parks.find((x) => x.id === id);
      if (p && p.lat != null) {
        map.flyTo([p.lat, p.lon], 9, { duration: 0.6 });
        if (markers[id]) markers[id].openPopup();
      }
      if (p && p.total) openPark(id);
    };
  });
}

/* ---------------- park detail ---------------- */

async function openPark(id) {
  const p = filters();
  p.set("id", id);
  let data;
  try {
    data = await api("/api/park", p);
  } catch (err) {
    return toast("Could not load park: " + err.message, 4000);
  }
  if (data.error) return toast(data.error);

  const start = new Date(data.gridStart + "T00:00:00");
  const fmt = (d) => d.toLocaleDateString(undefined,
    { month: "short", day: "numeric" });

  // Availability strips, all sharing one horizontal scroller so that a given
  // column is the same date on every unit, with a month ruler across the top.
  const nDays = data.units.length ? data.units[0].series.length : 0;
  const months = [];
  for (let i = 0; i < nDays; i++) {
    const d = new Date(start); d.setDate(d.getDate() + i);
    const key = d.getFullYear() + "-" + d.getMonth();
    const last = months[months.length - 1];
    if (!last || last.key !== key) {
      months.push({
        key, days: 1,
        label: d.toLocaleDateString(undefined, { month: "short", year: "2-digit" }),
      });
    } else last.days++;
  }
  const ruler = months.map((m) =>
    `<div class="mo" style="width:${m.days * 8}px">${m.label}</div>`).join("");

  const rows = data.units.map((u) => {
    const cells = u.series.map((v, i) => {
      const d = new Date(start); d.setDate(d.getDate() + i);
      const [cls, label] = AVAIL[v] || ["c-cl", "?"];
      return `<div class="cell ${cls}" title="${fmt(d)}: ${label}"></div>`;
    }).join("");
    const open = u.series.filter((v) => v === 0).length;
    const ms = u.minStay === u.maxMinStay
      ? (u.minStay > 1 ? ` · min ${u.minStay} nights` : "")
      : ` · min ${u.minStay}–${u.maxMinStay} nights`;
    return (
      `<div class="gridunit"><div class="lbl"><b>${esc(u.name)}</b> ` +
      `<span>· ${esc(u.type)}${u.capacity ? " · sleeps " + u.capacity : ""} ` +
      `· ${open} open night(s)${ms}</span></div>` +
      `<div class="strip">${cells}</div></div>`
    );
  }).join("");

  const grid = data.units.length
    ? `<div class="striplegend">` +
      `<span><i class="cell c-av"></i>available</span>` +
      `<span><i class="cell c-un"></i>booked</span>` +
      `<span><i class="cell c-pa"></i>partial</span>` +
      `<span><i class="cell c-hd"></i>held</span>` +
      `<span><i class="cell c-cl"></i>closed</span></div>` +
      `<div class="gridscroll"><div class="gridinner" ` +
      `style="width:${nDays * 8}px">` +
      `<div class="ruler">${ruler}</div>${rows}</div></div>`
    : `<div class="empty">No units match the type/capacity filter.</div>`;

  const stays = data.stays.map((s) => (
    `<div class="stay ${s.partial ? "partial" : ""}">` +
    `<span class="when">${s.arrive} → ${s.depart}</span>` +
    `<span class="who">${esc(s.unit)} · ${esc(s.type)}` +
    `${s.capacity ? " · sleeps " + s.capacity : ""}` +
    `${s.partial ? " · partial" : ""}</span>` +
    `<a href="${s.url}" target="_blank" rel="noopener">Book</a></div>`
  )).join("") || `<div class="empty">No bookable stays with these filters.</div>`;

  $("#detail-body").innerHTML =
    `<h2>${esc(data.name)}</h2>` +
    `<div class="meta">${data.units.length} unit(s) shown · ` +
    `${data.stayTotal} matching stay(s)</div>` +
    `<h3>Availability by night <span style="text-transform:none">` +
    `(hover a cell for the date)</span></h3>${grid}` +
    `<h3>Bookable stays${data.stayTotal > data.stays.length
      ? ` · showing first ${data.stays.length}` : ""}</h3>${stays}`;
  $("#detail").classList.remove("hidden");
}

/* ---------------- plumbing ---------------- */

/* When deployed, /api/search sits behind Vercel's CDN. After a Rescan we
   need a different URL or the edge keeps serving the pre-scan results. */
let cacheBust = "";

function api(path, params) {
  const p = params || new URLSearchParams();
  if (cacheBust) p.set("_", cacheBust);
  return fetch(path + "?" + p.toString()).then((r) => {
    if (!r.ok) throw new Error(`${path} returned ${r.status}`);
    return r.json();
  });
}

let timer = null;
let firstLoad = true;
function refresh(debounce = 0) {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    guardTypes();
    if (firstLoad) {
      // A cold serverless start scans Ontario Parks live, which takes a few
      // seconds. Say so rather than showing an empty page.
      $("#results").innerHTML =
        `<div class="empty">Scanning Ontario Parks for live availability…<br>` +
        `<small>First load can take a few seconds.</small></div>`;
    }
    try {
      const payload = await api("/api/search", filters());
      if (payload.error) return toast(payload.error);
      lastPayload = payload;
      firstLoad = false;
      const age = ago(payload.ageSeconds);
      const live = payload.canScan !== false;
      $("#scanmeta").textContent =
        (age ? `data ${age} · ` : "") + `${payload.start} → ${payload.end}`;
      $("#scanmeta").title = payload.scannedAt
        ? `Availability scanned at ${payload.scannedAt}`
        : "";
      // Stale data is worth flagging: this is a cancellation hunt.
      $("#scanmeta").classList.toggle(
        "stale", !live && payload.ageSeconds != null &&
                 payload.ageSeconds > 3 * 3600);

      const b = $("#rescan");
      if (live) {
        b.textContent = "Rescan";
        b.title = "Pull fresh availability from Ontario Parks now";
      } else {
        // Deployed: Ontario Parks blocks Vercel, so a GitHub Action does the
        // scanning hourly. All this button can do is pick up the newest one.
        b.textContent = "Refresh";
        b.title = "Ontario Parks blocks requests from Vercel, so a scheduled "
                + "GitHub Action scans hourly and redeploys. This re-checks "
                + "for the newest published data.";
      }
      drawMarkers(payload.parks);
      renderList(payload);
    } catch (err) {
      $("#results").innerHTML =
        `<div class="empty">Could not load availability.<br>` +
        `<small>${esc(err.message)}</small></div>`;
      toast("Request failed: " + err.message, 4000);
    }
  }, debounce);
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

function wire() {
  ["#f-nights", "#f-capacity", "#f-start", "#f-end"].forEach(
    (s) => ($(s).oninput = () => refresh(350)));
  $("#f-loose").onchange = () => refresh();
  document.querySelectorAll("#f-arrive input")
    .forEach((r) => (r.onchange = () => refresh()));
  document.querySelectorAll("#f-types input")
    .forEach((c) => (c.onchange = () => refresh()));
  $("#f-park").oninput = () => refresh(300);

  $("#detail-close").onclick = () => $("#detail").classList.add("hidden");
  $("#detail").onclick = (e) => {
    if (e.target.id === "detail") $("#detail").classList.add("hidden");
  };
  document.onkeydown = (e) => {
    if (e.key === "Escape") $("#detail").classList.add("hidden");
  };

  $("#rescan").onclick = async () => {
    const b = $("#rescan");
    const label = b.textContent;
    b.disabled = true; b.textContent = "Checking…";
    try {
      const r = await fetch("/api/scan?_=" + Date.now(), { method: "POST" })
        .then((x) => x.json());
      // Change the search URL so the CDN cannot serve pre-refresh results.
      cacheBust = String(Date.now());
      if (r.error) toast(r.error);
      else if (r.source === "live") toast("Fresh data pulled from Ontario Parks");
      else toast(`Showing data from ${ago(r.ageSeconds)}`);
      refresh();
    } catch (err) {
      toast("Scan failed: " + err);
    } finally {
      b.disabled = false; b.textContent = label;
    }
  };
}

window.openPark = openPark;
initMap();
wire();
refresh();
