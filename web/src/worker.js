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

    // --- downloads: count, then hand over to GitHub Releases
    if (url.pathname === "/download") {
      ctx.waitUntil(
        env.DB.prepare("INSERT INTO downloads (ts, country, src) VALUES (?, ?, ?)")
          .bind(now(), country, clip(url.searchParams.get("src") || "direct", 24)).run()
      );
      return Response.redirect(EXE_URL, 302);
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
      return new Response(await dashboard(env), { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
    }

    return env.ASSETS.fetch(request);
  },
};

// ------------------------------------------------------------------ dashboard
async function dashboard(env) {
  const day = 86400, t = now();
  const q = (sql, ...a) => env.DB.prepare(sql).bind(...a);
  const [dl, dl7, dlDays, hits, uniq, hitDays, refs, countries, act1, act7, act30, starts, usage, versions] =
    await env.DB.batch([
      q("SELECT COUNT(*) n FROM downloads"),
      q("SELECT COUNT(*) n FROM downloads WHERE ts > ?", t - 7 * day),
      q("SELECT date(ts,'unixepoch') d, COUNT(*) n FROM downloads WHERE ts > ? GROUP BY d ORDER BY d", t - 14 * day),
      q("SELECT COUNT(*) n FROM hits WHERE ts > ?", t - 30 * day),
      q("SELECT COUNT(DISTINCT visitor || date(ts,'unixepoch')) n FROM hits WHERE ts > ?", t - 30 * day),
      q("SELECT date(ts,'unixepoch') d, COUNT(DISTINCT visitor) n FROM hits WHERE ts > ? GROUP BY d ORDER BY d", t - 14 * day),
      q("SELECT ref, COUNT(*) n FROM hits WHERE ts > ? AND ref IS NOT NULL GROUP BY ref ORDER BY n DESC LIMIT 8", t - 30 * day),
      q("SELECT country, COUNT(*) n FROM downloads WHERE country IS NOT NULL GROUP BY country ORDER BY n DESC LIMIT 8"),
      q("SELECT COUNT(DISTINCT install) n FROM app_events WHERE ts > ?", t - day),
      q("SELECT COUNT(DISTINCT install) n FROM app_events WHERE ts > ?", t - 7 * day),
      q("SELECT COUNT(DISTINCT install) n FROM app_events WHERE ts > ?", t - 30 * day),
      q("SELECT COUNT(*) n FROM app_events WHERE kind='start' AND ts > ?", t - 30 * day),
      q("SELECT data FROM app_events WHERE kind='usage' AND ts > ?", t - 30 * day),
      q("SELECT version, COUNT(DISTINCT install) n FROM app_events WHERE ts > ? GROUP BY version ORDER BY n DESC", t - 30 * day),
    ]);
  const one = (r) => r.results?.[0]?.n || 0;
  const sum = {};
  for (const row of usage.results || []) {
    try { for (const [k, v] of Object.entries(JSON.parse(row.data))) if (typeof v === "number") sum[k] = (sum[k] || 0) + v; } catch {}
  }
  const pct = (a, b) => (b ? Math.round((100 * a) / b) + "%" : "–");
  const bars = (rows) => {
    const max = Math.max(1, ...rows.map((r) => r.n));
    return rows.map((r) => `<div class=b><span>${r.d.slice(5)}</span><i style="width:${(100 * r.n) / max}%"></i><b>${r.n}</b></div>`).join("") || "<p class=m>No data yet</p>";
  };
  const list = (rows, key) => rows.map((r) => `<li><span>${r[key] ?? "–"}</span><b>${r.n}</b></li>`).join("") || "<li class=m>No data yet</li>";
  const card = (label, value, sub = "") => `<div class=c><div class=l>${label}</div><div class=v>${value}</div><div class=m>${sub}</div></div>`;
  return `<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Lirico stats</title>
<style>body{margin:0;background:#0d0f15;color:#f2f4f8;font:15px/1.5 system-ui,Segoe UI,sans-serif;padding:28px}
h1{font-weight:600;margin:0 0 4px}h2{font-size:14px;color:#7ee0c3;font-weight:500;margin:34px 0 12px;text-transform:uppercase;letter-spacing:.08em}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.c{background:#151924;border:1px solid #232838;border-radius:14px;padding:16px}
.l{color:#8a90a0;font-size:13px}.v{font-size:30px;font-weight:600}.m{color:#8a90a0;font-size:12.5px}
.b{display:grid;grid-template-columns:52px 1fr 40px;gap:10px;align-items:center;margin:4px 0}.b i{display:block;height:12px;border-radius:6px;background:#7ee0c3}.b span,.b b{font-size:12.5px;color:#8a90a0}
ul{list-style:none;padding:0;margin:0}li{display:flex;justify-content:space-between;border-bottom:1px solid #1f2430;padding:6px 0}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}</style>
<h1>Lirico stats</h1><div class=m>Updated live · last 30 days unless noted · no personal data stored</div>
<h2>Downloads</h2><div class=g>${card("All time", one(dl))}${card("Last 7 days", one(dl7))}</div>
<div class=two><div><h2>Downloads per day</h2>${bars(dlDays.results || [])}</div><div><h2>Download countries</h2><ul>${list(countries.results || [], "country")}</ul></div></div>
<h2>Website</h2><div class=g>${card("Page views", one(hits))}${card("Unique visitors", one(uniq), "per day, summed")}</div>
<div class=two><div><h2>Visitors per day</h2>${bars(hitDays.results || [])}</div><div><h2>Top referrers</h2><ul>${list(refs.results || [], "ref")}</ul></div></div>
<h2>App usage</h2><div class=g>${card("Active today", one(act1), "installs")}${card("Active 7 days", one(act7))}${card("Active 30 days", one(act30))}${card("App launches", one(starts))}
${card("Hours of lyrics", Math.round((sum.minutes || 0) / 6) / 10, "time lyrics were on screen")}${card("Songs played", sum.songs || 0)}
${card("Lyrics found", pct(sum.found || 0, (sum.found || 0) + (sum.not_found || 0)), `${sum.found || 0} found · ${sum.not_found || 0} not found`)}
${card("Auto-sync locked", pct(sum.sync_locked || 0, (sum.sync_locked || 0) + (sum.sync_gave_up || 0)), `${sum.sync_locked || 0} locked · ${sum.sync_gave_up || 0} gave up`)}
${card("Taskbar / title bar / floating", `${sum.mode_taskbar || 0} / ${sum.mode_titlebar || 0} / ${sum.mode_float || 0}`, "minutes in each position")}</div>
<h2>Versions</h2><ul>${list(versions.results || [], "version")}</ul>`;
}
