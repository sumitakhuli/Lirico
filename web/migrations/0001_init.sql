-- Lirico analytics. No IPs, no cookies, no song titles are stored.
CREATE TABLE IF NOT EXISTS downloads (
  ts INTEGER NOT NULL,          -- unix seconds
  country TEXT,                 -- 2-letter code from Cloudflare
  src TEXT                      -- where the click came from (site, readme, ...)
);
CREATE INDEX IF NOT EXISTS downloads_ts ON downloads (ts);

CREATE TABLE IF NOT EXISTS hits (
  ts INTEGER NOT NULL,
  path TEXT,
  ref TEXT,                     -- referrer host only
  country TEXT,
  visitor TEXT                  -- daily-rotating hash: counts unique visitors per day, can't track across days
);
CREATE INDEX IF NOT EXISTS hits_ts ON hits (ts);

CREATE TABLE IF NOT EXISTS app_events (
  ts INTEGER NOT NULL,
  install TEXT,                 -- random id made by the app; not linked to any person
  version TEXT,
  os TEXT,
  kind TEXT,                    -- 'start' | 'usage'
  data TEXT,                    -- small JSON of counters
  country TEXT
);
CREATE INDEX IF NOT EXISTS app_events_ts ON app_events (ts);
CREATE INDEX IF NOT EXISTS app_events_install ON app_events (install);
