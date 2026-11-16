// Lirico website worker: static site + download counter + privacy-friendly stats.
// Stores no IP addresses, no cookies and no song titles.

const EXE_URL = "https://github.com/sumitakhuli/Lirico/releases/latest/download/Lirico.exe";
const MAX_BODY = 4096;

const now = () => Math.floor(Date.now() / 1000);
const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", "access-control-allow-origin": "*" },
  });

async function readJson(request) {
  const text = await request.text();
  if (text.length > MAX_BODY) return null;
  try { return JSON.parse(text); } catch { return null; }
}

const clip = (v, n) => (typeof v === "string" ? v.slice(0, n) : null);

async function dailyVisitor(request) {
  // Hash of (ip, browser, today): counts unique visitors per day; the day is part
  // of the input, so the same person can't be followed from one day to the next.
  const ip = request.headers.get("cf-connecting-ip") || "";
  const ua = request.headers.get("user-agent") || "";
  const day = new Date().toISOString().slice(0, 10);
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${ip}|${ua}|${day}|lirico`));
  return [...new Uint8Array(buf)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const country = request.cf?.country || null;

    // --- Google Search Console ownership file (served as-is; Pages would redirect .html)
    if (url.pathname === "/google74dab7bd6afadbbc.html") {
      return new Response("google-site-verification: google74dab7bd6afadbbc.html", { headers: { "content-type": "text/html; charset=utf-8" } });
    }

    // --- downloads: count, then hand over to GitHub Releases
    if (url.pathname === "/download") {
      ctx.waitUntil(
        env.DB.prepare("INSERT INTO downloads (ts, country, src) VALUES (?, ?, ?)")
          .bind(now(), country, clip(url.searchParams.get("src") || "direct", 24)).run()
      );
      return new Response(null, { status: 302, headers: { location: EXE_URL, "x-robots-tag": "noindex" } });
    }

    // --- page views (sent by the site with navigator.sendBeacon)
    if (url.pathname === "/api/hit" && request.method === "POST") {
      const body = (await readJson(request)) || {};
      let ref = null;
      try { ref = body.ref ? new URL(body.ref).hostname : null; } catch { ref = null; }
      if (ref && ref === url.hostname) ref = null;
      const visitor = await dailyVisitor(request);
      ctx.waitUntil(
        env.DB.prepare("INSERT INTO hits (ts, path, ref, country, visitor) VALUES (?, ?, ?, ?, ?)")
          .bind(now(), clip(body.path || "/", 120), clip(ref, 120), country, visitor).run()
      );
      return new Response(null, { status: 204 });
    }

    // --- anonymous app usage (opt-out in the app's Settings)
    if (url.pathname === "/api/events" && request.method === "POST") {
      const b = await readJson(request);
      if (!b || typeof b.install !== "string" || !/^[a-f0-9]{16,40}$/.test(b.install)) return json({ ok: false }, 400);
      const kind = b.kind === "start" ? "start" : b.kind === "usage" ? "usage" : null;
      if (!kind) return json({ ok: false }, 400);
      const data = JSON.stringify(b.data && typeof b.data === "object" ? b.data : {}).slice(0, 2000);
      ctx.waitUntil(
        env.DB.prepare("INSERT INTO app_events (ts, install, version, os, kind, data, country) VALUES (?, ?, ?, ?, ?, ?, ?)")
          .bind(now(), b.install, clip(b.version, 20), clip(b.os, 40), kind, data, country).run()
      );
      return json({ ok: true });
    }

    // --- public counter for the site
    if (url.pathname === "/api/public") {
      const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM downloads").first();
      return json({ downloads: row?.n || 0 });
    }

    // --- private dashboard
    if (url.pathname === "/stats") {
      if (!env.STATS_KEY || url.searchParams.get("key") !== env.STATS_KEY) return new Response("Not found", { status: 404 });
      return new Response(await dashboard(env), { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex, nofollow" } });
    }

    return env.ASSETS.fetch(request);
  },
};

// ------------------------------------------------------------------ dashboard
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : n);

// Last `days` dates (UTC, oldest first) with zeros where nothing happened.
function fillDays(rows, days) {
  const by = Object.fromEntries((rows || []).map((r) => [r.d, r.n]));
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10);
    out.push({ d, n: by[d] || 0 });
  }
  return out;
}

// Single-series column chart: thin columns, rounded data end, recessive grid,
// labels only on the peak and today, a tooltip on every column.
function columns(rows, unit, id) {
  const W = 720, H = 200, L = 34, R = 8, T = 14, B = 26;
  const max = Math.max(1, ...rows.map((r) => r.n));
  const nice = max <= 4 ? 4 : Math.ceil(max / 4) * 4;
  const step = (W - L - R) / rows.length, bw = Math.max(6, step - 6);
  const y = (v) => T + (H - T - B) * (1 - v / nice);
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = (nice / 4) * k, yy = y(v).toFixed(1);
    g += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" class="${k ? "grid" : "base"}"/>` +
         `<text x="${L - 8}" y="${yy}" class="ax" text-anchor="end" dominant-baseline="middle">${fmt(v)}</text>`;
  }
  const peak = rows.reduce((a, r, i) => (r.n > rows[a].n ? i : a), 0);
  rows.forEach((r, i) => {
    const x = L + i * step + (step - bw) / 2, top = y(r.n), h = y(0) - top, rr = Math.min(4, bw / 2, h);
    const label = `${new Date(r.d + "T00:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })}`;
    if (r.n > 0) {
      g += `<path class="bar" d="M${x},${y(0)} V${top + rr} Q${x},${top} ${x + rr},${top} H${x + bw - rr} Q${x + bw},${top} ${x + bw},${top + rr} V${y(0)} Z"/>`;
    }
    g += `<rect class="hit" x="${L + i * step}" y="${T}" width="${step}" height="${H - T - B}" data-tip="${esc(label)}: ${fmt(r.n)} ${esc(unit)}"/>`;
    if ((i === peak && r.n > 0) || (i === rows.length - 1 && r.n > 0 && i !== peak)) {
      g += `<text x="${x + bw / 2}" y="${top - 5}" class="val" text-anchor="middle">${fmt(r.n)}</text>`;
    }
    if (i % 2 === (rows.length - 1) % 2) {
      g += `<text x="${x + bw / 2}" y="${H - 8}" class="ax" text-anchor="middle">${esc(label)}</text>`;
    }
  });
  const table = rows.map((r) => `<tr><td>${r.d}</td><td>${fmt(r.n)}</td></tr>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-labelledby="${id}">${g}</svg>
  <details class="tbl"><summary>Show as table</summary><table><tr><th>Day (UTC)</th><th>${esc(unit)}</th></tr>${table}</table></details>`;
}

// Ranked horizontal bars (countries, referrers, versions...).
function ranked(rows, key, empty, labelFn = (v) => v) {
  if (!rows.length) return `<p class="empty">${empty}</p>`;
  const max = Math.max(1, ...rows.map((r) => r.n)), total = rows.reduce((a, r) => a + r.n, 0);
  return `<ul class="rank">${rows.map((r) => `<li title="${esc(labelFn(r[key]))}: ${fmt(r.n)}">
    <span class="nm">${esc(labelFn(r[key]) ?? "–")}</span>
    <span class="tr"><i style="width:${Math.max(2, (100 * r.n) / max)}%"></i></span>
    <span class="n">${fmt(r.n)}<small>${Math.round((100 * r.n) / total)}%</small></span></li>`).join("")}</ul>`;
}

function meter(label, part, whole, detail) {
  const p = whole ? Math.round((100 * part) / whole) : null;
  return `<div class="meter"><div class="mh"><span>${label}</span><b>${p === null ? "–" : p + "%"}</b></div>
    <div class="tr" role="img" aria-label="${label}: ${p ?? 0}%"><i style="width:${p ?? 0}%"></i></div><div class="sub">${detail}</div></div>`;
}

const flag = (cc) => {
  try { return cc && /^[A-Z]{2}$/.test(cc) ? new Intl.DisplayNames(["en"], { type: "region" }).of(cc) : cc || "–"; }
  catch { return cc || "–"; }
};

async function dashboard(env) {
  const day = 86400, t = now();
  const q = (sql, ...a) => env.DB.prepare(sql).bind(...a);
  const [dl, dl7, dlDays, hits, uniq, hitDays, refs, countries, act1, act7, act30, starts, usage, versions, src] =
    await env.DB.batch([
      q("SELECT COUNT(*) n FROM downloads"),
      q("SELECT COUNT(*) n FROM downloads WHERE ts > ?", t - 7 * day),
      q("SELECT date(ts,'unixepoch') d, COUNT(*) n FROM downloads WHERE ts > ? GROUP BY d ORDER BY d", t - 30 * day),
      q("SELECT COUNT(*) n FROM hits WHERE ts > ?", t - 30 * day),
      q("SELECT COUNT(DISTINCT visitor || date(ts,'unixepoch')) n FROM hits WHERE ts > ?", t - 30 * day),
      q("SELECT date(ts,'unixepoch') d, COUNT(DISTINCT visitor) n FROM hits WHERE ts > ? GROUP BY d ORDER BY d", t - 30 * day),
      q("SELECT ref, COUNT(*) n FROM hits WHERE ts > ? AND ref IS NOT NULL GROUP BY ref ORDER BY n DESC LIMIT 8", t - 30 * day),
      q("SELECT country, COUNT(*) n FROM downloads WHERE country IS NOT NULL GROUP BY country ORDER BY n DESC LIMIT 8"),
      q("SELECT COUNT(DISTINCT install) n FROM app_events WHERE ts > ?", t - day),
      q("SELECT COUNT(DISTINCT install) n FROM app_events WHERE ts > ?", t - 7 * day),
      q("SELECT COUNT(DISTINCT install) n FROM app_events WHERE ts > ?", t - 30 * day),
      q("SELECT COUNT(*) n FROM app_events WHERE kind='start' AND ts > ?", t - 30 * day),
      q("SELECT data FROM app_events WHERE kind='usage' AND ts > ?", t - 30 * day),
      q("SELECT version, COUNT(DISTINCT install) n FROM app_events WHERE ts > ? GROUP BY version ORDER BY n DESC LIMIT 8", t - 30 * day),
      q("SELECT src, COUNT(*) n FROM downloads WHERE ts > ? GROUP BY src ORDER BY n DESC LIMIT 8", t - 30 * day),
    ]);
  const one = (r) => r.results?.[0]?.n || 0;
  const sum = {};
  for (const row of usage.results || []) {
    try { for (const [k, v] of Object.entries(JSON.parse(row.data))) if (typeof v === "number") sum[k] = (sum[k] || 0) + v; } catch {}
  }
  const s = (k) => sum[k] || 0;
  const found = s("found"), nf = s("not_found"), locked = s("sync_locked"), gave = s("sync_gave_up");
  const hours = Math.round(s("minutes") / 6) / 10;
  const tile = (label, value, sub) => `<div class="tile"><div class="tl">${label}</div><div class="tv">${value}</div><div class="sub">${sub}</div></div>`;
  const modes = [
    { m: "Taskbar", n: Math.round(s("mode_taskbar")) },
    { m: "Title bar", n: Math.round(s("mode_titlebar")) },
    { m: "Floating", n: Math.round(s("mode_float")) },
  ].filter((r) => r.n > 0).sort((a, b) => b.n - a.n);
  const updated = new Date().toUTCString().replace(" GMT", " UTC");

  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Lirico stats</title><link rel="icon" href="/assets/favicon.png">
<style>
:root{--bg:#f6f4ef;--card:#fffdf9;--ink:#17131f;--ink2:#5b5566;--mute:#8a8494;--line:#e6e1d8;--grid:#eeeae2;--acc:#14a77c;--acc-soft:#d7f2e8;--pink:#ff5fa2}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f15;--card:#151924;--ink:#f2f4f8;--ink2:#b4bac8;--mute:#8a90a0;--line:#232838;--grid:#1d2230;--acc:#7ee0c3;--acc-soft:#1d3a33;--pink:#ff7fb6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:12px}.brand img{width:40px;height:40px;border-radius:11px}
h1{font-size:24px;margin:0;letter-spacing:-.02em}h1 span{color:var(--mute);font-weight:500}
.meta{color:var(--mute);font-size:13px}.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:99px;background:var(--acc-soft);color:var(--ink);font-size:12.5px}
.pill:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--acc)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:26px}
.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:16px}
.tile{padding:16px 18px}.tl{color:var(--ink2);font-size:13px}.tv{font-size:32px;font-weight:650;letter-spacing:-.03em;line-height:1.15;margin-top:4px}.sub{color:var(--mute);font-size:12.5px;margin-top:2px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;color:var(--mute);font-weight:600;margin:30px 0 12px}
.grid2{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
@media (max-width:860px){.grid2,.grid3{grid-template-columns:minmax(0,1fr)}}
.card{padding:18px 20px}.card h3{font-size:15px;margin:0 0 10px;font-weight:600}.card h3 small{color:var(--mute);font-weight:400;margin-left:6px}
.chart{width:100%;height:auto;display:block;overflow:visible}
.chart .grid{stroke:var(--grid);stroke-width:1}.chart .base{stroke:var(--line);stroke-width:1.5}
.chart .ax{fill:var(--mute);font-size:11px}.chart .val{fill:var(--ink);font-size:12px;font-weight:600}
.chart .bar{fill:var(--acc)}.chart .hit{fill:transparent;cursor:default}.chart .hit:hover{fill:var(--ink);fill-opacity:.05}
.rank{list-style:none;margin:0;padding:0}.rank li{display:grid;grid-template-columns:minmax(70px,1fr) 1.3fr auto;gap:12px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
.rank li:last-child{border-bottom:0}.nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tr{height:8px;border-radius:4px;background:var(--grid);overflow:hidden}.tr i{display:block;height:100%;border-radius:4px;background:var(--acc)}
.n{font-variant-numeric:tabular-nums;font-weight:600;text-align:right;min-width:62px}.n small{color:var(--mute);font-weight:400;margin-left:6px;font-size:11.5px}
.meter{padding:6px 0 12px}.mh{display:flex;justify-content:space-between;align-items:baseline}.mh b{font-size:26px;letter-spacing:-.02em}.meter .tr{height:10px;margin:6px 0 4px}
.empty{color:var(--mute);margin:8px 0}
.tbl{margin-top:8px;font-size:13px;color:var(--ink2)}.tbl summary{cursor:pointer;color:var(--mute)}.tbl table{border-collapse:collapse;margin-top:6px}.tbl td,.tbl th{padding:2px 14px 2px 0;text-align:left}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);font-size:12.5px;padding:5px 9px;border-radius:8px;opacity:0;transition:opacity .1s;white-space:nowrap;z-index:9}
footer{color:var(--mute);font-size:12.5px;margin-top:34px}
</style></head><body><div class="wrap">
<header><div class="brand"><img src="/assets/logo.svg" alt=""><div><h1>Lirico <span>stats</span></h1><div class="meta">Last 30 days unless noted · updated ${esc(updated)}</div></div></div>
<span class="pill">No personal data · no IPs stored</span></header>

<div class="tiles">
${tile("Downloads", fmt(one(dl)), `all time · +${fmt(one(dl7))} this week`)}
${tile("Active installs", fmt(one(act7)), `last 7 days · ${fmt(one(act1))} today · ${fmt(one(act30))} in 30 d`)}
${tile("Website visitors", fmt(one(uniq)), `unique per day · ${fmt(one(hits))} page views`)}
${tile("App launches", fmt(one(starts)), "starts of Lirico")}
${tile("Lyrics on screen", fmt(hours) + " h", `${fmt(Math.round(s("songs")))} songs played`)}
${tile("Lyrics found", found + nf ? Math.round((100 * found) / (found + nf)) + "%" : "–", `${fmt(found)} found · ${fmt(nf)} missing`)}
</div>

<h2>Downloads</h2>
<div class="grid2"><div class="card"><h3 id="c1">Downloads per day<small>30 days</small></h3>${columns(fillDays(dlDays.results, 30), "downloads", "c1")}</div>
<div class="card"><h3>Where downloads come from</h3>${ranked(countries.results || [], "country", "No downloads yet", flag)}
<h3 style="margin-top:18px">Which button<small>30 days</small></h3>${ranked(src.results || [], "src", "No downloads yet")}</div></div>

<h2>Website</h2>
<div class="grid2"><div class="card"><h3 id="c2">Visitors per day<small>30 days</small></h3>${columns(fillDays(hitDays.results, 30), "visitors", "c2")}</div>
<div class="card"><h3>Top referrers</h3>${ranked(refs.results || [], "ref", "No referrers yet (direct visits only)")}</div></div>

<h2>The app</h2>
<div class="grid3">
<div class="card"><h3>How well it works</h3>
${meter("Lyrics found", found, found + nf, `${fmt(found)} of ${fmt(found + nf)} songs`)}
${meter("Auto-sync locked on", locked, locked + gave, `${fmt(locked)} locked · ${fmt(gave)} gave up`)}</div>
<div class="card"><h3>Where people keep the lyrics<small>minutes</small></h3>${ranked(modes, "m", "No usage yet")}</div>
<div class="card"><h3>Versions in use<small>installs</small></h3>${ranked(versions.results || [], "version", "No installs reporting yet", (v) => "v" + v)}</div>
</div>
<footer>Counts only. Lirico never sends song names, lyrics or audio; installs are random ids linked to nobody.</footer>
</div><div id="tip"></div>
<script>
const tip=document.getElementById("tip");
document.querySelectorAll(".hit").forEach(h=>{h.addEventListener("mousemove",e=>{tip.textContent=h.dataset.tip;tip.style.opacity=1;
const x=Math.min(e.clientX+12,innerWidth-tip.offsetWidth-8);tip.style.left=x+"px";tip.style.top=(e.clientY-34)+"px"});
h.addEventListener("mouseleave",()=>tip.style.opacity=0)});
</script></body></html>`;
}
