import React, { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  Pie,
  PieChart,
  Rectangle,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import roleBadgesUrl from "./assets/role_sprites.png";
import boosterBadgesUrl from "./assets/booster_sprites.png";

// Glyph sprite sheets: transparent-background glyphs, one per grid cell,
// centered and sized uniformly (see scratchpad process_sprites.py, which
// un-blended the original navy background and recentered each cell).
// Both sheets are in id order (row-major).
const SpriteBadge = ({ url, cols, rows, index, size, className }) => {
  const idx = Number(index);
  if (!Number.isFinite(idx) || idx < 0 || idx >= cols * rows) return null;
  const col = idx % cols;
  const row = Math.floor(idx / cols);
  // Geometry goes through CSS variables so stylesheet rules can render the
  // sprite either as a normal image (default) or as a mask filled with a flat
  // medal color (gold/silver/bronze cards) — inline background-* would win
  // over any stylesheet override.
  return (
    <span
      className={className}
      style={{
        width: size,
        height: size,
        "--sprite-url": `url(${url})`,
        "--sprite-size": `${cols * 100}% ${rows * 100}%`,
        "--sprite-pos": `${(col * 100) / (cols - 1)}% ${(row * 100) / (rows - 1)}%`,
      }}
    />
  );
};

// role_sprites.png — 4x3, role-id order: 0 crosshair (Main AWP), 1 soldier
// (Support), 2 AK-on-T (Attacker), 3 IGL helmet (Leader), 4 rising chart
// (Stathunter), 5 bullets (Entry Fragger), 6 tent+campfire (Camper), 7 CT
// eagle (Defender), 8 headshot burst (HS Machine), 9 falling chart (Noob),
// 10 three skulls (Multi Fragger), 11 UMP (Eco Friendly).
const RoleBadge = ({ roleId, size = 68 }) => (
  <SpriteBadge url={roleBadgesUrl} cols={4} rows={3} index={Number(roleId)} size={size} className="role-badge" />
);

const ROLE_NAMES = {
  0: "Main AWP",
  1: "Support",
  2: "Attacker",
  3: "Leader",
  4: "Stathunter",
  5: "Entry Fragger",
  6: "Camper",
  7: "Defender",
  8: "HS Machine",
  9: "Noob",
  10: "Multi Fragger",
  11: "Eco Friendly",
};

// Reverse lookup for role badges when only a role NAME is stored (normalized
// to bare letters so "Entry Fragger" / "entry_fragger" both resolve).
const ROLE_ID_BY_NAME = Object.fromEntries(
  Object.entries(ROLE_NAMES).map(([id, name]) => [String(name).toLowerCase().replace(/[^a-z]/g, ""), Number(id)])
);
const roleIdFromName = (roleName) => {
  const key = String(roleName || "").toLowerCase().replace(/[^a-z]/g, "");
  return key in ROLE_ID_BY_NAME ? ROLE_ID_BY_NAME[key] : null;
};

// Short round labels for the playoff EV per-round splits / reach odds.
const STAGE_SHORT = {
  round_of_32: "R32",
  round_of_16: "R16",
  quarters: "QF",
  semis: "SF",
  final: "Final",
  third_place: "3rd",
  ub_r1: "UB R1",
  ub_sf: "UB SF",
  ub_final: "UB Final",
  lb_r1: "LB R1",
  lb_sf: "LB SF",
  lb_final: "LB Final",
  cons_final: "Cons. Final",
  grand_final: "Grand Final",
  bye_r3: "Bye R3",
  bye_r5: "Bye R5",
};
const STAGE_FULL = {
  round_of_32: "Round of 32",
  round_of_16: "Round of 16",
  quarters: "Quarter-final",
  semis: "Semi-final",
  final: "Final",
  third_place: "3rd place",
  ub_r1: "Upper round 1",
  ub_sf: "Upper semi-finals",
  ub_final: "Upper final",
  lb_r1: "Lower round 1",
  lb_sf: "Lower semi-finals",
  lb_final: "Lower final",
  cons_final: "Consolidation final",
  grand_final: "Grand final",
  bye_r3: "Round 3 bye",
  bye_r5: "Round 5 bye",
};

// booster_sprites.png — 5x4 (18 icons, last two cells empty), booster-id order
// EXCEPT cells 14/15: the sheet draws shot-in-the-back at cell 14 and the
// arms-raised hero at 15, which semantically are Cannon fodder (15) and
// Carry (14) respectively — hence the swap. 0 crossed pistols (Best Pistol
// Round), 1 red down arrow (Bottom of scoreboard), 2 CLUTCH, 3 green up arrow
// (Top of scoreboard), 4 trade arrows (Avenger), 5 fish hook (Bait), 6 knife
// (Rambo), 7 flash burst (Flash), 8 scales (Mister consistent), 9 grenade
// (Kobe), 10 runner (Saver), 11 helping-up (Assist), 12 robot (Aim bot),
// 13 4x skull (Quad), 16 UZI+dollars (Farmer), 17 flame (Hellcase).
const BOOSTER_BADGE_SPRITE = { 14: 15, 15: 14 };
const BoosterBadge = ({ boosterId, size = 68 }) => {
  const id = Number(boosterId);
  const idx = BOOSTER_BADGE_SPRITE[id] !== undefined ? BOOSTER_BADGE_SPRITE[id] : id;
  return <SpriteBadge url={boosterBadgesUrl} cols={5} rows={4} index={idx} size={size} className="role-badge booster-badge" />;
};

// The backend picks a free port at startup and memorizes it; inside Electron the
// preload hands us the resolved base URL. The 8000 fallback only applies when
// the renderer runs in a plain browser (Vite dev) with no preload.
const API_BASE =
  (typeof window !== "undefined" && window.api?.baseUrl) || import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

// Cached HLTV images served by the backend (see backend/services/image_cache).
// Both components fall back to an initials chip when the image isn't cached.
const ASSETS_BASE = `${API_BASE}/assets`;

// The three kinds of points a Top 5 roster player carries (backend
// services/roster_scores.py), each split into rating / win / role / booster /
// total: `average` over every outcome, `outcome` in one specific outcome (the
// roster's best one, or the completed one the operator picked), `winning`
// over the outcomes the roster wins. A ranking mode shows exactly one kind.
const POINTS_KIND_BY_MODE = { average: "average", single_outcome: "outcome", most_outcomes: "winning", completed: "outcome" };
const NO_POINTS = { rating: null, win: null, role: null, booster: null, total: 0 };
const rosterPoints = (player, mode) => {
  const kind = POINTS_KIND_BY_MODE[mode] || "average";
  const value = player?.[kind] || player?.average || NO_POINTS;
  // A kind stored as a total alone (no split) shows the average's split.
  if (value.rating == null && kind !== "average" && player?.average) return { ...player.average, total: value.total };
  return value;
};
const POINTS_PARTS = [
  ["Rating", "rating"],
  ["Win", "win"],
  ["Role", "role"],
  ["Boost", "booster"],
];

const initialsOf = (name) => {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return `${parts[0][0] || ""}${parts[1][0] || ""}`.toUpperCase();
  return String(name || "?").slice(0, 2).toUpperCase();
};

// Stored dates are ISO (YYYY-MM-DD); the UI shows day/month/year.
const formatDMY = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || "").trim());
  if (m) return `${m[3]}/${m[2]}/${m[1]}`;
  return String(iso || "").trim() || "-";
};

function TeamLogo({ hltvTeamId, name, size = 22, className = "" }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [hltvTeamId]);
  const id = Number(hltvTeamId);
  if (!Number.isFinite(id) || id <= 0 || failed) {
    return (
      <span className={`img-fallback ${className}`} style={{ width: size, height: size, fontSize: Math.max(8, size * 0.36) }}>
        {initialsOf(name)}
      </span>
    );
  }
  return (
    <img
      className={`team-logo-img ${className}`}
      src={`${ASSETS_BASE}/team/${id}.png?v=2`}
      width={size}
      height={size}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

function PlayerPhoto({ playerId, name, size = 26, className = "" }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [playerId]);
  const id = Number(playerId);
  if (!Number.isFinite(id) || id <= 0 || failed) {
    return (
      <span className={`img-fallback round ${className}`} style={{ width: size, height: size, fontSize: Math.max(8, size * 0.36) }}>
        {initialsOf(name)}
      </span>
    );
  }
  return (
    <img
      className={`player-photo-img ${className}`}
      src={`${ASSETS_BASE}/player/${id}.jpg?v=2`}
      width={size}
      height={size}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

// Roster cards are paged rather than tabled: this many per page.
const COMBO_PAGE_SIZE = 10;
// Rosters are always listed by score, best first (sort_key "ev_desc").
function ComboPager({ page, pageSize, total, onPage }) {
  const count = Number(total || 0);
  const pages = Math.max(1, Math.ceil(count / pageSize));
  const first = count > 0 ? page * pageSize + 1 : 0;
  const last = Math.min(count, (page + 1) * pageSize);
  return (
    <div className="combo-pager">
      <button className="secondary" onClick={() => onPage(Math.max(0, page - 1))} disabled={page <= 0}>
        Prev
      </button>
      <span className="muted">
        {first.toLocaleString()}–{last.toLocaleString()} of {count.toLocaleString()} · page {page + 1} of {pages}
      </span>
      <button className="secondary" onClick={() => onPage(page + 1)} disabled={page + 1 >= pages}>
        Next
      </button>
    </div>
  );
}

function BreakdownHero({ playerId, name, teamName, hltvTeamId, roleId, stats, total, note = null, decimals = 2 }) {
  return (
    <div className="breakdown-hero">
      <div className="breakdown-id">
        <div className="breakdown-id-photo">
          <PlayerPhoto playerId={playerId} name={name} size={68} />
          {teamName ? (
            <span className="breakdown-id-logo">
              <TeamLogo hltvTeamId={hltvTeamId} name={teamName} size={22} />
            </span>
          ) : null}
        </div>
        <div className="breakdown-id-text">
          <div className="breakdown-id-name">{name}</div>
          <div className="breakdown-id-meta">
            {teamName ? <span className="breakdown-id-team">{teamName}</span> : null}
            {teamName && roleId != null ? <span className="breakdown-id-sep">·</span> : null}
            {roleId != null ? (
              <span className="breakdown-id-role">
                <RoleBadge roleId={roleId} size={18} />
                {ROLE_NAMES[roleId] || `Role ${roleId}`}
              </span>
            ) : null}
            {note ? (
              <>
                <span className="breakdown-id-sep">·</span>
                <span className="breakdown-id-note">{note}</span>
              </>
            ) : null}
          </div>
        </div>
      </div>
      <div className="breakdown-stats">
        {stats.map(([label, value]) => (
          <div className="breakdown-stat" key={label}>
            <div className="breakdown-stat-value">{Number(value || 0).toFixed(decimals)}</div>
            <div className="breakdown-stat-label">{label}</div>
          </div>
        ))}
        <div className="breakdown-stat total">
          <div className="breakdown-stat-value">{Number(total || 0).toFixed(decimals)}</div>
          <div className="breakdown-stat-label">Total</div>
        </div>
      </div>
    </div>
  );
}

const APP_INFO = (typeof window !== "undefined" && window.api?.appInfo) || null;
// Public build: the installed app distributed from the website (see
// electron/main.js). Operator-only tabs and every control that scrapes HLTV
// or rewrites the stored runs are hidden; the backend refuses them anyway
// (backend/services/public_access.py). `?public` on the dev URL previews it.
// PUBLIC_BUILD_STATIC never changes: this is the installed app (sign-in,
// updater, server probe). PUBLIC_BUILD is the *view*: true for ordinary users
// of the installed app, false in the operator checkout and false again once
// an admin account signs in, which unlocks every operator tab and control
// (the backend treats an admin session as the operator too).
const PUBLIC_BUILD_STATIC =
  Boolean(APP_INFO?.publicBuild) || (typeof window !== "undefined" && new URLSearchParams(window.location.search).has("public"));
let PUBLIC_BUILD = PUBLIC_BUILD_STATIC;
const setAdminView = (isAdmin) => {
  PUBLIC_BUILD = PUBLIC_BUILD_STATIC && !isAdmin;
};
const APP_VERSION = APP_INFO?.version || "dev";
const ALL_TABS = [
  { key: "view", label: "Database" },
  { key: "events", label: "Events" },
  { key: "tournament", label: "Tournament" },
  { key: "live", label: "Live" },
  { key: "season", label: "Season" },
  { key: "scheduling", label: "Scheduling" },
  { key: "users", label: "Users" },
  { key: "settings", label: "Settings" },
];
const PUBLIC_TAB_KEYS = ["view", "events", "tournament", "live", "season", "settings"];
const visibleTabs = () => (PUBLIC_BUILD ? ALL_TABS.filter((t) => PUBLIC_TAB_KEYS.includes(t.key)) : ALL_TABS);
const NOT_PUBLISHED = "Valuations for this event have not been published yet.";

// Update prompt in the title bar (public build): electron-updater downloads in
// the background and the user restarts when it is ready.
// One subscription to the updater's status, shared by the title-bar banner
// and the in-app prompt.
const updateStatusListeners = new Set();
let latestUpdateStatus = null;
if (typeof window !== "undefined" && window.api?.onUpdateStatus) {
  window.api.onUpdateStatus((s) => {
    latestUpdateStatus = s;
    updateStatusListeners.forEach((fn) => fn(s));
  });
  // Lets a preview or a test drive the prompt without a real release.
  window.__setUpdateStatus = (s) => {
    latestUpdateStatus = s;
    updateStatusListeners.forEach((fn) => fn(s));
  };
}
const useUpdateStatus = () => {
  const [status, setStatus] = useState(latestUpdateStatus);
  useEffect(() => {
    updateStatusListeners.add(setStatus);
    return () => updateStatusListeners.delete(setStatus);
  }, []);
  return status;
};

// Shown once when an update has been downloaded; "Later" leaves the title-bar
// button, and the update still installs when the app is next closed.
function UpdateModal() {
  const status = useUpdateStatus();
  const [dismissedVersion, setDismissedVersion] = useState(null);
  if (!status || dismissedVersion === (status.version || "?")) return null;
  if (status.status === "blocked") {
    return (
      <div className="modal-backdrop update-modal-backdrop">
        <div className="update-modal">
          <p className="server-gate-kicker">Update blocked</p>
          <h1>Windows would not run the update</h1>
          <p>
            Smart App Control on this PC refused to start the installer for version {status.version || ""} because the
            app is not code-signed yet. You can keep using this version; a signed release will install normally.
          </p>
          <div className="server-gate-actions">
            <button className="primary" onClick={() => setDismissedVersion(status.version || "?")}>
              OK
            </button>
          </div>
        </div>
      </div>
    );
  }
  if (status.status !== "downloaded") return null;
  return (
    <div className="modal-backdrop update-modal-backdrop">
      <div className="update-modal">
        <p className="server-gate-kicker">Update ready</p>
        <h1>Version {status.version || ""} is ready to install</h1>
        <p>Restart now and it installs in a few seconds, or keep working and it installs when you next close the app.</p>
        <div className="server-gate-actions">
          <button className="primary" onClick={() => window.api?.installUpdate?.()}>
            Restart now
          </button>
          <button onClick={() => setDismissedVersion(status.version || "?")}>Later</button>
        </div>
      </div>
    </div>
  );
}

function UpdateBanner() {
  const status = useUpdateStatus();
  if (!status) return null;
  if (status.status === "downloaded") {
    return (
      <button type="button" className="titlebar-update" onClick={() => window.api?.installUpdate?.()}>
        Update {status.version ? `v${status.version} ` : ""}ready · restart to install
      </button>
    );
  }
  if (status.status === "downloading") {
    return <span className="titlebar-update muted">Downloading update {status.percent ? `${status.percent}%` : ""}</span>;
  }
  if (status.status === "blocked") {
    return <span className="titlebar-update muted">Update {status.version ? `v${status.version} ` : ""}blocked by Windows</span>;
  }
  return null;
}

const ACTIVE_MAP_POOL = ["Mirage", "Inferno", "Nuke", "Ancient", "Anubis", "Dust2", "Cache"];

// Map identity colors — vibrant takes on the familiar map hues (user-tuned:
// Inferno is dark blue, per HLTV's scheme). Identity never relies on color
// alone — every bar/card is labeled with its map.
const MAP_BAR_COLORS = {
  Dust2: "#eab308", // vivid gold
  Inferno: "#3e63dd", // dark royal blue
  Nuke: "#f4694b", // hot terracotta
  Ancient: "#2fbf71", // emerald
  Anubis: "#ec4899", // hot magenta
  Mirage: "#a855f7", // electric purple
  Cache: "#38bdf8", // bright sky blue
  Overpass: "#14b8a6", // vivid teal
  Vertigo: "#818cf8", // indigo
  Train: "#94a3b8", // railyard steel
};
const MAP_BAR_FALLBACK_COLOR = "#64748b";

const parseJsonSafe = async (res) => {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
};

const requestJson = async (path, init = {}, timeoutMs = 60000) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, signal: controller.signal });
  } catch (e) {
    if (e?.name === "AbortError") {
      throw new Error("Request timed out — the backend may still be working. Wait a moment and retry.");
    }
    throw e;
  } finally {
    clearTimeout(timeout);
  }
  const data = await parseJsonSafe(res);
  if (!res.ok) {
    const detail = data?.detail || `HTTP ${res.status}`;
    throw new Error(String(detail));
  }
  return data;
};

// Public build: the event the user is viewing (null = follow the server's
// active event). Tournament-data requests carry it as ?event_id= so the
// backend serves that event's stored runs (see _event_key in the routers).
let VIEW_EVENT_ID = (() => {
  try {
    const v = Number(localStorage.getItem("cs.viewEventId"));
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
})();
const rememberViewEvent = (id) => {
  VIEW_EVENT_ID = id || null;
  try {
    if (id) localStorage.setItem("cs.viewEventId", String(id));
    else localStorage.removeItem("cs.viewEventId");
  } catch {
    /* per-viewer convenience only */
  }
};
const EVENT_SCOPED_PATH = /^\/(playoff|groups|simulate|best-team)(\/|$)/;
const withViewEvent = (path) => {
  if (!PUBLIC_BUILD_STATIC || !VIEW_EVENT_ID || !EVENT_SCOPED_PATH.test(path)) return path;
  return `${path}${path.includes("?") ? "&" : "?"}event_id=${VIEW_EVENT_ID}`;
};

const rawApi = window.api || {
  get: (path, timeoutMs) => requestJson(path, {}, timeoutMs),
  post: (path, body, timeoutMs) =>
    requestJson(
      path,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      timeoutMs
    ),
  delete: (path) =>
    requestJson(path, {
      method: "DELETE",
    }),
  // Server-sent events without the bridge (a plain browser): same contract
  // as the preload's subscribe.
  subscribe: (path, onEvent, onClose) => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(`${API_BASE}${path}`, { headers: { Accept: "text/event-stream" }, signal: controller.signal });
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let cut;
          while ((cut = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, cut);
            buffer = buffer.slice(cut + 2);
            let event = "message";
            let data = "";
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) event = line.slice(6).trim();
              else if (line.startsWith("data:")) data += line.slice(5).trim();
            }
            if (!data) continue;
            let parsed = data;
            try {
              parsed = JSON.parse(data);
            } catch {
              /* plain text */
            }
            onEvent({ event, data: parsed });
          }
        }
      } catch {
        /* dropped or aborted */
      }
      if (!controller.signal.aborted && typeof onClose === "function") onClose();
    })();
    return () => controller.abort();
  },
  openExternal: async (url) => {
    if (typeof window !== "undefined" && typeof window.open === "function") {
      window.open(url, "_blank", "noopener,noreferrer");
    }
    return { status: "ok" };
  },
};
const api = {
  ...rawApi,
  get: (path, timeoutMs) => rawApi.get(withViewEvent(path), timeoutMs),
  post: (path, body, timeoutMs) => rawApi.post(withViewEvent(path), body, timeoutMs),
  delete: (path) => rawApi.delete(withViewEvent(path)),
};


// The server's live stream (GET /live/stream): a `change` message names an
// event whose HLTV pages were just re-read and differ. onChange gets its
// data ({event_id, version, read_at, live_count}). The stream reconnects
// after a short pause whenever it drops; callers keep a slow poll as the
// fallback for when it cannot connect at all.
function useLiveStream(onChange) {
  const handler = useRef(onChange);
  handler.current = onChange;
  useEffect(() => {
    if (typeof api.subscribe !== "function") return undefined;
    let stop = null;
    let timer = null;
    let closed = false;
    const open = () => {
      stop = api.subscribe(
        "/live/stream",
        (msg) => {
          if (msg?.event === "change" && msg.data && typeof msg.data === "object") handler.current(msg.data);
        },
        () => {
          if (!closed) timer = setTimeout(open, 5000);
        }
      );
    };
    open();
    return () => {
      closed = true;
      clearTimeout(timer);
      if (stop) stop();
    };
  }, []);
}

// "0.1.2" vs "0.1.10" -> -1. Only digits matter.
const compareVersions = (a, b) => {
  const pa = String(a || "0").split(".").map((x) => parseInt(x, 10) || 0);
  const pb = String(b || "0").split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d < 0 ? -1 : 1;
  }
  return 0;
};

// Public build: is the server reachable, and is this app new enough for it?
// Probes /public/config on start and every 30 s. Anything but "ok" replaces
// the tabs with ServerGate, so an outage or an outdated app reads as a plain
// message instead of empty pages and failed requests.
const SERVER_PROBE_MS = 30000;
function useServerStatus() {
  const [state, setState] = useState({ status: PUBLIC_BUILD_STATIC ? "checking" : "ok", config: null, error: null, checkedAt: null, failures: 0 });
  const probe = useCallback(async () => {
    if (!PUBLIC_BUILD_STATIC) return;
    try {
      const cfg = await api.get("/public/config", 10000);
      const min = cfg?.min_client_version;
      const outdated = APP_VERSION !== "dev" && Boolean(min) && compareVersions(APP_VERSION, min) < 0;
      setState({ status: outdated ? "outdated" : "ok", config: cfg, error: null, checkedAt: Date.now(), failures: 0 });
    } catch (e) {
      setState((prev) => {
        const failures = (prev.failures || 0) + 1;
        // A single miss while running is tolerated (one slow probe should not
        // hide the app); on start, or after two misses in a row, say so.
        const unreachable = prev.status !== "ok" || failures >= 2;
        return { ...prev, status: unreachable ? "unreachable" : prev.status, error: String(e?.message || e), checkedAt: Date.now(), failures };
      });
    }
  }, []);
  useEffect(() => {
    if (!PUBLIC_BUILD_STATIC) return undefined;
    probe();
    const timer = setInterval(probe, SERVER_PROBE_MS);
    return () => clearInterval(timer);
  }, [probe]);
  return { ...state, retry: probe };
}

const apiHost = (() => {
  try {
    return new URL(API_BASE).host;
  } catch {
    return API_BASE;
  }
})();

// ---- Accounts (public build): Google sign-in through the server ------------
// The app opens the browser at the server's sign-in URL with a random state
// and a challenge; the server does Google; the app polls for its session
// token with the verifier (see backend/routes/auth.py). The token lives in
// Electron's safe storage and rides on every request as a bearer header.
const randomToken = (bytes = 32) => {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return btoa(String.fromCharCode(...arr)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};
const sha256Base64Url = async (text) => {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};
const AUTH_POLL_MS = 2000;
const AUTH_TIMEOUT_MS = 5 * 60 * 1000;

function useAuth(serverOk, required) {
  const [state, setState] = useState({ status: PUBLIC_BUILD_STATIC ? "checking" : "signed_in", user: null, error: null });
  // Bumped by "Check again" on the sign-in screen: re-runs the stored-session check.
  const [checkNonce, setCheckNonce] = useState(0);
  const pollRef = useRef(null);
  const stopPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  };
  useEffect(() => {
    if (!PUBLIC_BUILD_STATIC || !serverOk) return undefined;
    if (!required) {
      // The server is not asking for sign-in (no Google client configured yet).
      setState({ status: "signed_in", user: null, error: null });
      return undefined;
    }
    let cancelled = false;
    (async () => {
      if (!window.api?.auth?.get?.()) {
        window.api?.log?.({ auth: "no stored session; showing sign-in" });
        setState({ status: "signed_out", user: null, error: null });
        return;
      }
      // Confirm the stored session. A 401 means the server no longer knows
      // it (signed out elsewhere, expired): drop it. Anything else is the
      // server or the connection having a moment: retry a few times before
      // asking the user to sign in again, and keep the token regardless.
      let lastMessage = "";
      for (let attempt = 1; attempt <= 4 && !cancelled; attempt++) {
        try {
          const me = await api.get("/auth/me", 15000);
          setAdminView(Boolean(me?.user?.is_admin));
          window.api?.log?.({ auth: "stored session accepted", attempt, admin: Boolean(me?.user?.is_admin) });
          if (!cancelled) setState({ status: "signed_in", user: me?.user || null, error: null });
          return;
        } catch (e) {
          lastMessage = String(e?.message || e);
          if (/sign in|\b401\b/i.test(lastMessage)) {
            window.api?.log?.({ auth: "stored session rejected (401); clearing", attempt });
            window.api?.auth?.clear?.("rejected by server");
            if (!cancelled) setState({ status: "signed_out", user: null, error: null });
            return;
          }
          window.api?.log?.({ auth: "session check failed; will retry", attempt, message: lastMessage });
          await new Promise((r) => setTimeout(r, 3000 * attempt));
        }
      }
      if (cancelled) return;
      window.api?.log?.({ auth: "session check gave up; token kept", message: lastMessage });
      setState({ status: "signed_out", user: null, error: `Couldn't confirm your sign-in (${lastMessage}). Your session is still saved.`, retryable: true });
    })();
    return () => {
      cancelled = true;
    };
  }, [serverOk, required, checkNonce]);
  useEffect(() => () => stopPolling(), []);
  const signIn = async () => {
    stopPolling();
    const state = randomToken(24);
    const verifier = randomToken(32);
    const challenge = await sha256Base64Url(verifier);
    setState({ status: "signing_in", user: null, error: null });
    await window.api?.openExternal?.(`${API_BASE}/auth/google/start?state=${state}&challenge=${challenge}`);
    const started = Date.now();
    pollRef.current = setInterval(async () => {
      if (Date.now() - started > AUTH_TIMEOUT_MS) {
        stopPolling();
        setState({ status: "signed_out", user: null, error: "Sign-in timed out. Try again." });
        return;
      }
      try {
        const res = await api.post("/auth/poll", { state, verifier }, 10000);
        if (res?.status === "ok" && res.token) {
          stopPolling();
          await window.api?.auth?.set?.(res.token);
          setAdminView(Boolean(res.user?.is_admin));
          setState({ status: "signed_in", user: res.user || null, error: null });
        } else if (res?.status === "unknown") {
          stopPolling();
          setState({ status: "signed_out", user: null, error: "That sign-in expired. Try again." });
        }
      } catch {
        /* keep polling; the server gate handles a real outage */
      }
    }, AUTH_POLL_MS);
  };
  const cancelSignIn = () => {
    stopPolling();
    setState({ status: "signed_out", user: null, error: null });
  };
  const recheck = () => {
    setState((prev) => ({ ...prev, status: "checking", error: null }));
    setCheckNonce((n) => n + 1);
  };
  const signOut = async () => {
    try {
      await api.post("/auth/signout", {}, 10000);
    } catch {
      /* the token is dropped locally regardless */
    }
    await window.api?.auth?.clear?.("signed out");
    setAdminView(false);
    setState({ status: "signed_out", user: null, error: null });
  };
  return { ...state, signIn, cancelSignIn, signOut, recheck };
}

function SignInGate({ auth, configured }) {
  return (
    <div className="server-gate">
      <div className="server-gate-card">
        <p className="server-gate-kicker">Sign in</p>
        <h1>Sign in to continue</h1>
        {configured === false ? (
          <p>This server has no sign-in set up yet. Try again later.</p>
        ) : (
          <p>CS Fantasy Toolkit signs you in with your Google account. It keeps only your name, email and picture.</p>
        )}
        {auth.status === "signing_in" ? (
          <>
            <p className="muted">Finish signing in in your browser, then come back here.</p>
            <div className="server-gate-actions">
              <button onClick={auth.cancelSignIn}>Cancel</button>
            </div>
          </>
        ) : (
          <div className="server-gate-actions">
            {auth.retryable && (
              <button className="primary" onClick={auth.recheck}>
                Check again
              </button>
            )}
            <button className={auth.retryable ? "" : "primary"} onClick={auth.signIn} disabled={configured === false || auth.status === "checking"}>
              {auth.status === "checking" ? "Checking..." : "Continue with Google"}
            </button>
          </div>
        )}
        {auth.error && <p className="muted server-gate-meta">{auth.error}</p>}
      </div>
    </div>
  );
}

// ---- Users (operator / admin): every account, activity, admin toggle ------
const fmtWhen = (ts) => {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const mins = (Date.now() - d.getTime()) / 60000;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.round(mins)} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
};

function UsersTab({ notify, me }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState(null);
  const load = async () => {
    setError("");
    try {
      setData(await api.get("/admin/users", 30000));
    } catch (e) {
      setError(e?.message || "Could not load users.");
    }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, []);
  const toggleAdmin = async (u) => {
    if (me && Number(me.id) === Number(u.id) && u.is_admin) {
      notify("You can't remove your own admin access.");
      return;
    }
    setBusyId(u.id);
    try {
      await api.post(`/admin/users/${u.id}/admin`, { is_admin: !u.is_admin });
      await load();
    } catch (e) {
      notify(e?.message || "Could not update the account.");
    } finally {
      setBusyId(null);
    }
  };
  const users = data?.users || [];
  return (
    <div className="stack">
      <Section title="Users">
        {error && <p className="muted">{error}</p>}
        {data && (
          <div className="pills" style={{ flexWrap: "wrap", marginBottom: 12 }}>
            <div className="pill">{data.total} accounts</div>
            <div className="pill">{data.online_now} online now</div>
            <div className="pill">{data.active_7d} active in 7 days</div>
            <div className="pill">{data.admins} admins</div>
          </div>
        )}
        {data && users.length === 0 && <p className="muted">Nobody has signed in yet.</p>}
        {users.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>User</th>
                <th>Email</th>
                <th>Last seen</th>
                <th>First sign-in</th>
                <th>Sessions</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const online = (u.last_seen_at || 0) * 1000 > Date.now() - 10 * 60000;
                return (
                  <tr key={u.id}>
                    <td>
                      <span className="user-cell">
                        {u.picture ? <img src={u.picture} alt="" referrerPolicy="no-referrer" /> : <span className="user-avatar-fallback">{String(u.name || u.email || "?").slice(0, 1).toUpperCase()}</span>}
                        <span>{u.name || "-"}</span>
                        {online && <span className="online-dot" title="Online now" />}
                      </span>
                    </td>
                    <td>{u.email || "-"}</td>
                    <td title={u.last_seen_at ? new Date(u.last_seen_at * 1000).toLocaleString() : ""}>{fmtWhen(u.last_seen_at || u.last_login_at)}</td>
                    <td>{u.created_at ? new Date(u.created_at * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "-"}</td>
                    <td>
                      {u.active_sessions} active / {u.total_sessions}
                    </td>
                    <td>
                      <button className={u.is_admin ? "primary" : "secondary"} onClick={() => toggleAdmin(u)} disabled={busyId === u.id}>
                        {u.is_admin ? "Admin" : "Make admin"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Section>
    </div>
  );
}

function ServerGate({ status, config, error, checkedAt, retry }) {
  const [busy, setBusy] = useState(false);
  const onRetry = async () => {
    setBusy(true);
    try {
      await retry();
    } finally {
      setBusy(false);
    }
  };
  if (status === "checking") {
    return (
      <div className="server-gate">
        <div className="server-gate-card">
          <p className="server-gate-kicker">Connecting</p>
          <h1>Reaching the valuation server</h1>
          <p className="muted">{apiHost}</p>
        </div>
      </div>
    );
  }
  if (status === "outdated") {
    return (
      <div className="server-gate">
        <div className="server-gate-card">
          <p className="server-gate-kicker">Update required</p>
          <h1>This version is too old for the server</h1>
          <p>
            You have v{APP_VERSION}; the server needs v{config?.min_client_version} or newer. Updates download in the
            background and install when you restart the app.
          </p>
          <div className="server-gate-actions">
            <button className="primary" onClick={() => window.api?.checkForUpdates?.()}>
              Check for updates
            </button>
            {APP_INFO?.siteUrl && <button onClick={() => window.api?.openExternal?.(APP_INFO.siteUrl)}>Download from the website</button>}
          </div>
          <div className="server-gate-update">
            <UpdateBanner />
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="server-gate">
      <div className="server-gate-card">
        <p className="server-gate-kicker">Can't reach the server</p>
        <h1>The valuation server isn't responding</h1>
        <p>
          CS Fantasy Toolkit could not reach {apiHost}. It may be down for maintenance, or this PC may be offline. The app
          keeps trying every 30 seconds.
        </p>
        <div className="server-gate-actions">
          <button className="primary" onClick={onRetry} disabled={busy}>
            {busy ? "Checking..." : "Retry now"}
          </button>
        </div>
        <p className="muted server-gate-meta">
          Last checked {checkedAt ? new Date(checkedAt).toLocaleTimeString() : "just now"}
          {error ? ` \u00b7 ${error}` : ""}
        </p>
      </div>
    </div>
  );
}

// A component error used to unmount the whole page (blank window). Now it
// stays inside the tab that threw, is reported to the app log, and can be
// retried in place.
class TabErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    try {
      window.api?.reportError?.({ where: this.props.name || "tab", message: String(error?.message || error), stack: String(error?.stack || ""), component: String(info?.componentStack || "").slice(0, 2000) });
    } catch {
      /* reporting must never throw */
    }
  }
  render() {
    if (this.state.error) {
      return (
        <div className="card">
          <header>
            <h2>This page hit an error</h2>
          </header>
          <p className="muted">{String(this.state.error?.message || this.state.error)}</p>
          <div className="actions">
            <button className="primary" onClick={() => this.setState({ error: null })}>
              Try again
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
if (typeof window !== "undefined") {
  window.addEventListener("error", (e) => window.api?.reportError?.({ where: "window", message: String(e?.message || ""), stack: String(e?.error?.stack || "") }));
  window.addEventListener("unhandledrejection", (e) => window.api?.reportError?.({ where: "promise", message: String(e?.reason?.message || e?.reason || ""), stack: String(e?.reason?.stack || "") }));
}

const TabButton = ({ active, onClick, children }) => (
  <button className={active ? "tab active" : "tab"} onClick={onClick}>
    {children}
  </button>
);

const Section = ({ title, children }) => (
  <section className="card">
    <header>
      <h2>{title}</h2>
    </header>
    <div>{children}</div>
  </section>
);

const Input = ({ label, value, onChange, type = "text", placeholder = "", className = "" }) => (
  <label className={`field${className ? ` ${className}` : ""}`}>
    <span>{label}</span>
    <input value={value} onChange={(e) => onChange(e.target.value)} type={type} placeholder={placeholder} />
  </label>
);

// Text input with a suggestion dropdown filtered by the typed value. Optional
// `actions` add per-row buttons (e.g. Include/Exclude) that receive the name.
// onEnter (optional) gets the current matches when Enter is pressed, so a
// caller can accept a single match without a mouse.
const SuggestInput = ({ label, value, onChange, placeholder = "", suggestions = [], actions = null, onEnter = null }) => {
  const [open, setOpen] = useState(false);
  const q = String(value || "").trim().toLowerCase();
  const found = q ? suggestions.filter((s) => s.toLowerCase().includes(q)).slice(0, 8) : [];
  // A plain picker has nothing to offer once the box holds an exact entry;
  // a box with actions keeps the row so Include/Exclude stay clickable.
  const matches = actions ? found : found.filter((s) => s !== value);
  return (
    <label className="field suggest-field">
      <span>{label}</span>
      <input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && onEnter) {
            e.preventDefault();
            onEnter(matches);
            setOpen(false);
          }
        }}
        type="text"
        placeholder={placeholder}
      />
      {open && matches.length > 0 && (
        <div className="suggest-list">
          {matches.map((name) => (
            <div key={name} className="suggest-row">
              <button
                type="button"
                className="suggest-item"
                onMouseDown={(e) => {
                  e.preventDefault();
                  onChange(name);
                  setOpen(false);
                }}
              >
                {name}
              </button>
              {(actions || []).map((a) => (
                <button
                  key={a.key}
                  type="button"
                  className={`suggest-action ${a.key}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    a.onPick(name);
                    setOpen(false);
                  }}
                >
                  {a.label}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </label>
  );
};

const Select = ({ label, value, onChange, options }) => (
  <label className="field">
    <span>{label}</span>
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  </label>
);

const sortDirectionFor = (sortValue, ascValue, descValue) => {
  if (sortValue === ascValue) return "asc";
  if (sortValue === descValue) return "desc";
  return "";
};

const nextSortValue = (sortValue, ascValue, descValue, defaultDirection = "asc") => {
  if (sortValue === ascValue) return descValue;
  if (sortValue === descValue) return ascValue;
  return defaultDirection === "desc" ? descValue : ascValue;
};

const SortHeader = ({ children, sortValue, asc, desc, onChange, defaultDirection = "asc", title, className }) => {
  const direction = sortDirectionFor(sortValue, asc, desc);
  const arrow = direction === "asc" ? "↑" : direction === "desc" ? "↓" : "↕";
  return (
    <th title={title} className={className}>
      <button
        type="button"
        className="table-sort-button"
        onClick={() => onChange(nextSortValue(sortValue, asc, desc, defaultDirection))}
        aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}
      >
        {children} <span>{arrow}</span>
      </button>
    </th>
  );
};

const ROLE_NAME_MAP = {
  0: "Main AWP",
  1: "Support",
  2: "Attacker",
  3: "Leader",
  4: "Stathunter",
  5: "Entry Fragger",
  6: "Camper",
  7: "Defender",
  8: "HS Machine",
  9: "Noob",
  10: "Multi Fragger",
  11: "Eco Friendly",
};

const roleLabel = (roleName) => {
  const n = Number(roleName);
  if (Number.isFinite(n) && ROLE_NAME_MAP[n]) return ROLE_NAME_MAP[n];
  return roleName || "-";
};

const buildNiceStepAxis = (values, step = 0.05) => {
  const finite = (values || []).filter((value) => Number.isFinite(value));
  if (finite.length === 0) {
    return { domain: ["auto", "auto"], ticks: undefined };
  }

  let min = Math.min(...finite);
  let max = Math.max(...finite);
  const stepValue = Number(step) || 0.05;

  min = Math.floor(min / stepValue) * stepValue;
  max = Math.ceil(max / stepValue) * stepValue;
  if (min >= Math.min(...finite)) {
    min -= stepValue;
  }
  if (max <= Math.max(...finite)) {
    max += stepValue;
  }
  if (min === max) {
    min -= stepValue;
    max += stepValue;
  }

  min = Number(min.toFixed(4));
  max = Number(max.toFixed(4));

  const ticks = [];
  for (let value = min; value <= max + stepValue / 2; value += stepValue) {
    ticks.push(Number(value.toFixed(2)));
  }

  return {
    domain: [min, max],
    ticks,
  };
};

const buildPlayerValueRowsFromSimulation = (simResults, players) => {
  const playerById = new Map();
  (players || []).forEach((p) => {
    const pid = Number(p?.player_id);
    if (!Number.isFinite(pid) || pid <= 0) return;
    playerById.set(pid, p);
  });

  const raw = simResults && typeof simResults === "object" ? simResults : {};
  const teamMap = raw.teams && typeof raw.teams === "object" ? raw.teams : raw;
  const byId = new Map();

  Object.entries(teamMap || {}).forEach(([tid, teamData]) => {
    const playersData = teamData?.players || {};
    Object.entries(playersData).forEach(([pidRaw, comps]) => {
      const pid = Number(pidRaw);
      if (!Number.isFinite(pid) || pid <= 0) return;
      const player = playerById.get(pid) || {};
      const pointsCandidates = [
        Number(comps?.total_points),
        Number(comps?.total),
        Number(comps?.expected_total_points),
        Number(comps?.rating_points_total) +
          Number(comps?.win_points_total) +
          Number(comps?.role_points_total) +
          Number(comps?.booster_points_total),
      ];
      const points = pointsCandidates.find((v) => Number.isFinite(v));
      if (!Number.isFinite(points)) return;

      // the price the run was valued at (its input snapshot) over the players
      // table, which only holds the last imported event's prices; the run's
      // own team over the table's current one (a re-spelt team is a new row)
      const snap = raw.input_snapshot?.players?.[String(pid)];
      const price = Number(snap?.price ?? player?.price);
      byId.set(pid, {
        player_id: pid,
        name: player?.name || snap?.name || `Player ${pid}`,
        team_id: Number(tid || player?.team_id || 0),
        price: Number.isFinite(price) ? price : 0,
        points,
        raw_booster: Number(comps?.booster_points_total || comps?.booster || 0),
      });
    });
  });

  const rows = Array.from(byId.values())
    .filter((r) => Number.isFinite(r.price) && Number.isFinite(r.points))
    .sort((a, b) => b.points - a.points);

  if (rows.length === 0) return { rows: [], slope: 0, intercept: 0 };

  const xMean = rows.reduce((s, r) => s + r.price, 0) / rows.length;
  const yMean = rows.reduce((s, r) => s + r.points, 0) / rows.length;
  const num = rows.reduce((s, r) => s + (r.price - xMean) * (r.points - yMean), 0);
  const den = rows.reduce((s, r) => s + (r.price - xMean) ** 2, 0);
  const slope = den > 0 ? num / den : 0;
  const intercept = yMean - slope * xMean;

  const withDistance = rows.map((r) => {
    const onLine = intercept + slope * r.price;
    return {
      ...r,
      on_line: onLine,
      distance: r.points - onLine,
    };
  });

  return { rows: withDistance, slope, intercept };
};

const boosterOptionsForBreakdownRow = (row) => {
  const options = Array.isArray(row?.booster_options) && row.booster_options.length > 0
    ? row.booster_options
    : [
        {
          booster_id: row?.booster_id,
          booster_name: row?.booster_name,
          booster_trigger_rate: row?.booster_trigger_rate,
          booster_points: row?.booster_points,
        },
      ];
  return options
    .map((option) => {
      const boosterId = Number(option?.booster_id);
      const points = Number(option?.booster_points || 0);
      if (!Number.isFinite(boosterId) || boosterId < 0 || boosterId > 30 || points <= 0) return null;
      return {
        boosterId,
        boosterName: option.booster_name || `Booster ${boosterId}`,
        triggerRate: Number(option.booster_trigger_rate || 0),
        points,
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.points - a.points);
};

const assignRosterBoostersFromBreakdowns = (rosterBreakdowns) => {
  const slots = [];
  Object.entries(rosterBreakdowns || {}).forEach(([pidRaw, rows]) => {
    const playerId = Number(pidRaw);
    if (!Number.isFinite(playerId)) return;
    (rows || []).forEach((row, rowIdx) => {
      const options = boosterOptionsForBreakdownRow(row);
      if (options.length === 0) return;
      slots.push({
        key: `${playerId}:${rowIdx}`,
        playerId,
        rowIdx,
        matchNumber: Number(row?.match_number || 0),
        options,
      });
    });
  });

  let states = new Map([[0, { score: 0, choices: [] }]]);
  slots.forEach((slot) => {
    const nextStates = new Map(states);
    states.forEach((state, mask) => {
      slot.options.forEach((option) => {
        const bit = 1 << option.boosterId;
        if (mask & bit) return;
        const nextMask = mask | bit;
        const nextScore = state.score + option.points;
        const existing = nextStates.get(nextMask);
        if (!existing || nextScore > existing.score + 1e-9) {
          nextStates.set(nextMask, {
            score: nextScore,
            choices: [
              ...state.choices,
              {
                key: slot.key,
                playerId: slot.playerId,
                rowIdx: slot.rowIdx,
                matchNumber: slot.matchNumber,
                boosterId: option.boosterId,
                boosterName: option.boosterName,
                triggerRate: option.triggerRate,
                points: option.points,
              },
            ],
          });
        }
      });
    });
    states = nextStates;
  });

  let best = { score: 0, choices: [] };
  states.forEach((state) => {
    if (state.score > best.score + 1e-9) best = state;
  });
  return best.choices;
};

// Roster context for the shared modal: the roster's assigned role with its
// trigger rates (from the player's roles JSON) and the roster's booster plan
// rows for this player.
const rosterRoleContext = (players, ctx, pid) => {
  const prow = (players || []).find((x) => Number(x.player_id) === Number(pid));
  let rolesObj = {};
  try {
    rolesObj = prow?.roles_json ? JSON.parse(prow.roles_json) : {};
  } catch {
    rolesObj = {};
  }
  const rr = rolesObj[String(ctx.role_id)] || {};
  const major = Number(rr.major || 0);
  const minor = Number(rr.minor || 0);
  return { ...ctx, major, minor, per_match: 5 * major + 2 * minor - 2 * (1 - major - minor) };
};

const rosterAssignedBoosterPlayer = (player, rosterPlayers) => {
  const pid = Number(player?.player_id);
  const roster = Array.isArray(rosterPlayers) ? rosterPlayers : [];
  if (!Number.isFinite(pid) || !Array.isArray(player?.point_breakdown) || player.point_breakdown.length === 0) {
    return player;
  }

  const rosterBreakdowns = {};
  roster.forEach((rosterPlayer) => {
    const rosterPid = Number(rosterPlayer?.player_id);
    if (Number.isFinite(rosterPid)) rosterBreakdowns[rosterPid] = rosterPlayer?.point_breakdown || [];
  });
  const assignedRows = new Map(assignRosterBoostersFromBreakdowns(rosterBreakdowns).map((assignment) => [assignment.key, assignment]));

  let assignedBoosterTotal = 0;
  const pointBreakdown = player.point_breakdown.map((row, rowIdx) => {
    const key = `${pid}:${rowIdx}`;
    const assigned = assignedRows.get(key);
    const rating = Number(row.rating_points || 0);
    const win = Number(row.win_points || 0);
    const role = Number(row.role_points || 0);
    const assignedBooster = assigned ? Number(assigned.points || 0) : 0;
    assignedBoosterTotal += assignedBooster;
    return {
      ...row,
      raw_booster_points: Number(row.booster_points || 0),
      raw_booster_name: row.booster_name,
      raw_booster_trigger_rate: Number(row.booster_trigger_rate || 0),
      booster_points: assignedBooster,
      booster_id: assigned ? assigned.boosterId : row.booster_id,
      booster_name: assigned ? assigned.boosterName : "Unassigned",
      booster_trigger_rate: assigned ? Number(assigned.triggerRate || 0) : 0,
      booster_assigned: Boolean(assigned),
      total_points: rating + win + role,
    };
  });

  return {
    ...player,
    booster: assignedBoosterTotal,
    booster_ev: assignedBoosterTotal,
    point_breakdown: pointBreakdown,
    roster_boosters_assigned: true,
  };
};

function PriceVsPointsPanel({ title, rows, slope, intercept, showTable = true, onPointClick = null }) {
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState("distance_desc");
  // 2D nearest-dot hover: the scatter shapes record their pixel positions and
  // mouse-move picks the closest dot in BOTH axes (the built-in tooltip only
  // matches on x, which picks the wrong player when prices tie).
  const [hover, setHover] = useState(null);
  const wrapRef = useRef(null);
  const dotPosRef = useRef(new Map());
  const minPrice = useMemo(() => Math.min(...rows.map((r) => Number(r.price))), [rows]);
  const maxPrice = useMemo(() => Math.max(...rows.map((r) => Number(r.price))), [rows]);
  const chartRows = useMemo(
    () =>
      [...rows]
        .sort((a, b) => a.price - b.price)
        .map((r) => ({
          ...r,
          trend: r.on_line,
        })),
    [rows]
  );
  // the players the search matches: highlighted on the chart, listed in the table
  const searchActive = search.trim().length > 0;
  const matchedIds = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return new Set();
    return new Set(
      rows
        .filter(
          (r) =>
            String(r.name || "").toLowerCase().includes(q) ||
            String(r.player_id || "").includes(q) ||
            String(r.team_id || "").includes(q)
        )
        .map((r) => Number(r.player_id))
    );
  }, [rows, search]);
  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    let out = [...rows];
    if (q) {
      out = out.filter(
        (r) =>
          String(r.name || "").toLowerCase().includes(q) ||
          String(r.player_id || "").includes(q) ||
          String(r.team_id || "").includes(q)
      );
    }
    switch (sortBy) {
      case "player_id_asc":
        out.sort((a, b) => Number(a.player_id) - Number(b.player_id));
        break;
      case "player_id_desc":
        out.sort((a, b) => Number(b.player_id) - Number(a.player_id));
        break;
      case "points_asc":
        out.sort((a, b) => a.points - b.points);
        break;
      case "points_desc":
        out.sort((a, b) => b.points - a.points);
        break;
      case "price_asc":
        out.sort((a, b) => a.price - b.price);
        break;
      case "price_desc":
        out.sort((a, b) => b.price - a.price);
        break;
      case "on_line_asc":
        out.sort((a, b) => a.on_line - b.on_line);
        break;
      case "on_line_desc":
        out.sort((a, b) => b.on_line - a.on_line);
        break;
      case "distance_abs_desc":
        out.sort((a, b) => Math.abs(b.distance) - Math.abs(a.distance));
        break;
      case "distance_asc":
        out.sort((a, b) => a.distance - b.distance);
        break;
      case "name_asc":
        out.sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
        break;
      case "name_desc":
        out.sort((a, b) => String(b.name || "").localeCompare(String(a.name || "")));
        break;
      case "distance_desc":
      default:
        out.sort((a, b) => b.distance - a.distance);
        break;
    }
    return out;
  }, [rows, search, sortBy]);

  // Reset the dot-position map during render (before the scatter shapes
  // re-register) whenever the data changes; an effect would wipe it AFTER the
  // shapes ran and leave it empty until the next re-render.
  const lastChartRowsRef = useRef(null);
  if (lastChartRowsRef.current !== chartRows) {
    lastChartRowsRef.current = chartRows;
    dotPosRef.current = new Map();
  }
  useEffect(() => {
    setHover(null);
  }, [chartRows]);

  // Plain DOM mouse tracking on the wrapper: independent of recharts' event
  // plumbing, matched against the dots' recorded svg positions (the svg sits
  // at the wrapper's 6px padding offset).
  const handleWrapMove = (e) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = e.clientX - rect.left - 6;
    const y = e.clientY - rect.top - 6;
    let bestId = null;
    let bestPos = null;
    let bestD2 = Infinity;
    dotPosRef.current.forEach((pos, id) => {
      const dx = pos.cx - x;
      const dy = pos.cy - y;
      const d2 = dx * dx + dy * dy;
      if (d2 < bestD2) {
        bestD2 = d2;
        bestId = id;
        bestPos = pos;
      }
    });
    if (bestId != null && bestD2 <= 28 * 28) {
      setHover((prev) => {
        if (prev?.id === bestId) return prev;
        const row = chartRows.find((r) => Number(r.player_id) === Number(bestId));
        return row ? { id: bestId, cx: bestPos.cx, cy: bestPos.cy, row } : null;
      });
    } else {
      setHover(null);
    }
  };

  return (
    <div className="card sub">
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="muted">Generate player valuation data first to view price vs points.</p>
      ) : (
        <div className="stack">
          <div
            className="value-chart-wrap value-scatter"
            ref={wrapRef}
            onMouseMove={handleWrapMove}
            onMouseLeave={() => setHover(null)}
            onClick={() => {
              if (hover?.row && onPointClick) onPointClick(hover.row);
            }}
          >
            <div className="value-trend-label">
              Average line: points = {intercept.toFixed(2)} + {(slope * 1000).toFixed(3)} per $1k of price
            </div>
            {hover && (
              <div
                className="value-point-tooltip"
                style={{
                  left: (wrapRef.current?.clientWidth || 9999) - hover.cx < 250 ? hover.cx - 14 : hover.cx + 14,
                  top: Math.max(8, hover.cy - 24),
                  transform:
                    (wrapRef.current?.clientWidth || 9999) - hover.cx < 250 ? "translateX(-100%)" : "none",
                }}
              >
                <div style={{ fontWeight: 700 }}>{hover.row.name}</div>
                <div>Points: {Number(hover.row.points).toFixed(2)}</div>
                <div>Average line: {Number(hover.row.trend).toFixed(2)}</div>
                <div>
                  Distance: {Number(hover.row.distance) >= 0 ? "+" : ""}
                  {Number(hover.row.distance).toFixed(2)}
                </div>
                <div>Price: ${Number(hover.row.price).toLocaleString()}</div>
              </div>
            )}
            <ResponsiveContainer width="100%" height={380}>
              <ComposedChart
                data={chartRows}
                margin={{ top: 12, right: 18, left: 14, bottom: 26 }}
                accessibilityLayer={false}
              >
                <CartesianGrid stroke="#232a34" strokeDasharray="3 3" />
                <XAxis
                  type="number"
                  dataKey="price"
                  domain={[minPrice, maxPrice]}
                  tick={{ fill: "#9fb2c9", fontSize: 12 }}
                  tickMargin={8}
                  tickFormatter={(v) => String(Math.round(Number(v) / 1000))}
                  axisLine={{ stroke: "#3a4452" }}
                  tickLine={{ stroke: "#3a4452" }}
                  name="Price"
                  label={{ value: "Price ($k)", position: "insideBottom", offset: -18, fill: "#9fb2c9", fontSize: 12.5 }}
                />
                <YAxis
                  type="number"
                  dataKey="points"
                  tick={{ fill: "#9fb2c9", fontSize: 12 }}
                  tickMargin={7}
                  axisLine={{ stroke: "#3a4452" }}
                  tickLine={{ stroke: "#3a4452" }}
                  name="Points"
                  label={{
                    value: "Expected points",
                    angle: -90,
                    position: "insideLeft",
                    offset: 2,
                    fill: "#9fb2c9",
                    fontSize: 12.5,
                    style: { textAnchor: "middle" },
                  }}
                />
                <Legend verticalAlign="top" height={30} wrapperStyle={{ color: "#9fb2c9" }} />
                <Line
                  type="linear"
                  dataKey="trend"
                  name="Average line"
                  stroke="#35a2ff"
                  strokeWidth={2}
                  dot={false}
                  activeDot={false}
                  isAnimationActive={false}
                />
                <Scatter
                  name="Players"
                  dataKey="points"
                  fill="#4fc3ff"
                  isAnimationActive={false}
                  shape={(props) => {
                    const { cx, cy, payload: pt } = props;
                    if (!Number.isFinite(cx) || !Number.isFinite(cy)) return null;
                    const pid = Number(pt?.player_id);
                    if (Number.isFinite(pid)) dotPosRef.current.set(pid, { cx, cy });
                    const active = hover != null && pid === Number(hover.id);
                    const matched = searchActive && matchedIds.has(pid);
                    const lit = active || matched;
                    return (
                      <g opacity={searchActive && !lit ? 0.28 : 1}>
                        {lit && <circle cx={cx} cy={cy} r={9.5} fill="none" stroke="#ff8a47" strokeWidth={2} />}
                        <circle cx={cx} cy={cy} r={lit ? 6 : 4.5} fill={lit ? "#ff8a47" : "#4fc3ff"} />
                        {matched && !active && (
                          <text x={cx} y={cy - 13} textAnchor="middle" fill="#ffb07a" fontSize={11} fontWeight={700}>
                            {pt?.name}
                          </text>
                        )}
                      </g>
                    );
                  }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
          {showTable && (
            <>
              <div className="grid two">
                <Input label="Search Players" value={search} onChange={setSearch} placeholder="Player name" className="search-field" />
              </div>
              <table>
                <thead>
                  <tr>
                    <SortHeader sortValue={sortBy} asc="name_asc" desc="name_desc" onChange={setSortBy}>Player</SortHeader>
                    <SortHeader sortValue={sortBy} asc="price_asc" desc="price_desc" onChange={setSortBy}>Price</SortHeader>
                    <SortHeader sortValue={sortBy} asc="points_asc" desc="points_desc" defaultDirection="desc" onChange={setSortBy}>Points</SortHeader>
                    <SortHeader sortValue={sortBy} asc="distance_asc" desc="distance_desc" defaultDirection="desc" onChange={setSortBy}>Distance</SortHeader>
                    <SortHeader sortValue={sortBy} asc="on_line_asc" desc="on_line_desc" defaultDirection="desc" onChange={setSortBy}>Expected Points</SortHeader>
                  </tr>
                </thead>
                <tbody>
                  {filteredRows.map((r) => (
                    <tr key={`dist-${r.player_id}`}>
                      <td>
                        <div className="table-player-cell">
                          <PlayerPhoto playerId={Number(r.player_id)} name={r.name} size={28} />
                          <span>{r.name}</span>
                        </div>
                      </td>
                      <td>${Number(r.price).toLocaleString()}</td>
                      <td>{r.points.toFixed(2)}</td>
                      <td>{r.distance >= 0 ? "+" : ""}{r.distance.toFixed(2)}</td>
                      <td>{r.on_line.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// Completed results: every player's points for the chosen results, in the
// app's standard table (photo, team, sortable columns, player search).
function PlayerScoresTable({ rows, teamOfPid, teamLookup, onOpen }) {
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState("points_desc");
  const list = useMemo(() => {
    const q = search.trim().toLowerCase();
    let out = (rows || []).map((r) => ({ ...r, team_id: teamOfPid?.[Number(r.player_id)] ?? r.team_id }));
    if (q) {
      out = out.filter(
        (r) => String(r.name || "").toLowerCase().includes(q) || String(teamLookup?.[r.team_id] || "").toLowerCase().includes(q)
      );
    }
    const [key, dir] = sortBy.split("_");
    const sign = dir === "asc" ? 1 : -1;
    const val = (r) => (key === "name" ? String(r.name || "") : key === "team" ? String(teamLookup?.[r.team_id] || "") : Number(r[key] || 0));
    out.sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sign;
    });
    return out;
  }, [rows, search, sortBy, teamOfPid, teamLookup]);
  return (
    <div className="card sub">
      <h3>Player Scores</h3>
      <div className="grid two">
        <Input label="Search Players" value={search} onChange={setSearch} placeholder="Player or team" className="search-field" />
      </div>
      <table>
        <thead>
          <tr>
            <SortHeader sortValue={sortBy} asc="name_asc" desc="name_desc" onChange={setSortBy}>Player</SortHeader>
            <SortHeader sortValue={sortBy} asc="team_asc" desc="team_desc" onChange={setSortBy}>Team</SortHeader>
            <SortHeader sortValue={sortBy} asc="price_asc" desc="price_desc" defaultDirection="desc" onChange={setSortBy}>Price</SortHeader>
            <SortHeader sortValue={sortBy} asc="points_asc" desc="points_desc" defaultDirection="desc" onChange={setSortBy}>Points</SortHeader>
            <SortHeader sortValue={sortBy} asc="rating_asc" desc="rating_desc" defaultDirection="desc" onChange={setSortBy}>Rating</SortHeader>
            <SortHeader sortValue={sortBy} asc="win_asc" desc="win_desc" defaultDirection="desc" onChange={setSortBy}>Win</SortHeader>
            <SortHeader sortValue={sortBy} asc="role_asc" desc="role_desc" defaultDirection="desc" onChange={setSortBy}>Role</SortHeader>
            <SortHeader sortValue={sortBy} asc="booster_asc" desc="booster_desc" defaultDirection="desc" onChange={setSortBy}>Boost</SortHeader>
          </tr>
        </thead>
        <tbody>
          {list.map((r) => (
            <tr key={r.player_id} onClick={() => onOpen && onOpen(r)}>
              <td>
                <div className="table-player-cell">
                  <PlayerPhoto playerId={Number(r.player_id)} name={r.name} size={28} />
                  <span>{r.name}</span>
                </div>
              </td>
              <td>{teamLookup?.[r.team_id] || "-"}</td>
              <td>${Math.round(Number(r.price || 0) / 1000)}k</td>
              <td>{Number(r.points || 0).toFixed(2)}</td>
              <td>{Number(r.rating || 0).toFixed(2)}</td>
              <td>{Number(r.win || 0).toFixed(2)}</td>
              <td>{Number(r.role || 0).toFixed(2)}</td>
              <td>{Number(r.booster || 0).toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GroupPlayerBreakdownModal({ player, teamLookup, teams, onClose }) {
  if (!player) return null;
  // Roster mode (opened from a Top 5 roster): the roster's assigned role and
  // its booster plan replace the player's best role and own best boosters.
  const roster = player.roster || null;
  const rating = Number(player.rating || 0);
  const win = Number(player.win || 0);
  const role = roster ? Number(roster.role_ev || 0) : Number(player.role || 0);
  const booster = roster ? Number(roster.booster_ev || 0) : Number(player.booster || 0);
  const total = roster
    ? rating + win + role + booster
    : Number.isFinite(Number(player.total)) && Number(player.total) !== 0
      ? Number(player.total)
      : rating + win + role + booster;
  const heroStats = [
    ["Rating", rating],
    ["Win", win],
    ["Role", role],
    ["Booster", booster],
  ];
  // Per-round cards, read top to bottom: the role (trigger rates → points per
  // match), the booster(s) for that match slot, the opponents the team can
  // meet with their win chance and the rating / win points each brings, then
  // the per-match sum of the four components, then that sum weighted by how
  // often the round is actually played (plus any bye / out-earlier credit).
  const stageList = Array.isArray(player.stage_list) ? player.stage_list : [];
  const stageEv = player.stage_ev || {};
  const teamRounds = player.team_rounds || {};
  const roleInfo = player.role_info || null;
  // Role points scale linearly with the per-match score, so the assigned
  // role's stage points are the best role's scaled by their ratio; each
  // booster slot carries the roster's booster for that match number.
  const bestPerMatch = roleInfo ? Number(roleInfo.points || 0) : 0;
  const roleScale = roster && Math.abs(bestPerMatch) > 1e-9 ? Number(roster.per_match || 0) / bestPerMatch : 1;
  const rosterBoosterBySlot = {};
  (roster?.boosters || []).forEach((a) => {
    const prob = Number(a.slot_probability || 0);
    rosterBoosterBySlot[Number(a.match_number)] = {
      booster_id: a.booster_id,
      name: a.booster || `Booster ${a.booster_id}`,
      rate: prob > 0 ? Number(a.expected_points || 0) / (prob * 5) : 0,
    };
  });
  const rosterAdjust = (cell, play) => {
    const list = Array.isArray(cell.boosters) ? cell.boosters : [];
    const boosters = list.map((b) => {
      const rb = rosterBoosterBySlot[Number(b.slot)];
      return {
        ...b,
        booster_id: rb ? rb.booster_id : null,
        booster_name: rb ? rb.name : "No booster left",
        booster_rate: rb ? rb.rate : 0,
      };
    });
    const boosterEv = play * boosters.reduce((a, b) => a + Number(b.share || 0) * 5 * Number(b.booster_rate || 0), 0);
    const roleEv = Number(cell.role || 0) * roleScale;
    return {
      ...cell,
      role: roleEv,
      booster: boosterEv,
      total: Number(cell.rating || 0) + Number(cell.win || 0) + roleEv + boosterEv,
      boosters,
    };
  };
  const padFor = (pad) =>
    roster && pad
      ? {
          ...pad,
          role: Number(pad.role || 0) * roleScale,
          total: Number(pad.rating || 0) + Number(pad.win || 0) + Number(pad.role || 0) * roleScale + Number(pad.booster || 0),
        }
      : pad;
  const pct = (v) => `${Math.round(Number(v || 0) * 100)}%`;
  const num = (v, d = 1) => Number(v || 0).toFixed(d);
  const ordinal = (n) => {
    const v = Number(n);
    const suffix = v % 100 >= 11 && v % 100 <= 13 ? "th" : ["th", "st", "nd", "rd"][v % 10] || "th";
    return `${v}${suffix}`;
  };
  const teamInfo = (tid) => (teams || []).find((t) => Number(t.team_id) === Number(tid));
  const teamRank = (tid) => {
    const info = teamInfo(tid);
    const r = player.team_ranks?.[String(tid)] ?? info?.rank ?? info?.world_rank ?? info?.hltv_rank;
    return r != null && Number(r) > 0 && Number(r) < 999 ? ` (#${r})` : "";
  };
  const roleRow = roster
    ? {
        role_id: roster.role_id,
        name: ROLE_NAMES[roster.role_id] || (roster.role_id != null ? `Role ${roster.role_id}` : "No role"),
        major: Number(roster.major || 0),
        minor: Number(roster.minor || 0),
        points: Number(roster.per_match || 0),
      }
    : roleInfo
      ? {
          role_id: roleInfo.role_id,
          name: ROLE_NAMES[roleInfo.role_id] || (roleInfo.role_id != null ? `Role ${roleInfo.role_id}` : "No role"),
          major: Number(roleInfo.major || 0),
          minor: Number(roleInfo.minor || 0),
          points: Number(roleInfo.points || 0),
        }
      : null;
  const matchCard = (s) => {
    const round = teamRounds[s.key] || {};
    const play = Number(round.play || 0);
    const cell = roster ? rosterAdjust(stageEv[s.key] || {}, play) : stageEv[s.key] || {};
    const bye = Number(round.bye || 0);
    const elimProb = Number(round.elim_before?.prob || 0);
    const elimPts = Number(round.elim_before?.points || 0);
    const cellTotal = Number(cell.total || 0);
    if (play < 0.0005 && bye < 0.0005 && elimProb < 0.0005 && Math.abs(cellTotal) < 0.005) return null;
    const oppPts = cell.opponents || {};
    const opponents = Object.entries(round.opponents || {})
      .map(([tid, o]) => {
        const p = Number(o.play || 0);
        const c = oppPts[tid] || {};
        const per = (v) => (p > 0 ? Number(v || 0) / p : 0);
        return {
          team_id: Number(tid),
          play: p,
          win: Number(o.win || 0),
          rating: per(c.rating),
          winPts: per(c.win),
          total: per(c.total),
        };
      })
      .filter((o) => o.play > 0.0005)
      .sort((a, b) => b.play - a.play);
    const shown = opponents.slice(0, 8);
    const rest = opponents.slice(8);
    const restPlay = rest.reduce((a, o) => a + o.play, 0);
    const wavg = (list, key) => (restPlay > 0 ? list.reduce((a, o) => a + o.play * o[key], 0) / restPlay : 0);
    const oppPlay = opponents.reduce((a, o) => a + o.play, 0);
    const oppAvg = (key) => (oppPlay > 0 ? opponents.reduce((a, o) => a + o.play * o[key], 0) / oppPlay : 0);
    const avgRow = oppPlay > 0.0005 ? { play: oppPlay, win: oppAvg("win"), winPts: oppAvg("winPts"), rating: oppAvg("rating") } : null;
    const covered = play + bye + elimProb;
    const elsewhere = (s.phase === "group" || s.bracket) && covered < 0.9995 ? 1 - covered : 0;
    const elsewhereLabel =
      s.bracket === "lower" ? "Upper bracket"
      : s.phase === "group" ? "Lower bracket"
      : s.label === "3rd place" ? "Final or out earlier"
      : "Out earlier";
    const perMatch = {
      rating: play > 0.0005 ? Number(cell.rating || 0) / play : 0,
      win: play > 0.0005 ? (Number(cell.win || 0) - elimPts) / play : 0,
      role: play > 0.0005 ? Number(cell.role || 0) / play : 0,
      booster: play > 0.0005 ? Number(cell.booster || 0) / play : 0,
    };
    const perMatchTotal = perMatch.rating + perMatch.win + perMatch.role + perMatch.booster;
    const boosters = Array.isArray(cell.boosters) ? cell.boosters : [];
    const steps = [
      { op: "", label: "Total", value: num(perMatchTotal) },
      { op: "×", label: "Play chance", value: pct(play) },
      ...(Math.abs(elimPts) > 0.005 ? [{ op: "−", label: "Out early", value: num(Math.abs(elimPts)) }] : []),
    ];
    return {
      key: s.key,
      label: s.full || STAGE_FULL[s.label] || s.label,
      chance: play > 0.0005 ? pct(Number(round.win || 0)) : bye > 0.0005 ? "Bye" : "—",
      chanceLabel: play > 0.0005 ? "Match win" : null,
      roleRow,
      boosterRows: boosters.map((b) => ({
        key: `${b.booster_id}-${b.slot}`,
        booster_id: b.booster_id,
        name: b.booster_name || `Booster ${b.booster_id}`,
        slot: b.slot,
        share: boosters.length > 1 ? Number(b.share || 0) : null,
        rate: Number(b.booster_rate || 0),
        points: 5 * Number(b.booster_rate || 0),
      })),
      noBooster: boosters.length === 0 && play > 0.0005,
      tableHead: "Team",
      rows: [
        ...shown.map((o) => ({
          key: `t${o.team_id}`,
          team_id: o.team_id,
          name: String(teamLookup[o.team_id] || o.team_id),
          play: o.play,
          win: o.win,
          winPts: o.winPts,
          rating: o.rating,
          total: o.total,
        })),
        ...(rest.length > 0
          ? [{ key: "others", muted: true, name: `Others (${rest.length})`, play: restPlay, win: wavg(rest, "win"), winPts: wavg(rest, "winPts"), rating: wavg(rest, "rating"), total: wavg(rest, "total") }]
          : []),
        ...(elimProb > 0.0005 ? [{ key: "elim", muted: true, name: "Out earlier", play: elimProb, winPts: elimPts / elimProb, total: elimPts / elimProb }] : []),
        ...(bye > 0.0005 && play > 0.0005 ? [{ key: "pad", muted: true, name: "Qualified earlier", play: bye, total: 0 }] : []),
        ...(elsewhere > 0.0005 ? [{ key: "else", muted: true, name: elsewhereLabel, play: elsewhere, total: 0 }] : []),
      ],
      avgRow,
      ledger: {
        parts: [
          ["Rating", perMatch.rating],
          ["Win", perMatch.win],
          ["Role", perMatch.role],
          ["Booster", perMatch.booster],
        ],
        sum: perMatchTotal,
        steps,
      },
      total: cellTotal,
    };
  };
  // Padding cards (group: the match a quick qualifier skips; playoff: the
  // quarter-final a bye skips): a flat credit, so the card is the points list
  // and the multiplication, laid out vertically in large type to fill its cell.
  const padCard = (key, label, pad, prob) => {
    if (!pad || !(Math.abs(Number(pad.total || 0)) > 0.005 || prob > 0.0005)) return null;
    const per = (v) => (prob > 0.0005 ? Number(v || 0) / prob : 0);
    const perMatch = { rating: per(pad.rating), win: per(pad.win), role: per(pad.role), booster: per(pad.booster) };
    const perMatchTotal = perMatch.rating + perMatch.win + perMatch.role + perMatch.booster;
    return {
      key,
      extra: true,
      label,
      chance: null,
      chanceLabel: null,
      vertical: true,
      roleRow: null,
      boosterRows: [],
      noBooster: false,
      note: null,
      rows: [],
      avgRow: null,
      ledger: {
        parts: [
          ["Rating", perMatch.rating],
          ["Win", perMatch.win],
          ["Role", perMatch.role],
          ["Booster", perMatch.booster],
        ],
        sum: perMatchTotal,
        steps: [
          { op: "", label: "Total", value: num(perMatchTotal) },
          { op: "×", label: "Applies", value: pct(prob) },
        ],
      },
      total: Number(pad.total || 0),
    };
  };
  const paddingCard = padCard("padding", "Padding", padFor(player.padding || null), Number(player.padding_prob || 0));
  const playoffPadCard = padCard("po_padding", "Padding", padFor(player.playoff_padding || null), Number(player.playoff_pad_prob || 0));
  const sideOf = (s) => s.bracket || (s.phase === "playoff" ? "playoff" : "upper");
  // A stage flagged `pad` is a bye / padding credit shown as a vertical pad
  // card (the stage's points and the chance it applies); anything else is a
  // match card.
  const cardFor = (s) =>
    s.pad
      ? padCard(s.key, s.full || STAGE_FULL[s.key] || s.key, padFor(stageEv[s.key] || null), Number(teamRounds[s.key]?.bye || 0))
      : matchCard(s);
  // Sections: explicit `section` ids on the stages, else the classic group /
  // playoff split. A section is a bracket grid (an opener spanning both rows,
  // then an upper and a lower row) when any stage names a bracket side.
  const sections = [];
  stageList.forEach((s) => {
    const id = s.section || (s.phase === "playoff" ? "playoff" : "group");
    let sec = sections.find((x) => x.id === id);
    if (!sec) {
      sec = { id, title: s.section_title || (id === "playoff" ? "Playoffs" : "Group Stage"), stages: [] };
      sections.push(sec);
    }
    sec.stages.push(s);
  });
  const built = sections.map((sec) => {
    // A section is a bracket grid when a stage sits on an upper/lower side;
    // the playoff rounds ("playoff" side) read as one row of cards.
    const isBracket = sec.id === "group" || sec.stages.some((st) => (st.bracket && st.bracket !== "playoff") || st.opener);
    if (!isBracket) {
      const cards = sec.stages.map(cardFor).filter(Boolean);
      // the bye card sits by the round it skips: a bracket's first round, or
      // after the last round of a stage whose padding is for rounds not played
      const padAt = sec.stages.some((st) => st.pad_position === "end") ? cards.length : Math.min(1, cards.length);
      if (sec.id === "playoff" && playoffPadCard && cards.length > 0) cards.splice(padAt, 0, playoffPadCard);
      return { ...sec, isBracket: false, cards };
    }
    const openerStage = sec.stages.find((st) => st.opener) || (sec.id === "group" ? sec.stages[0] : null);
    const opener = openerStage ? cardFor(openerStage) : null;
    const upper = sec.stages.filter((st) => st !== openerStage && sideOf(st) === "upper").map(cardFor).filter(Boolean);
    const lower = sec.stages.filter((st) => st !== openerStage && sideOf(st) === "lower").map(cardFor).filter(Boolean);
    if (sec.id === "group" && paddingCard && (upper.length > 0 || opener)) upper.splice(Math.min(1, upper.length), 0, paddingCard);
    return { ...sec, isBracket: true, opener, upper, lower, cols: (opener ? 1 : 0) + Math.max(upper.length, lower.length) };
  });
  const hasStages = built.some((sec) => (sec.isBracket ? Boolean(sec.opener) || sec.upper.length > 0 || sec.lower.length > 0 : sec.cards.length > 0));
  const matchRows = Array.isArray(player.match_rows) ? player.match_rows : [];
  // Conditioned outcomes (the outcomes a roster wins, its single best one):
  // the team's runs through the event, most common first — each match with
  // its opponent, result and the player's points, the paddings and the
  // out-early penalty — in place of the per-round cards. Shares sum to one;
  // the share-weighted totals are the hero figures.
  const runs = Array.isArray(player.runs) ? player.runs : null;
  const runsRest = player.runs_rest || null;
  const sharePct = (v) => {
    const p = Number(v || 0) * 100;
    return `${p >= 10 ? p.toFixed(0) : p >= 1 ? p.toFixed(1) : p.toFixed(2)}%`;
  };
  const runStage = (m) => m.label || STAGE_SHORT[m.stage] || STAGE_FULL[m.stage] || m.stage;
  const runChip = (m, j) => {
    const oppId = Number(m.opponent_team_id || 0);
    const info = oppId > 0 ? teamInfo(oppId) : null;
    return (
      <span className={`run-chip ${m.won ? "win" : "loss"}`} key={`m-${j}`} title={m.booster_name ? `${runStage(m)} · ${m.booster_name}` : runStage(m)}>
        <span className="run-chip-stage">{runStage(m)}</span>
        <span className="run-chip-main">
          <span className="run-chip-result">{m.won ? "W" : "L"}</span>
          {info ? <TeamLogo hltvTeamId={info?.hltv_team_id} name={teamLookup[oppId]} size={18} /> : null}
          <span className="run-chip-opp">{teamLookup[oppId] || oppId}</span>
          {Number(m.opponent_share ?? 1) < 0.995 ? <span className="run-chip-share">{Math.round(Number(m.opponent_share) * 100)}%</span> : null}
        </span>
        <span className={`run-chip-pts${Number(m.total) < -0.005 ? " neg" : ""}`}>{num(m.total)}</span>
      </span>
    );
  };
  const adjustChip = (key, label, value) => (
    <span className="run-chip adjust" key={key}>
      <span className="run-chip-stage">{label}</span>
      <span className={`run-chip-pts${Number(value) < -0.005 ? " neg" : ""}`}>{num(value)}</span>
    </span>
  );
  const runCells = (t) =>
    ["rating", "win", "role", "booster", "total"].map((k) => (
      <span className="run-cell num" key={k}>
        <span className={`match-v${k === "total" ? " strong" : ""}${Number(t?.[k]) < -0.005 ? " neg" : ""}`}>{num(t?.[k], 2)}</span>
      </span>
    ));
  const runsSection = runs ? (
    <div className="stack">
      <h4>Runs</h4>
      <div className="run-table">
        <div className="run-trow head">
          <span>Share</span>
          <span>Run</span>
          <span className="num">Rating</span>
          <span className="num">Win</span>
          <span className="num">Role</span>
          <span className="num">Booster</span>
          <span className="num">Total</span>
        </div>
        {runs.map((run, i) => (
          <div className="run-trow" key={i}>
            <span className="run-cell">
              <span className="match-v accent">{sharePct(run.share)}</span>
            </span>
            <span className="run-chips">
              {(run.matches || []).map(runChip)}
              {(run.pads || [])
                .filter((p) => Math.abs(Number(p.total || 0)) > 0.005)
                .map((p, j) => adjustChip(`p-${j}`, p.label || "Padding", p.total))}
              {Math.abs(Number(run.penalty || 0)) > 0.005 ? adjustChip("pen", "Out early", run.penalty) : null}
            </span>
            {runCells(run.totals || {})}
          </div>
        ))}
        {runsRest ? (
          <div className="run-trow muted">
            <span className="run-cell">
              <span className="match-v">{sharePct(runsRest.share)}</span>
            </span>
            <span className="run-chips">
              <span className="match-v small">Other runs ({runsRest.count})</span>
            </span>
            {runCells(runsRest.totals || {})}
          </div>
        ) : null}
        <div className="run-trow total">
          <span className="run-cell">
            <span className="match-v small">Weighted</span>
          </span>
          <span className="run-chips">
            <span className="match-sub">
              {runs.length + (runsRest ? runsRest.count : 0)} run{runs.length + (runsRest ? runsRest.count : 0) === 1 ? "" : "s"}
            </span>
          </span>
          {runCells({ rating, win, role, booster, total })}
        </div>
      </div>
    </div>
  ) : null;
  const dash = <span className="round-cell-dash">—</span>;
  const renderCard = (c) => (
    <div className={`round-card${c.extra ? " extra" : ""}`} key={c.key}>
      <div className="round-card-top">
        <span className="round-card-name">{c.label}</span>
        {c.chance && (
          <span className="round-card-play">
            <span className="round-card-chance">{c.chance}</span>
            {c.chanceLabel && <span className="event-player-stat-label">{c.chanceLabel}</span>}
          </span>
        )}
      </div>
      {c.note && <div className="round-card-note top">{c.note}</div>}
      {c.roleRow && (
        <div className="round-section">
          <div className="round-section-title">Role</div>
          <div className="round-info-row">
            <span className="round-info-main">
              {c.roleRow.role_id != null && <RoleBadge roleId={c.roleRow.role_id} size={20} />}
              <span className="round-info-name">
                {c.roleRow.name}
                <em>
                  {pct(c.roleRow.major)} major · {pct(c.roleRow.minor)} minor
                </em>
              </span>
            </span>
            <span className="round-info-value">{num(c.roleRow.points)}</span>
          </div>
        </div>
      )}
      {(c.boosterRows.length > 0 || c.noBooster) && (
        <div className="round-section">
          <div className="round-section-title">Booster</div>
          {c.boosterRows.map((b) => (
            <div className="round-info-row" key={b.key}>
              <span className="round-info-main">
                {b.booster_id != null && <BoosterBadge boosterId={b.booster_id} size={20} />}
                <span className="round-info-name">
                  {b.name}
                  <em>
                    {ordinal(b.slot)} match · {pct(b.rate)} trigger{b.share != null ? ` · ${pct(b.share)} of the time` : ""}
                  </em>
                </span>
              </span>
              <span className="round-info-value">{num(b.points)}</span>
            </div>
          ))}
          {c.noBooster && (
            <div className="round-info-row">
              <span className="round-info-main muted">None left for this match</span>
              <span className="round-info-value">0.0</span>
            </div>
          )}
        </div>
      )}
      {c.rows.length > 0 && (
        <div className="round-section">
          <div className="round-section-title">Win and Rating</div>
            <div className="round-table">
            <div className="round-trow head">
              <span className="round-tcell name">{c.tableHead}</span>
              <span className="round-tcell">{c.extra ? "Odds" : "Play"}</span>
              <span className="round-tcell">Win%</span>
              <span className="round-tcell">Win</span>
              <span className="round-tcell">Rating</span>
            </div>
            {c.rows.map((r) => {
              const info = r.team_id != null ? teamInfo(r.team_id) : null;
              return (
                <div className={`round-trow${r.muted ? " muted" : ""}`} key={r.key}>
                  <span className="round-tcell name">
                    {r.team_id != null && <TeamLogo hltvTeamId={info?.hltv_team_id} name={teamLookup[r.team_id]} size={14} />}
                    <span className="round-tcell-text">{r.name}</span>
                  </span>
                  <span className="round-tcell">{pct(r.play)}</span>
                  <span className="round-tcell">{r.win != null ? pct(r.win) : dash}</span>
                  <span className={`round-tcell${Number(r.winPts) < -0.005 ? " neg" : ""}`}>{r.winPts != null ? num(r.winPts) : dash}</span>
                  <span className="round-tcell">{r.rating != null ? num(r.rating) : dash}</span>
                </div>
              );
            })}
            {c.avgRow && (
              <div className="round-trow avg">
                <span className="round-tcell name">
                  <span className="round-tcell-text">Average</span>
                </span>
                <span className="round-tcell">{pct(c.avgRow.play)}</span>
                <span className="round-tcell">{c.avgRow.win != null ? pct(c.avgRow.win) : dash}</span>
                <span className={`round-tcell${Number(c.avgRow.winPts) < -0.005 ? " neg" : ""}`}>{num(c.avgRow.winPts)}</span>
                <span className="round-tcell">{num(c.avgRow.rating)}</span>
              </div>
            )}
          </div>
        </div>
      )}
      <div className={`round-section${c.vertical ? " fill" : ""}`}>
        <div className="round-section-title">Total</div>
        {c.vertical ? (
          <div className="round-vparts">
            {c.ledger.parts.map(([label, v]) => (
              <div className="round-vpart" key={label}>
                <span className="round-vpart-label">{label}</span>
                <span className={`round-vpart-value${Number(v) < -0.005 ? " neg" : ""}`}>{num(v)}</span>
              </div>
            ))}
            {c.ledger.steps.map((st, i) => (
              <div className={`round-vpart${i === 0 ? " sum" : ""}`} key={st.label}>
                <span className="round-vpart-label">
                  {st.op && <i>{st.op}</i>}
                  {st.label}
                </span>
                <span className="round-vpart-value">{st.value}</span>
              </div>
            ))}
            <div className="round-vpart expected">
              <span className="round-vpart-label">
                <i>=</i>
                Expected
              </span>
              <span className="round-vpart-value accent">{num(c.total, 2)}</span>
            </div>
          </div>
        ) : (
          <>
            <div className="round-parts">
              {[["Total", c.ledger.sum], ...c.ledger.parts].map(([label, v], i) => (
                <div className={`round-part${i === 0 ? " total" : ""}`} key={label}>
                  <div className={`round-part-value${Number(v) < -0.005 ? " neg" : ""}`}>{num(v)}</div>
                  <div className="round-part-label">{label}</div>
                </div>
              ))}
            </div>
            <div className="round-eq">
              {c.ledger.steps.map((st) => (
                <React.Fragment key={st.label}>
                  {st.op && <span className="round-eq-op">{st.op}</span>}
                  <div className="round-part">
                    <div className="round-part-value">{st.value}</div>
                    <div className="round-part-label">{st.label}</div>
                  </div>
                </React.Fragment>
              ))}
              <span className="round-eq-op">=</span>
              <div className="round-part">
                <div className="round-part-value accent">{num(c.total, 2)}</div>
                <div className="round-part-label">Expected</div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
  const gridStyle = (cols) => ({ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` });
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal player-sources-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h3>Point Sources</h3>
          <button className="close" onClick={onClose}>
            &times;
          </button>
        </header>
        <div className="modal-body">
          <BreakdownHero
            playerId={player.player_id}
            name={player.name || `Player ${player.player_id}`}
            teamName={player.team_id ? teamLookup[player.team_id] || String(player.team_id) : null}
            hltvTeamId={player.team_id ? teamInfo(player.team_id)?.hltv_team_id : null}
            roleId={roster ? roster.role_id : player.role_id}
            note={
              player.hero_note ||
              (roster
                ? `in this roster · best role ${ROLE_NAMES[player.role_id] || (player.role_id != null ? `Role ${player.role_id}` : "—")}`
                : null)
            }
            stats={heroStats}
            total={total}
          />
          {player.note && <p className="muted">{player.note}</p>}
          {!hasStages && !player.note && matchRows.length === 0 && !runs && (
            <p className="muted">Re-run the valuation to see the per-round split.</p>
          )}
          {runsSection}
          {!runs && built.map((sec) =>
            sec.isBracket ? (
              (sec.opener || sec.upper.length > 0 || sec.lower.length > 0) && (
                <div className="stack round-row" key={sec.id}>
                  <h4>{sec.title}</h4>
                  <div className="round-cards stage-grid bracket-grid" style={gridStyle(sec.cols)}>
                    {sec.opener && (
                      <div className="bracket-cell opener" style={{ gridColumn: 1, gridRow: "1 / span 2" }}>
                        {renderCard(sec.opener)}
                      </div>
                    )}
                    {sec.upper.map((c, i) => (
                      <div className="bracket-cell" style={{ gridColumn: i + (sec.opener ? 2 : 1), gridRow: 1 }} key={`u-${c.key}`}>
                        {renderCard(c)}
                      </div>
                    ))}
                    {sec.lower.map((c, i) => (
                      <div className="bracket-cell" style={{ gridColumn: i + (sec.opener ? 2 : 1), gridRow: 2 }} key={`l-${c.key}`}>
                        {renderCard(c)}
                      </div>
                    ))}
                  </div>
                </div>
              )
            ) : (
              sec.cards.length > 0 && (
                <div className="stack round-row" key={sec.id}>
                  <h4>{sec.title}</h4>
                  <div className="round-cards stage-grid" style={gridStyle(Math.max(1, sec.cards.length))}>{sec.cards.map(renderCard)}</div>
                </div>
              )
            )
          )}
          {player.outcome_bracket && (
            <div className="stack">
              <h4>Bracket</h4>
              <OutcomeBracketVisual bracket={player.outcome_bracket} teamLookup={teamLookup} teams={teams} highlightTeamId={player.team_id} />
            </div>
          )}
          {matchRows.length > 0 &&
            (() => {
              const sums = matchRows.reduce(
                (a, r) => ({
                  rating: a.rating + Number(r.rating_points || 0),
                  win: a.win + Number(r.win_points || 0),
                  role: a.role + Number(r.role_points || 0),
                  booster: a.booster + Number(r.booster_points || 0),
                  total: a.total + Number(r.total_points || 0),
                }),
                { rating: 0, win: 0, role: 0, booster: 0, total: 0 }
              );
              const played = matchRows.filter((r) => r.match_number != null).length;
              const padded = matchRows.filter((r) => String(r.match_type || "").toUpperCase() === "PADDING").length;
              const rated = matchRows.filter((r) => r.rating_used != null);
              const ratingAvg = rated.length ? rated.reduce((a, r) => a + Number(r.rating_used || 0), 0) / rated.length : null;
              const cellNum = (v, extra = "", sub = null) => (
                <span className="match-cell num">
                  <span className={`match-v${Number(v) < -0.005 ? " neg" : ""}${extra ? ` ${extra}` : ""}`}>{Number(v || 0).toFixed(2)}</span>
                  {sub ? <span className="match-sub">{sub}</span> : null}
                </span>
              );
              return (
                <div className="stack">
                  <h4>Match Sources</h4>
                  <div className="match-table">
                    <div className="match-trow head">
                      <span>Match</span>
                      <span>Opponent</span>
                      <span className="num">Rating</span>
                      <span className="num">Rating pts</span>
                      <span className="num">Win</span>
                      <span className="num">Role</span>
                      <span className="num">Booster</span>
                      <span className="num">Booster pts</span>
                      <span className="num">Total</span>
                    </div>
                    {matchRows.map((row, idx) => {
                      const type = String(row.match_type || "").toUpperCase();
                      const isPad = type === "PADDING";
                      const isElim = type === "ELIMINATION";
                      const opponentId = Number(row.opponent_team_id || 0);
                      const info = opponentId > 0 ? teamInfo(opponentId) : null;
                      const label = row.match_number ? `Match ${row.match_number}` : isPad ? "Padding" : isElim ? "Elimination" : "Adjustment";
                      const labelSub = row.match_number ? row.match_type || "BO3" : "";
                      const missed = isElim ? String(row.note || "").match(/(\d+) unplayed/) : null;
                      const oppText = opponentId > 0 ? `${teamLookup[opponentId] || opponentId}${teamRank(opponentId)}` : isPad ? "N/A" : isElim ? "Knocked out" : "—";
                      const oppSub =
                        opponentId > 0 && row.did_win != null
                          ? `${row.did_win ? "Win" : "Loss"} · ${(Number(row.win_probability || 0) * 100).toFixed(0)}% chance`
                          : isPad
                            ? ""
                            : isElim
                              ? `${missed ? missed[1] : ""} round${missed && missed[1] === "1" ? "" : "s"} missed · −3 each`
                              : row.note || "";
                      const roleName = row.role_id == null ? "" : ROLE_NAMES[row.role_id] || `Role ${row.role_id}`;
                      const rate = Number(row.booster_trigger_rate || 0);
                      const boosterName = row.booster_name || (row.booster_slot ? `Booster slot ${row.booster_slot}` : "");
                      return (
                        <div className={`match-trow${isPad || isElim ? " adjust" : ""}`} key={`${row.match_number || "adj"}-${idx}`}>
                          <span className="match-cell">
                            <span className="match-v small">{label}</span>
                            {labelSub ? <span className="match-sub">{labelSub}</span> : null}
                          </span>
                          <span className="match-cell opp">
                            {info ? <TeamLogo hltvTeamId={info?.hltv_team_id} name={teamLookup[opponentId]} size={22} /> : null}
                            <span className="match-cell-text">
                              <span className="match-v small">{oppText}</span>
                              {oppSub ? (
                                <span className={`match-sub${opponentId > 0 && row.did_win != null ? (row.did_win ? " pos" : " neg") : ""}`}>{oppSub}</span>
                              ) : null}
                            </span>
                          </span>
                          <span className="match-cell num">
                            <span className="match-v">{row.rating_used != null ? Number(row.rating_used).toFixed(2) : "—"}</span>
                          </span>
                          {cellNum(row.rating_points)}
                          {cellNum(row.win_points)}
                          {cellNum(row.role_points, "", roleName || null)}
                          <span className="match-cell num">
                            <span className="match-v small">{boosterName || "—"}</span>
                            {rate > 0 ? <span className="match-sub">{(rate * 100).toFixed(0)}% trigger</span> : null}
                          </span>
                          {cellNum(row.booster_points)}
                          {cellNum(row.total_points, "strong")}
                        </div>
                      );
                    })}
                    <div className="match-trow total">
                      <span className="match-cell">
                        <span className="match-v small">Total</span>
                        <span className="match-sub">
                          {played} match{played === 1 ? "" : "es"}
                          {padded ? ` · ${padded} padded` : ""}
                        </span>
                      </span>
                      <span />
                      <span className="match-cell num">
                        <span className="match-v">{ratingAvg != null ? ratingAvg.toFixed(2) : "—"}</span>
                        {ratingAvg != null ? <span className="match-sub">average</span> : null}
                      </span>
                      {cellNum(sums.rating)}
                      {cellNum(sums.win)}
                      {cellNum(sums.role)}
                      <span />
                      {cellNum(sums.booster)}
                      {cellNum(sums.total, "accent")}
                    </div>
                  </div>
                </div>
              );
            })()}
          {!hasStages && matchRows.length === 0 && (
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>{player.note ? "Points" : "Expected Points"}</th>
                </tr>
              </thead>
              <tbody>
                {heroStats.map(([label, value]) => (
                  <tr key={label}><td>{label}</td><td>{value.toFixed(2)}</td></tr>
                ))}
                <tr>
                  <td><strong>Total</strong></td>
                  <td><strong>{total.toFixed(2)}</strong></td>
                </tr>
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

const formatBatchEta = (seconds) => {
  if (!Number.isFinite(seconds) || seconds == null || seconds < 0) return "Calculating...";
  if (seconds <= 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return `${minutes}m ${rem}s`;
};

const getBatchStartedAtMs = (status, fallback = Date.now()) => {
  const startedAt = Number(status?.started_at || status?.created_at || 0);
  return Number.isFinite(startedAt) && startedAt > 0 ? startedAt * 1000 : fallback;
};

const jobStatusLabel = (status) =>
  ({
    completed: "Completed",
    failed: "Failed",
    canceled: "Canceled",
    canceling: "Canceling",
    paused: "Paused",
    pausing: "Pausing",
    running: "Running",
    queued: "Queued",
  }[status] || "Queued");

// Shared map-stats import job state. Owned by App so both the Maps tab (bulk
// import) and the Database team modal (single-team import) drive the same job.
function useMapStatsJob({ refresh, notify, modalRefreshRef }) {
  const [status, setStatus] = useState("idle");
  const [jobId, setJobId] = useState("");
  const [processed, setProcessed] = useState(0);
  const [total, setTotal] = useState(0);
  const [ok, setOk] = useState(0);
  const [failed, setFailed] = useState(0);
  const [lastError, setLastError] = useState("");
  const [etaSeconds, setEtaSeconds] = useState(null);
  const pollingRef = useRef(false);

  const applyStatus = (statusPayload, jobIdOverride = "") => {
    const nextJobId = String(jobIdOverride || statusPayload?.job_id || "");
    const nextProcessed = Number(statusPayload?.processed_teams || 0);
    const nextTotal = Number(statusPayload?.total_teams || 0);
    const nextOk = Number(statusPayload?.ok || 0);
    const nextFailed = Number(statusPayload?.failed || 0);
    const nextStatus = String(statusPayload?.status || "queued");
    const nextLastError = String(statusPayload?.last_error || statusPayload?.error || "");

    setStatus(nextStatus);
    setJobId(nextJobId);
    setProcessed(nextProcessed);
    setTotal(nextTotal);
    setOk(nextOk);
    setFailed(nextFailed);
    setLastError(nextLastError);

    const startedAtMs = getBatchStartedAtMs(statusPayload);
    if (nextProcessed > 0 && nextTotal > nextProcessed && ["queued", "running", "pausing", "canceling"].includes(nextStatus)) {
      const elapsedSeconds = Math.max(1, (Date.now() - startedAtMs) / 1000);
      const rate = nextProcessed / elapsedSeconds;
      setEtaSeconds(rate > 0 ? (nextTotal - nextProcessed) / rate : null);
    } else if (nextTotal > 0 && nextProcessed >= nextTotal) {
      setEtaSeconds(0);
    } else {
      setEtaSeconds(null);
    }

    return { jobId: nextJobId, ok: nextOk, failed: nextFailed, nextStatus, lastError: nextLastError };
  };

  const poll = async (pollJobId) => {
    if (!pollJobId || pollingRef.current) return;
    pollingRef.current = true;
    try {
      let done = false;
      let pollFailures = 0;
      while (!done) {
        let statusPayload;
        try {
          statusPayload = await api.get(`/teams/map-stats-import/job/${pollJobId}`, 60000);
          pollFailures = 0;
        } catch (pollError) {
          // The job keeps running server-side; only give up after repeated failures.
          pollFailures += 1;
          if (pollFailures >= 5) throw pollError;
          await new Promise((resolve) => setTimeout(resolve, 3000));
          continue;
        }
        const applied = applyStatus(statusPayload, pollJobId);

        if (applied.nextStatus === "completed") {
          notify(`Map stats imported: ${applied.ok} ok, ${applied.failed} failed`);
          await refresh();
          await modalRefreshRef?.current?.();
          done = true;
          break;
        }
        if (applied.nextStatus === "failed") {
          notify(applied.lastError || "Map stats import failed.");
          done = true;
          break;
        }
        if (["paused", "canceled"].includes(applied.nextStatus)) {
          await refresh();
          await modalRefreshRef?.current?.();
          done = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 1200));
      }
    } catch (e) {
      setStatus("failed");
      setLastError(String(e?.message || "Failed to poll map stats job."));
      notify(`Map stats import failed: ${e?.message || "unknown error"}`);
    } finally {
      pollingRef.current = false;
    }
  };

  const start = async (missingOnly = false, teamIds = []) => {
    setStatus("queued");
    setProcessed(0);
    setTotal(0);
    setOk(0);
    setFailed(0);
    setLastError("");
    setEtaSeconds(null);
    try {
      const startPayload = await api.post("/teams/map-stats-import/start", {
        missing_only: missingOnly,
        team_ids: teamIds,
      });
      const startedJobId = String(startPayload?.job_id || "");
      if (!startedJobId) throw new Error("Failed to start map stats import job.");
      setJobId(startedJobId);
      await poll(startedJobId);
    } catch (e) {
      setStatus("failed");
      setLastError(String(e?.message || "Failed to start map stats import job."));
      notify(`Map stats import failed: ${e?.message || "unknown error"}`);
    }
  };

  const pause = async () => {
    if (!jobId) return;
    setStatus("pausing");
    try {
      const statusPayload = await api.post(`/teams/map-stats-import/job/${jobId}/pause`, {});
      const applied = applyStatus(statusPayload, jobId);
      if (["pausing", "running", "queued"].includes(applied.nextStatus)) {
        poll(jobId);
      }
    } catch (e) {
      setStatus("running");
      notify(`Failed to pause map stats import: ${e?.message || "unknown error"}`);
    }
  };

  const cancel = async () => {
    if (!jobId) return;
    setStatus("canceling");
    try {
      const statusPayload = await api.post(`/teams/map-stats-import/job/${jobId}/cancel`, {});
      const applied = applyStatus(statusPayload, jobId);
      if (["canceling", "running", "queued", "pausing"].includes(applied.nextStatus)) {
        poll(jobId);
      }
    } catch (e) {
      notify(`Failed to cancel map stats import: ${e?.message || "unknown error"}`);
    }
  };

  const resume = async () => {
    if (!jobId) return;
    try {
      const statusPayload = await api.post(`/teams/map-stats-import/job/${jobId}/resume`, {});
      const applied = applyStatus(statusPayload, jobId);
      if (["queued", "running", "pausing"].includes(applied.nextStatus)) {
        poll(jobId);
      }
    } catch (e) {
      notify(`Failed to resume map stats import: ${e?.message || "unknown error"}`);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const hydrateLatest = async () => {
      try {
        const latest = await api.get("/teams/map-stats-import/latest");
        if (cancelled || !latest?.exists) return;
        if (latest?.status === "completed") return;
        const applied = applyStatus(latest);
        if (["queued", "running", "pausing", "canceling"].includes(applied.nextStatus)) {
          poll(applied.jobId);
        }
      } catch {
        // The map-stats progress panel is optional on startup.
      }
    };
    hydrateLatest();
    return () => {
      cancelled = true;
    };
  }, []);

  const active = ["queued", "running", "pausing", "canceling"].includes(status);
  return {
    status,
    jobId,
    processed,
    total,
    ok,
    failed,
    lastError,
    etaSeconds,
    start,
    pause,
    cancel,
    resume,
    active,
    resumable: ["paused", "failed"].includes(status),
    show: status !== "idle" && status !== "completed",
    progressPct: total > 0 ? Math.min(100, Math.max(0, (processed / total) * 100)) : 0,
    statusLabel: jobStatusLabel(status),
  };
}

const MapsBarTooltip = ({ active, payload, label }) => {
  if (!active || !payload || !payload.length) return null;
  const row = payload[0]?.payload || {};
  return (
    <div className="card sub" style={{ padding: "8px 12px", border: "1px solid #232a34" }}>
      <p style={{ margin: 0, fontWeight: 600 }}>{label}</p>
      {Number(row.played || 0) === 0 ? (
        <p className="muted" style={{ margin: 0 }}>Not played in the last 3 months</p>
      ) : (
        <p className="muted" style={{ margin: 0 }}>Maps played: {Number(row.played || 0).toLocaleString()}</p>
      )}
      {row.teams !== undefined && Number(row.played || 0) > 0 && (
        <p className="muted" style={{ margin: 0 }}>Teams with data: {row.teams}</p>
      )}
      {row.win_rate !== undefined && row.win_rate !== null && (
        <p className="muted" style={{ margin: 0 }}>Win rate: {(Number(row.win_rate) * 100).toFixed(1)}%</p>
      )}
    </div>
  );
};

// Bar with a white tick marking the team's win rate on that map: the tick sits
// at win_rate percent of the bar's own height (top of bar = 100% win rate).
const MapBarWithWinRate = (props) => {
  const { x, y, width, height, fill, payload } = props;
  const winRate = payload?.win_rate;
  const showTick = winRate !== null && winRate !== undefined && Number.isFinite(winRate) && height > 4;
  const tickY = showTick ? y + height * (1 - Math.min(1, Math.max(0, winRate))) : 0;
  return (
    <g>
      <Rectangle x={x} y={y} width={width} height={height} fill={fill} radius={[4, 4, 0, 0]} />
      {showTick && (
        <line x1={x - 3} x2={x + width + 3} y1={tickY} y2={tickY} stroke="#f8fafc" strokeWidth={2.5} strokeLinecap="round" />
      )}
    </g>
  );
};

function PredictedVetoPanel({ teams }) {
  // Greedy veto prediction from each team's stored pick/ban tendencies
  // (per-team map-profile): bans follow historical ban rates, nudged toward
  // denying the opponent's best map; picks follow pick rates and map strength.
  const [teamAId, setTeamAId] = useState("");
  const [teamBId, setTeamBId] = useState("");
  const [months, setMonths] = useState("3");
  const [bo, setBo] = useState("bo3");
  const [profiles, setProfiles] = useState({});
  const [loading, setLoading] = useState(false);

  const teamOptions = useMemo(
    () => [
      { value: "", label: "Select team" },
      ...(teams || [])
        .slice()
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
        .map((t) => ({ value: String(t.team_id), label: t.name || `Team ${t.team_id}` })),
    ],
    [teams]
  );
  const teamById = useMemo(() => {
    const m = {};
    (teams || []).forEach((t) => (m[String(t.team_id)] = t));
    return m;
  }, [teams]);

  useEffect(() => {
    const ids = [teamAId, teamBId].filter((id) => Number(id) > 0);
    const missing = ids.filter((id) => !profiles[`${id}:${months}`]);
    if (missing.length === 0) return undefined;
    let cancelled = false;
    setLoading(true);
    Promise.all(
      missing.map((id) =>
        api
          .get(`/teams/${id}/map-profile?months=${months}`, 60000)
          .then((d) => ({ id, d }))
          .catch(() => ({ id, d: null }))
      )
    ).then((results) => {
      if (cancelled) return;
      setProfiles((prev) => {
        const next = { ...prev };
        results.forEach(({ id, d }) => {
          if (d?.status === "ok") next[`${id}:${months}`] = d;
        });
        return next;
      });
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [teamAId, teamBId, months, profiles]);

  const profA = profiles[`${teamAId}:${months}`];
  const profB = profiles[`${teamBId}:${months}`];
  const bothSelected = Number(teamAId) > 0 && Number(teamBId) > 0 && teamAId !== teamBId;

  const prediction = useMemo(() => {
    if (!bothSelected || !profA || !profB) return null;
    const index = (prof) => {
      const m = {};
      (prof.maps || []).forEach((r) => {
        m[r.map] = r;
      });
      return m;
    };
    const ra = index(profA);
    const rb = index(profB);
    const val = (row, key, dflt = 0) => {
      const v = row?.[key];
      return v === null || v === undefined ? dflt : Number(v);
    };
    const banScore = (own, opp, m) =>
      val(own[m], "ban_rate") * 3 + val(opp[m], "win_rate", 0.5) - val(own[m], "win_rate", 0.5);
    const pickScore = (own, opp, m) =>
      val(own[m], "pick_rate") * 3 + val(own[m], "win_rate", 0.5) - val(opp[m], "win_rate", 0.5);
    const sequence = {
      bo1: ["ban", "ban", "ban", "ban", "ban", "ban"],
      bo3: ["ban", "ban", "pick", "pick", "ban", "ban"],
      bo5: ["ban", "ban", "pick", "pick", "pick", "pick"],
    }[bo];
    let pool = [...ACTIVE_MAP_POOL];
    const steps = [];
    sequence.forEach((action, i) => {
      const aTurn = i % 2 === 0;
      const own = aTurn ? ra : rb;
      const opp = aTurn ? rb : ra;
      const best = pool
        .map((m) => ({ m, s: action === "ban" ? banScore(own, opp, m) : pickScore(own, opp, m) }))
        .sort((x, y) => y.s - x.s)[0];
      if (!best) return;
      pool = pool.filter((m) => m !== best.m);
      steps.push({ teamId: aTurn ? teamAId : teamBId, action, map: best.m });
    });
    return { steps, decider: pool[0] || null };
  }, [bothSelected, profA, profB, bo, teamAId, teamBId]);

  const renderTeam = (id) => {
    const t = teamById[String(id)];
    return (
      <span className="veto-step-team">
        <TeamLogo hltvTeamId={t?.hltv_team_id} name={t?.name} size={22} />
        <span>{t?.name || "-"}</span>
      </span>
    );
  };
  const lowData =
    bothSelected &&
    profA &&
    profB &&
    (Number(profA.veto_matches || 0) < 5 || Number(profB.veto_matches || 0) < 5);

  return (
    <div className="card sub">
      <h3>Predicted Veto</h3>
      <div className="veto-controls">
        <Select label="Team A" value={teamAId} onChange={setTeamAId} options={teamOptions} />
        <Select label="Team B" value={teamBId} onChange={setTeamBId} options={teamOptions} />
        <Select
          label="Timeframe"
          value={months}
          onChange={setMonths}
          options={[
            { value: "1", label: "Last month" },
            { value: "3", label: "Last 3 months" },
            { value: "6", label: "Last 6 months" },
            { value: "12", label: "Last 12 months" },
          ]}
        />
        <Select
          label="Format"
          value={bo}
          onChange={setBo}
          options={[
            { value: "bo1", label: "Best of 1" },
            { value: "bo3", label: "Best of 3" },
            { value: "bo5", label: "Best of 5" },
          ]}
        />
      </div>
      {!bothSelected ? (
        teamAId && teamAId === teamBId ? <p className="muted">Pick two different teams.</p> : null
      ) : loading && (!profA || !profB) ? (
        <p className="muted">Loading team veto data...</p>
      ) : !prediction ? (
        <p className="muted">No stored veto data for these teams in this window.</p>
      ) : (
        <>
          <p className="muted">
            Based on {Number(profA?.veto_matches || 0)} stored vetoes for {teamById[String(teamAId)]?.name} and{" "}
            {Number(profB?.veto_matches || 0)} for {teamById[String(teamBId)]?.name}. Team A starts the veto.
            {lowData ? " Limited data — treat with caution." : ""}
          </p>
          <div className="veto-steps">
            {prediction.steps.map((s, i) => (
              <div
                className="veto-step"
                key={`veto-${i}`}
                style={{ "--map-color": MAP_BAR_COLORS[s.map] || MAP_BAR_FALLBACK_COLOR }}
              >
                <span className="veto-step-idx">{i + 1}</span>
                {renderTeam(s.teamId)}
                <span className={`veto-action ${s.action}`}>{s.action === "ban" ? "BAN" : "PICK"}</span>
                <span className="veto-step-map">{s.map}</span>
              </div>
            ))}
            {prediction.decider && (
              <div
                className="veto-step decider"
                style={{ "--map-color": MAP_BAR_COLORS[prediction.decider] || MAP_BAR_FALLBACK_COLOR }}
              >
                <span className="veto-step-idx">{prediction.steps.length + 1}</span>
                <span className="veto-step-team muted">Leftover</span>
                <span className="veto-action decider-label">DECIDER</span>
                <span className="veto-step-map">{prediction.decider}</span>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function MapsTab({ teams }) {
  const [mapsSubTab, setMapsSubTab] = useState("popularity");
  const [selectedTeamId, setSelectedTeamId] = useState("");
  // The hover tooltip can float over the donut hole; hide the center totals
  // while a slice is hovered so the two never overlap.
  const [pieHovered, setPieHovered] = useState(false);
  // Pie filters: computed live from stored match results, so the window and
  // the VRS-rank restriction are adjustable (unlike the fixed team-stats
  // scrape). Same filter semantics as the Matches browser.
  const [pieMonths, setPieMonths] = useState("3");
  const [pieRank, setPieRank] = useState("");
  const [pieDir, setPieDir] = useState("within");
  const [pieScope, setPieScope] = useState("both");
  const [pieData, setPieData] = useState(null);
  const [pieLoading, setPieLoading] = useState(false);
  useEffect(() => {
    let cancelled = false;
    // Debounce so typing a rank doesn't fire a request per keystroke.
    const timer = setTimeout(() => {
      setPieLoading(true);
      const rankParams =
        Number(pieRank) > 0
          ? `&vrs_rank=${Number(pieRank)}&vrs_scope=${encodeURIComponent(pieScope)}&vrs_dir=${encodeURIComponent(pieDir)}`
          : "";
      api
        .get(`/events/map-play-distribution?months=${pieMonths}${rankParams}`, 60000)
        .then((d) => {
          if (!cancelled && d?.status === "ok") setPieData(d);
        })
        .catch(() => {})
        .finally(() => {
          if (!cancelled) setPieLoading(false);
        });
    }, 350);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [pieMonths, pieRank, pieDir, pieScope]);

  // Team map profile: computed live from stored matches (map scores + vetoes)
  // so the window is adjustable, unlike the fixed HLTV team-page scrape.
  const [teamMonths, setTeamMonths] = useState("3");
  const [teamProfile, setTeamProfile] = useState(null);
  const [teamProfileLoading, setTeamProfileLoading] = useState(false);
  useEffect(() => {
    if (!selectedTeamId) {
      setTeamProfile(null);
      return undefined;
    }
    let cancelled = false;
    setTeamProfileLoading(true);
    api
      .get(`/teams/${selectedTeamId}/map-profile?months=${teamMonths}`, 60000)
      .then((d) => {
        if (!cancelled && d?.status === "ok") setTeamProfile(d);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setTeamProfileLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTeamId, teamMonths]);

  const teamRows = useMemo(() => {
    const rate = (value) => (value === null || value === undefined ? null : Number(value));
    const rows = (teamProfile?.maps || []).map((row) => {
      const name = String(row.map || "").trim();
      return {
        map: name,
        played: Number(row.played || 0),
        wins: Number(row.wins || 0),
        losses: Number(row.losses || 0),
        win_rate: rate(row.win_rate),
        total_rounds: Number(row.total_rounds || 0),
        pick_rate: rate(row.pick_rate),
        ban_rate: rate(row.ban_rate),
        inPool: ACTIVE_MAP_POOL.includes(name),
      };
    });
    const present = new Set(rows.map((row) => row.map));
    ACTIVE_MAP_POOL.forEach((name) => {
      if (!present.has(name)) {
        rows.push({
          map: name,
          played: 0,
          wins: 0,
          losses: 0,
          win_rate: null,
          total_rounds: 0,
          pick_rate: null,
          ban_rate: null,
          inPool: true,
        });
      }
    });
    return rows.sort((a, b) => b.played - a.played);
  }, [teamProfile]);

  const teamOptions = useMemo(
    () => [
      { value: "", label: "Select team" },
      ...(teams || [])
        .slice()
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
        .map((t) => ({ value: String(t.team_id), label: t.name || `Team ${t.team_id}` })),
    ],
    [teams]
  );

  const formatRate = (value, digits = 1) =>
    value === null || !Number.isFinite(value) ? "-" : `${(value * 100).toFixed(digits)}%`;

  const renderMapBars = (rows) => (
    <ResponsiveContainer width="100%" height={320}>
      <BarChart
        data={rows.map((r) => ({ ...r, label: r.inPool ? r.map : `${r.map} (out)` }))}
        margin={{ top: 12, right: 18, left: 6, bottom: 12 }}
      >
        <CartesianGrid stroke="#232a34" strokeDasharray="3 3" vertical={false} />
        <XAxis
          dataKey="label"
          interval={0}
          tick={{ fill: "#9fb2c9", fontSize: 12 }}
          axisLine={{ stroke: "#3a4452" }}
          tickLine={{ stroke: "#3a4452" }}
        />
        <YAxis
          allowDecimals={false}
          tick={{ fill: "#9fb2c9", fontSize: 12 }}
          axisLine={{ stroke: "#3a4452" }}
          tickLine={{ stroke: "#3a4452" }}
        />
        <Tooltip cursor={{ fill: "rgba(59, 130, 246, 0.08)" }} content={<MapsBarTooltip />} />
        <Bar
          dataKey="played"
          isAnimationActive={false}
          radius={[4, 4, 0, 0]}
          maxBarSize={48}
          shape={<MapBarWithWinRate />}
        >
          {rows.map((r) => (
            <Cell key={r.map} fill={r.inPool ? MAP_BAR_COLORS[r.map] || MAP_BAR_FALLBACK_COLOR : MAP_BAR_FALLBACK_COLOR} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );

  return (
    <Section title="Maps">
      <div className="tab-bar small">
        <button className={mapsSubTab === "popularity" ? "tab active" : "tab"} onClick={() => setMapsSubTab("popularity")}>
          Most Played
        </button>
        <button className={mapsSubTab === "teams" ? "tab active" : "tab"} onClick={() => setMapsSubTab("teams")}>
          Team Map Stats
        </button>
        <button className={mapsSubTab === "veto" ? "tab active" : "tab"} onClick={() => setMapsSubTab("veto")}>
          Predicted Veto
        </button>
      </div>
      <div className="stack">
        {mapsSubTab === "popularity" && (
        <div className="card sub">
          <h3>Most Played Maps</h3>
          {(() => {
            const totalPlayed = Number(pieData?.total_maps || 0);
            // Fold slivers (single show-match maps etc.) into "Other", and
            // only draw outside labels for slices wide enough to label
            // cleanly — thinner ones stay hoverable without colliding text.
            const MIN_SLICE_PCT = 0.012;
            const MIN_LABEL_PCT = 0.022;
            const major = [];
            let otherPlayed = 0;
            (pieData?.maps || [])
              .filter((r) => Number(r.played || 0) > 0)
              .forEach((r) => {
                if (totalPlayed > 0 && Number(r.played) / totalPlayed < MIN_SLICE_PCT) {
                  otherPlayed += Number(r.played);
                } else {
                  major.push(r);
                }
              });
            const pieRows = otherPlayed > 0 ? [...major, { map: "Other", played: otherPlayed }] : major;
            const chart =
              pieRows.length === 0 ? (
                <p className="muted">{pieLoading ? "Loading map data..." : "No stored matches in this window."}</p>
              ) : (
                <div className="pie-wrap" style={pieLoading ? { opacity: 0.6 } : undefined}>
                  <ResponsiveContainer width="100%" height={460}>
                    <PieChart accessibilityLayer={false}>
                      <Pie
                        data={pieRows}
                        dataKey="played"
                        nameKey="map"
                        cx="50%"
                        cy="50%"
                        outerRadius={168}
                        innerRadius={92}
                        stroke="#0a0c10"
                        strokeWidth={2}
                        isAnimationActive={false}
                        onMouseEnter={() => setPieHovered(true)}
                        onMouseLeave={() => setPieHovered(false)}
                        label={({ cx, cy, midAngle, outerRadius: or2, map, played }) => {
                          if (totalPlayed > 0 && Number(played) / totalPlayed < MIN_LABEL_PCT) return null;
                          const rad = (-midAngle * Math.PI) / 180;
                          const cos = Math.cos(rad);
                          const sin = Math.sin(rad);
                          const x = cx + (or2 + 28) * cos;
                          const y = cy + (or2 + 28) * sin;
                          // Near-vertical labels center on the line end (offset
                          // above/below it) so the line points at the text, not
                          // at one end of it.
                          const vertical = Math.abs(cos) < 0.35;
                          return (
                            <text
                              x={x}
                              y={vertical ? y + (sin > 0 ? 11 : -11) : y}
                              textAnchor={vertical ? "middle" : cos > 0 ? "start" : "end"}
                              dominantBaseline="central"
                              fill="#f2f5f9"
                              fontSize={15}
                              fontWeight={600}
                            >
                              {totalPlayed > 0 ? `${map} ${((played / totalPlayed) * 100).toFixed(1)}%` : map}
                            </text>
                          );
                        }}
                        labelLine={(props) => {
                          const { cx, cy, midAngle, outerRadius: or2 } = props;
                          const linePlayed = Number(props?.played ?? props?.payload?.played ?? 0);
                          if (totalPlayed > 0 && linePlayed / totalPlayed < MIN_LABEL_PCT) return null;
                          const rad = (-midAngle * Math.PI) / 180;
                          const sx = cx + (or2 + 3) * Math.cos(rad);
                          const sy = cy + (or2 + 3) * Math.sin(rad);
                          const ex = cx + (or2 + 20) * Math.cos(rad);
                          const ey = cy + (or2 + 20) * Math.sin(rad);
                          return <path d={`M${sx},${sy}L${ex},${ey}`} stroke="#3a4452" fill="none" />;
                        }}
                      >
                        {pieRows.map((r) => (
                          <Cell
                            key={`pie-${r.map}`}
                            fill={r.map === "Other" ? "#64748b" : MAP_BAR_COLORS[r.map] || MAP_BAR_FALLBACK_COLOR}
                          />
                        ))}
                      </Pie>
                      <Tooltip
                        contentStyle={{ background: "#14181f", border: "1px solid #3a4452", borderRadius: 10 }}
                        itemStyle={{ color: "#e9edf3" }}
                        formatter={(value, name) => [
                          `${Number(value).toLocaleString()} maps (${totalPlayed > 0 ? ((Number(value) / totalPlayed) * 100).toFixed(1) : 0}%)`,
                          name,
                        ]}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="pie-center-label" style={{ opacity: pieHovered ? 0 : 1 }}>
                    <div className="pie-center-number">{totalPlayed.toLocaleString()}</div>
                    <div className="pie-center-sub">maps played</div>
                    <div className="pie-center-sub">
                      last {Number(pieMonths)} month{Number(pieMonths) > 1 ? "s" : ""}
                    </div>
                    {Number(pieRank) > 0 && (
                      <div className="pie-center-sub">
                        {pieDir === "within" ? "inside" : "outside"} top {Number(pieRank)}
                      </div>
                    )}
                  </div>
                </div>
              );
            return (
              <div className="pie-layout">
                <div className="pie-filter-col">
                  <Select
                    label="Timeframe"
                    value={pieMonths}
                    onChange={setPieMonths}
                    options={[
                      { value: "1", label: "Last month" },
                      { value: "3", label: "Last 3 months" },
                      { value: "6", label: "Last 6 months" },
                      { value: "12", label: "Last 12 months" },
                    ]}
                  />
                  <Input label="VRS Rank" value={pieRank} onChange={setPieRank} placeholder="e.g. 50" />
                  <Select
                    label="Direction"
                    value={pieDir}
                    onChange={setPieDir}
                    options={[
                      { value: "within", label: "Inside top N" },
                      { value: "outside", label: "Outside top N" },
                    ]}
                  />
                  <Select
                    label="Teams"
                    value={pieScope}
                    onChange={setPieScope}
                    options={[
                      { value: "both", label: "Both teams" },
                      { value: "either", label: "At least one team" },
                    ]}
                  />
                </div>
                {chart}
              </div>
            );
          })()}
        </div>
        )}
        {mapsSubTab === "teams" && (
        <div className="card sub">
          <h3>Team Map Stats</h3>
          <div className="grid two">
            <Select label="Team" value={selectedTeamId} onChange={setSelectedTeamId} options={teamOptions} />
            <Select
              label="Timeframe"
              value={teamMonths}
              onChange={setTeamMonths}
              options={[
                { value: "1", label: "Last month" },
                { value: "3", label: "Last 3 months" },
                { value: "6", label: "Last 6 months" },
                { value: "12", label: "Last 12 months" },
              ]}
            />
          </div>
          {!selectedTeamId ? (
            <p className="muted">Select a team to see its per-map record.</p>
          ) : teamProfileLoading && !teamProfile ? (
            <p className="muted">Loading map data...</p>
          ) : Number(teamProfile?.matches || 0) === 0 && Number(teamProfile?.veto_matches || 0) === 0 ? (
            <p className="muted">No stored matches for this team in this window.</p>
          ) : (
            <>
              {renderMapBars(teamRows)}
              <table>
                <thead>
                  <tr>
                    <th>Map</th>
                    <th>Played</th>
                    <th>W - L</th>
                    <th>Win Rate</th>
                    <th>Rounds</th>
                    <th>Pick Rate</th>
                    <th>Ban Rate</th>
                  </tr>
                </thead>
                <tbody>
                  {teamRows.map((row) => (
                    <tr key={row.map}>
                      <td>{row.map}</td>
                      <td>{row.played}</td>
                      <td>{`${row.wins} - ${row.losses}`}</td>
                      <td>{formatRate(row.win_rate)}</td>
                      <td>{row.total_rounds || "-"}</td>
                      <td>{formatRate(row.pick_rate)}</td>
                      <td>{formatRate(row.ban_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
        )}
        {mapsSubTab === "veto" && <PredictedVetoPanel teams={teams} />}
      </div>
    </Section>
  );
}

// Whole-event 8-team double elimination (StarLadder StarSeries): the 14
// matches in play order with their feeders, mirroring the backend's
// _DOUBLE8_MATCHES (lower semis crossed: each upper-semi loser meets the other
// side's lower-round-1 winner; consolidation final = upper-final loser v
// lower-final winner).
const DOUBLE8_MATCHES = [
  ["ub_r1_1", "ub_r1", ["seed", 0], ["seed", 1]],
  ["ub_r1_2", "ub_r1", ["seed", 2], ["seed", 3]],
  ["ub_r1_3", "ub_r1", ["seed", 4], ["seed", 5]],
  ["ub_r1_4", "ub_r1", ["seed", 6], ["seed", 7]],
  ["ub_sf_1", "ub_sf", ["win", "ub_r1_1"], ["win", "ub_r1_2"]],
  ["ub_sf_2", "ub_sf", ["win", "ub_r1_3"], ["win", "ub_r1_4"]],
  ["lb_r1_1", "lb_r1", ["lose", "ub_r1_1"], ["lose", "ub_r1_2"]],
  ["lb_r1_2", "lb_r1", ["lose", "ub_r1_3"], ["lose", "ub_r1_4"]],
  ["lb_sf_1", "lb_sf", ["lose", "ub_sf_2"], ["win", "lb_r1_1"]],
  ["lb_sf_2", "lb_sf", ["lose", "ub_sf_1"], ["win", "lb_r1_2"]],
  ["ub_final", "ub_final", ["win", "ub_sf_1"], ["win", "ub_sf_2"]],
  ["lb_final", "lb_final", ["win", "lb_sf_1"], ["win", "lb_sf_2"]],
  ["cons_final", "cons_final", ["lose", "ub_final"], ["win", "lb_final"]],
  ["grand_final", "grand_final", ["win", "ub_final"], ["win", "cons_final"]],
];
const DOUBLE8_BY_KEY = Object.fromEntries(DOUBLE8_MATCHES.map((m) => [m[0], m]));
const DOUBLE8_MATCH_TITLE = {
  ub_r1_1: "UB 1",
  ub_r1_2: "UB 2",
  ub_r1_3: "UB 3",
  ub_r1_4: "UB 4",
  ub_sf_1: "UB SF 1",
  ub_sf_2: "UB SF 2",
  ub_final: "UB Final",
  grand_final: "Grand Final",
  lb_r1_1: "LB 1",
  lb_r1_2: "LB 2",
  lb_sf_1: "LB SF 1",
  lb_sf_2: "LB SF 2",
  lb_final: "LB Final",
  cons_final: "Cons. Final",
};
// Columns of each bracket: [stage, match keys, straight]. "straight" rounds
// have one feeder card level with the card it feeds (no two-feeder bar).
const DOUBLE8_UPPER_ROUNDS = [
  ["ub_r1", ["ub_r1_1", "ub_r1_2", "ub_r1_3", "ub_r1_4"], false],
  ["ub_sf", ["ub_sf_1", "ub_sf_2"], false],
  ["ub_final", ["ub_final"], false],
  ["grand_final", ["grand_final"], true],
];
const DOUBLE8_LOWER_ROUNDS = [
  ["lb_r1", ["lb_r1_1", "lb_r1_2"], false],
  ["lb_sf", ["lb_sf_1", "lb_sf_2"], true],
  ["lb_final", ["lb_final"], false],
  ["cons_final", ["cons_final"], true],
];
// Player modal round cards for the double elimination: 3 columns x 4 rows,
// rounds 1-3 (upper row, lower row) then rounds 4-6 (upper row, lower row).
// [row, column, row span]; round 1 has no lower-bracket match, the grand final
// spans both rows of round 6.
const double8FeederLabel = (feeder) =>
  feeder[0] === "seed"
    ? `Seed ${Number(feeder[1]) + 1}`
    : `${feeder[0] === "win" ? "Winner" : "Loser"} ${DOUBLE8_MATCH_TITLE[feeder[1]] || feeder[1]}`;

function PlayoffTab({
  teams,
  teamLookup,
  players,
  sortTeams,
  applyFilters,
  onOpenPlayer,
  variant = "main",
  detectedBracketSize = null,
  detectedBracketKind = "single",
  grandFinalBo = 3,
  routingError = "",
  // The event page lists a third-place decider (kind detector).
  playoffThirdPlace = false,
}) {
  const isBounty = variant === "bounty";
  const [playoffTab, setPlayoffTab] = useState("stage");
  const [events, setEvents] = useState([]);
  const [selectedEventId, setSelectedEventId] = useState("");
  const [eventTeamNames, setEventTeamNames] = useState(new Set());
  const [latestPayload, setLatestPayload] = useState(null);
  const [updatedAt, setUpdatedAt] = useState("");
  const [slots, setSlots] = useState(Array(8).fill(""));
  const [bracketSize, setBracketSize] = useState(8); // every size up to 16 is enumerated exactly
  // "single" (knock-out) or "double" (whole-event double elimination, 8 teams)
  const [bracketKind, setBracketKind] = useState(detectedBracketKind === "double" ? "double" : "single");
  const isDouble = bracketKind === "double";
  // null = stored-sim hydration pending, 0 = no stored sim, else its size.
  const [storedSize, setStoredSize] = useState(null);
  const [storedKind, setStoredKind] = useState(null);
  // completed double-elimination picks: match key -> winner team id (string)
  const [completedDouble, setCompletedDouble] = useState({});
  const [mcSims, setMcSims] = useState(5000); // Monte-Carlo samples, only used for fields beyond 16 teams
  // Self-loading bracket: once the stored-sim hydration has resolved, seed
  // the page from the event automatically whenever the stored sim doesn't
  // match the detected format (or there is none).
  const autoSeededRef = useRef(false);
  useEffect(() => {
    if (isBounty || autoSeededRef.current) return;
    if (storedSize === null) return; // hydration still pending
    const size = Number(detectedBracketSize);
    if (![6, 8, 16].includes(size)) return;
    const wantKind = detectedBracketKind === "double" ? "double" : "single";
    if (storedSize === size && (storedKind || "single") === wantKind) return; // stored sim already fits this format
    autoSeededRef.current = true;
    autofillPlayoffFromEvent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedSize, storedKind, detectedBracketSize, detectedBracketKind]);

  // Fill the seed slots + bracket size straight from the linked HLTV event's
  // single-elimination playoff bracket (16/8), in bracket order.
  const autofillPlayoffFromEvent = async () => {
    if (PUBLIC_BUILD) return;
    setPlayoffAutofillBusy(true);
    setPlayoffAutofillMessage("");
    try {
      const data = await api.post(
        "/playoff/autofill-from-hltv-event",
        { fantasy_event_id: selectedEventId ? Number(selectedEventId) : undefined },
        90000
      );
      if (data?.detail || data?.error) {
        setPlayoffAutofillMessage(String(data.detail || data.error));
        return;
      }
      const ids = (data.team_ids || []).map((x) => String(x || ""));
      const names = data.team_names || [];
      const size = Number(data.bracket_size || ids.length);
      if (![6, 8, 16].includes(size)) {
        setPlayoffAutofillMessage(`Unsupported bracket size (${size}).`);
        return;
      }
      setBracketSize(size);
      setBracketKind(data.bracket_kind === "double" ? "double" : "single");
      setSlots([...ids, ...Array(size).fill("")].slice(0, size));
      setResults(null);
      setTopTeams(null);
      setAllTeams(null);
      setBaseTeams(null);
      setCompletedBracket({ rounds: emptyCompletedRounds(size), third: "" });
      setCompletedDouble({});
      setCompletedBracketResult(null);
      setCompletedBracketMessage("");
      const tbd = names.filter((_, i) => !Number(ids[i]));
      setPlayoffAutofillMessage(
        tbd.length
          ? `Filled ${size}-team bracket; ${tbd.length} seed(s) not matched to a team yet (${tbd.join(", ")}).`
          : `Filled ${size}-team ${data.bracket_kind === "double" ? "double-elimination " : ""}bracket from the event.`
      );
    } catch (e) {
      setPlayoffAutofillMessage(e?.message || "Autofill from HLTV event failed.");
    } finally {
      setPlayoffAutofillBusy(false);
    }
  };
  const [hasThirdPlaceDecider, setHasThirdPlaceDecider] = useState(Boolean(playoffThirdPlace));
  useEffect(() => {
    setHasThirdPlaceDecider(Boolean(playoffThirdPlace));
  }, [playoffThirdPlace]);
  // Bounty draft state: seeds 5-7 pick their QF opponent (seed 8 gets the
  // leftover), and per QF-winner scenario the first SF drafter's pick.
  const [draftPicks, setDraftPicks] = useState(["", "", ""]);
  const [sfPicks, setSfPicks] = useState({});
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState(false);
  const [topTeams, setTopTeams] = useState(null);
  const [allTeams, setAllTeams] = useState(null);
  const [baseTeams, setBaseTeams] = useState(null);
  const [sharedComboCount, setSharedComboCount] = useState(0);
  const [combosApproximate, setCombosApproximate] = useState(false);
  const [sharedCombosUpdatedAt, setSharedCombosUpdatedAt] = useState("");
  const [filteredCount, setFilteredCount] = useState(0);
  const [page, setPage] = useState(0);
  const [topMessage, setTopMessage] = useState("");
  const [includeSet, setIncludeSet] = useState(new Set());
  const [excludeSet, setExcludeSet] = useState(new Set());
  const [includeTeamSet, setIncludeTeamSet] = useState(new Set());
  const [excludeTeamSet, setExcludeTeamSet] = useState(new Set());
  const [appliedIncludeSet, setAppliedIncludeSet] = useState(new Set());
  const [appliedExcludeSet, setAppliedExcludeSet] = useState(new Set());
  const [appliedIncludeTeamSet, setAppliedIncludeTeamSet] = useState(new Set());
  const [appliedExcludeTeamSet, setAppliedExcludeTeamSet] = useState(new Set());
  const [comboSearch, setComboSearch] = useState("");
  const [showFilterModal, setShowFilterModal] = useState(false);
  const [filterSearch, setFilterSearch] = useState("");
  const [sortKey, setSortKey] = useState("ev_desc");
  const [playoffTopSubtab, setPlayoffTopSubtab] = useState("average");
  const [playoffBestMode, setPlayoffBestMode] = useState("average");
  // `rounds` holds one winner-array per round in bracket order (round 0 first,
  // final last); `third` is the third-place decider pick. Generalised from the
  // old {qf,sf,final,third} shape so the picker supports 8- and 16-team fields.
  const [completedBracket, setCompletedBracket] = useState({
    rounds: [["", "", "", ""], ["", ""], [""]],
    third: "",
  });
  const [completedBracketResult, setCompletedBracketResult] = useState(null);
  const [completedBracketMessage, setCompletedBracketMessage] = useState("");
  const [completedBracketUpdatedAt, setCompletedBracketUpdatedAt] = useState("");
  const [showCompletedBracketGraph, setShowCompletedBracketGraph] = useState(false);
  const [completedPlayerBreakdown, setCompletedPlayerBreakdown] = useState(null);
  const [processedSims, setProcessedSims] = useState(0);
  const [totalSims, setTotalSims] = useState(0);
  const [processedCombos, setProcessedCombos] = useState(0);
  const [totalCombos, setTotalCombos] = useState(0);
  const [topEtaSeconds, setTopEtaSeconds] = useState(null);
  const [comboPhase, setComboPhase] = useState("");
  const [completedEtaSeconds, setCompletedEtaSeconds] = useState(null);
  const [etaSeconds, setEtaSeconds] = useState(null);
  const [runMessage, setRunMessage] = useState("");
  const [playoffAutofillBusy, setPlayoffAutofillBusy] = useState(false);
  const [playoffAutofillMessage, setPlayoffAutofillMessage] = useState("");
  const playoffPollingRef = useRef(false);
  const comboQuerySeqRef = useRef(0);
  const normalizeTeamName = (name) => String(name || "").trim().toLowerCase();
  const playerLookup = useMemo(() => {
    const m = {};
    players.forEach((p) => (m[p.player_id] = p.name));
    return m;
  }, [players]);
  const playerById = useMemo(() => {
    const m = {};
    players.forEach((p) => {
      m[Number(p.player_id)] = p;
    });
    return m;
  }, [players]);
  const playerTeamById = useMemo(() => {
    const m = {};
    teams.forEach((team) => {
      [team.player1_id, team.player2_id, team.player3_id, team.player4_id, team.player5_id].forEach((pid) => {
        const id = Number(pid);
        if (Number.isFinite(id) && id > 0) m[id] = Number(team.team_id);
      });
    });
    return m;
  }, [teams]);
  const teamByName = useMemo(() => {
    const m = {};
    teams.forEach((t) => {
      const key = String(t?.name || "").trim().toLowerCase();
      if (key) m[key] = t.team_id;
    });
    return m;
  }, [teams]);
  const teamPlayerOptions = useMemo(() => {
    const safePlayers = Array.isArray(players)
      ? players.filter((p) => p && p.player_id !== null && p.player_id !== undefined)
      : [];
    const sorted = [...safePlayers].sort((a, b) => String(a?.name || "").localeCompare(String(b?.name || "")));
    return [
      { value: "", label: "-" },
      ...sorted.map((p) => ({
        value: String(p.player_id),
        label: p.name || `Player ${p.player_id}`,
      })),
    ];
  }, [players]);
  const playerNameById = playerLookup;
  const filteredTeams = useMemo(() => {
    if (!selectedEventId) return [];
    if (!eventTeamNames || eventTeamNames.size === 0) return [];
    return teams.filter((t) => eventTeamNames.has(normalizeTeamName(t.name)));
  }, [teams, selectedEventId, eventTeamNames]);
  const playoffTeamsForFilters = useMemo(() => {
    const selectedIds = Array.from(new Set(slots.map((s) => Number(s)).filter((id) => Number.isFinite(id) && id > 0)));
    const byId = new Map(teams.map((t) => [Number(t.team_id), t]));
    return selectedIds
      .map((id) => byId.get(id))
      .filter(Boolean)
      .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
  }, [slots, teams]);
  // A team's players for the filters: the roster the stored run was valued
  // with (the event's own lineup, stand-ins included), else the teams table.
  const teamPlayerIds = (teamId) => {
    const fromRun = Object.keys(results?.teams?.[String(teamId)]?.players || {})
      .map((pid) => Number(pid))
      .filter((pid) => Number.isFinite(pid) && pid > 0);
    if (fromRun.length > 0) return fromRun;
    const team = teams.find((t) => Number(t.team_id) === Number(teamId));
    if (!team) return [];
    return [team.player1_id, team.player2_id, team.player3_id, team.player4_id, team.player5_id]
      .map((pid) => Number(pid))
      .filter((pid) => Number.isFinite(pid) && pid > 0);
  };
  // Autocomplete pool for the combo search: names of everyone on a bracket team.
  const comboSearchPidByName = useMemo(() => {
    const m = {};
    playoffTeamsForFilters.forEach((t) => {
      teamPlayerIds(t.team_id).forEach((pid) => {
        const nm = playerNameById[pid];
        if (nm) m[String(nm)] = pid;
      });
    });
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playoffTeamsForFilters, playerNameById, teams, results]);
  const comboSearchNames = useMemo(
    () => Object.keys(comboSearchPidByName).sort((a, b) => a.localeCompare(b)),
    [comboSearchPidByName]
  );
  // Include/Exclude straight from the search dropdown — applied immediately,
  // shown (and removable) in the Player Filters chip strip.
  const addPlayerFilterByName = (name, kind) => {
    const pid = comboSearchPidByName[name];
    if (!pid) return;
    const addTo = (setter) => setter((prev) => new Set(prev).add(pid));
    const dropFrom = (setter) =>
      setter((prev) => {
        const next = new Set(prev);
        next.delete(pid);
        return next;
      });
    if (kind === "include") {
      addTo(setIncludeSet);
      addTo(setAppliedIncludeSet);
      dropFrom(setExcludeSet);
      dropFrom(setAppliedExcludeSet);
    } else {
      addTo(setExcludeSet);
      addTo(setAppliedExcludeSet);
      dropFrom(setIncludeSet);
      dropFrom(setAppliedIncludeSet);
    }
    setComboSearch("");
  };
  const buildEffectivePlayerFilters = (playerInclude, playerExclude, teamInclude, teamExclude) => {
    const include = new Set(playerInclude || []);
    const exclude = new Set(playerExclude || []);
    Array.from(teamInclude || []).forEach((tid) => {
      teamPlayerIds(tid).forEach((pid) => include.add(pid));
    });
    Array.from(teamExclude || []).forEach((tid) => {
      teamPlayerIds(tid).forEach((pid) => exclude.add(pid));
    });
    // Include wins over exclude for conflicting picks.
    Array.from(include).forEach((pid) => exclude.delete(pid));
    return { include, exclude };
  };
  const effectiveAppliedFilters = useMemo(
    () => buildEffectivePlayerFilters(appliedIncludeSet, appliedExcludeSet, appliedIncludeTeamSet, appliedExcludeTeamSet),
    [appliedIncludeSet, appliedExcludeSet, appliedIncludeTeamSet, appliedExcludeTeamSet, teams]
  );

  const setSlot = (idx, val) => {
    setSlots((prev) => {
      const next = [...prev];
      next[idx] = val;
      return next;
    });
  };
  const playoffTeamOptions = useMemo(
    () => [
      { value: "", label: "Select team" },
      ...filteredTeams.map((t) => ({ value: String(t.team_id), label: t.name || `Team ${t.team_id}` })),
    ],
    [filteredTeams]
  );
  const teamInitials = (teamId) => {
    const name = teamLookup[Number(teamId)] || "";
    const parts = String(name).trim().split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return `${parts[0][0] || ""}${parts[1][0] || ""}`.toUpperCase();
    return String(name || "?").slice(0, 2).toUpperCase();
  };
  const hltvIdByTeamId = useMemo(() => {
    const m = {};
    teams.forEach((t) => {
      if (t.hltv_team_id) m[Number(t.team_id)] = Number(t.hltv_team_id);
    });
    return m;
  }, [teams]);
  const TeamBadge = ({ teamId }) => {
    const id = Number(teamId);
    if (!Number.isFinite(id) || id <= 0) return <span className="playoff-team-badge empty">?</span>;
    return <TeamLogo hltvTeamId={hltvIdByTeamId[id]} name={teamLookup[id]} size={24} />;
  };
  // Bracket geometry helpers shared by the seeding view and the completed-bracket
  // picker so both work for any power-of-two field (exact up to 16 teams).
  const bracketTotalRounds = (n) => (n >= 2 ? Math.round(Math.log2(n)) : 0);
  const roundTitleForTeams = (teamsInRound) =>
    ({ 2: "Grand final", 4: "Semi-finals", 8: "Quarter-finals", 16: "Round of 16", 32: "Round of 32" }[teamsInRound] ||
      `Round of ${teamsInRound}`);
  const roundShortForTeams = (teamsInRound) =>
    ({ 2: "F", 4: "SF", 8: "QF", 16: "R16", 32: "R32" }[teamsInRound] || `R${teamsInRound}`);
  const roundNameForTeams = (teamsInRound) =>
    ({ 2: "final", 4: "semis", 8: "quarters", 16: "round_of_16", 32: "round_of_32" }[teamsInRound] ||
      `round_of_${teamsInRound}`);
  const emptyCompletedRounds = (n) => {
    const rounds = [];
    const total = bracketTotalRounds(n);
    for (let r = 0; r < total; r++) rounds.push(Array(n >> (r + 1)).fill(""));
    return rounds;
  };
  // Display-only: seeds load from the event automatically, no manual picking.
  const BracketTeamRow = ({ slotIndex, placeholder, muted = false }) => {
    const selectedTeamId = slotIndex !== null && slotIndex !== undefined ? slots[slotIndex] : "";
    const hasTeam = Boolean(selectedTeamId);
    return (
      <div className={`playoff-team-row ${muted ? "muted" : ""}`}>
        <TeamBadge teamId={hasTeam ? selectedTeamId : 0} />
        {hasTeam ? (
          <span>{teamLookup[Number(selectedTeamId)] || `Team ${selectedTeamId}`}</span>
        ) : (
          <span className="playoff-team-tbd">{placeholder || "TBD"}</span>
        )}
      </div>
    );
  };
  const CompletedBracketTeamRow = ({ teamId, selected, onSelect, placeholder = "TBD", muted = false }) => {
    const hasTeam = Boolean(teamId);
    return (
      <button
        type="button"
        className={`playoff-team-row completed-pick ${muted ? "muted" : ""} ${selected ? "active" : ""}`}
        onClick={() => hasTeam && onSelect(String(teamId))}
        disabled={!hasTeam}
      >
        <TeamBadge teamId={hasTeam ? teamId : 0} />
        <span>{hasTeam ? teamLookup[Number(teamId)] || `Team ${teamId}` : placeholder}</span>
      </button>
    );
  };
  const BracketMatchCard = ({ title, meta = "BO3", rows, className = "" }) => (
    <div className={`playoff-match-card ${className}`}>
      <div className="playoff-match-head">
        <strong>{title}</strong>
        <span>{meta}</span>
      </div>
      <div className="playoff-match-teams">{rows}</div>
    </div>
  );
  const matchCardTitle = (teamsInRound, matchIdx) =>
    teamsInRound === 2 ? "Final" : `${roundShortForTeams(teamsInRound)} ${matchIdx + 1}`;
  const matchCardMeta = (teamsInRound) => (teamsInRound === 2 ? `BO${grandFinalBo || 3}` : "BO3");
  // Seeding view (Run tab): round 0 has the seed selects; later rounds show the
  // muted winner placeholders. Auto-distributed via .pb-bracket for any size.
  const renderSeedingBracket = () => {
    const n = bracketSize;
    if (isDouble) return renderDoubleBracket("seed");
    if (n === 6) {
      // Byes bracket: slots [SF bye 1, QF1a, QF1b, QF2a, QF2b, SF bye 2].
      return (
        <div className="pb-bracket">
          <div className="pb-round">
            <div className="pb-round-title">Quarter-finals</div>
            <div className="pb-matches">
              <div className="pb-match-wrap">
                <BracketMatchCard
                  title="QF 1"
                  meta="BO3"
                  rows={
                    <>
                      <BracketTeamRow slotIndex={1} />
                      <BracketTeamRow slotIndex={2} />
                    </>
                  }
                />
              </div>
              <div className="pb-match-wrap">
                <BracketMatchCard
                  title="QF 2"
                  meta="BO3"
                  rows={
                    <>
                      <BracketTeamRow slotIndex={3} />
                      <BracketTeamRow slotIndex={4} />
                    </>
                  }
                />
              </div>
            </div>
          </div>
          <div className="pb-round">
            <div className="pb-round-title">Semi-finals</div>
            <div className="pb-matches">
              <div className="pb-match-wrap">
                <BracketMatchCard
                  title="SF 1"
                  meta="BO3"
                  rows={
                    <>
                      <BracketTeamRow slotIndex={0} />
                      <BracketTeamRow placeholder="Winner QF 1" muted />
                    </>
                  }
                />
              </div>
              <div className="pb-match-wrap">
                <BracketMatchCard
                  title="SF 2"
                  meta="BO3"
                  rows={
                    <>
                      <BracketTeamRow slotIndex={5} />
                      <BracketTeamRow placeholder="Winner QF 2" muted />
                    </>
                  }
                />
              </div>
            </div>
          </div>
          <div className="pb-round">
            <div className="pb-round-title">Grand final</div>
            <div className="pb-matches">
              <div className="pb-match-wrap">
                <BracketMatchCard
                  title="FINAL"
                  meta={`BO${grandFinalBo || 3}`}
                  rows={
                    <>
                      <BracketTeamRow placeholder="Winner SF 1" muted />
                      <BracketTeamRow placeholder="Winner SF 2" muted />
                    </>
                  }
                />
              </div>
            </div>
          </div>
        </div>
      );
    }
    const totalRounds = bracketTotalRounds(n);
    return (
      <div className="pb-bracket">
        {Array.from({ length: totalRounds }, (_, r) => {
          const teamsInRound = n >> r;
          const matches = n >> (r + 1);
          const feederShort = r > 0 ? roundShortForTeams(n >> (r - 1)) : "";
          return (
            <div className="pb-round" key={`seed-round-${r}`}>
              <div className="pb-round-title">{roundTitleForTeams(teamsInRound)}</div>
              <div className="pb-matches">
                {Array.from({ length: matches }, (_, m) => (
                  <div className="pb-match-wrap" key={`seed-${r}-${m}`}>
                    <BracketMatchCard
                      title={matchCardTitle(teamsInRound, m)}
                      meta={matchCardMeta(teamsInRound)}
                      rows={
                        r === 0 ? (
                          <>
                            <BracketTeamRow slotIndex={2 * m} />
                            <BracketTeamRow slotIndex={2 * m + 1} />
                          </>
                        ) : (
                          <>
                            <BracketTeamRow placeholder={`Winner ${feederShort} ${2 * m + 1}`} muted />
                            <BracketTeamRow placeholder={`Winner ${feederShort} ${2 * m + 2}`} muted />
                          </>
                        )
                      }
                    />
                  </div>
                ))}
                {hasThirdPlaceDecider && !isBounty && (
                  <div className={`pb-match-wrap pb-aux${r === totalRounds - 1 ? "" : " pb-spacer"}`} aria-hidden={r !== totalRounds - 1}>
                    <BracketMatchCard
                      title="3rd place"
                      meta="BO3"
                      rows={
                        <>
                          <BracketTeamRow placeholder="Loser SF 1" muted />
                          <BracketTeamRow placeholder="Loser SF 2" muted />
                        </>
                      }
                    />
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    );
  };
  // Completed-bracket picker: the same auto-distributed bracket, but each row is
  // a clickable winner pick sourced from completedBracketDerived.roundsPairs.
  const renderCompletedPicker = () => {
    const n = slots.length;
    const totalRounds = bracketTotalRounds(n);
    const { roundsPairs, thirdPair } = completedBracketDerived;
    return (
      <>
        <div className="pb-bracket">
          {Array.from({ length: totalRounds }, (_, r) => {
            const teamsInRound = n >> r;
            const matches = n >> (r + 1);
            const pairs = roundsPairs[r] || [];
            const picks = (completedBracket.rounds || [])[r] || [];
            return (
              <div className="pb-round" key={`cmp-round-${r}`}>
                <div className="pb-round-title">{roundTitleForTeams(teamsInRound)}</div>
                <div className="pb-matches">
                  {Array.from({ length: matches }, (_, m) => {
                    const pair = pairs[m] || ["", ""];
                    return (
                      <div className="pb-match-wrap" key={`cmp-${r}-${m}`}>
                        <BracketMatchCard
                          title={matchCardTitle(teamsInRound, m)}
                          meta={matchCardMeta(teamsInRound)}
                          rows={pair.map((teamId, rowIdx) => (
                            <CompletedBracketTeamRow
                              key={`cmp-${r}-${m}-${rowIdx}`}
                              teamId={teamId}
                              selected={Boolean(teamId) && String(picks[m]) === String(teamId)}
                              onSelect={(val) => setCompletedPick(r, m, val)}
                              placeholder="TBD"
                            />
                          ))}
                        />
                      </div>
                    );
                  })}
                  {hasThirdPlaceDecider && !isBounty && (
                    <div className={`pb-match-wrap pb-aux${r === totalRounds - 1 ? "" : " pb-spacer"}`} aria-hidden={r !== totalRounds - 1}>
                      <BracketMatchCard
                        title="3rd place"
                        rows={thirdPair.map((teamId, idx) => (
                          <CompletedBracketTeamRow
                            key={`cmp-third-${r}-${idx}`}
                            teamId={r === totalRounds - 1 ? teamId : ""}
                            selected={r === totalRounds - 1 && Boolean(teamId) && String(completedBracket.third) === String(teamId)}
                            onSelect={(val) => setCompletedThird(val)}
                            placeholder={idx === 0 ? "Loser SF 1" : "Loser SF 2"}
                          />
                        ))}
                      />
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </>
    );
  };
  const setCompletedPick = (roundIdx, matchIdx, value) => {
    setCompletedBracket((prev) => {
      const rounds = (prev.rounds || []).map((r) => [...r]);
      if (!rounds[roundIdx]) return prev;
      rounds[roundIdx][matchIdx] = value;
      // Invalidate downstream picks that depended on this result.
      if (isBounty && roundIdx === 0) {
        // Bounty semis are re-drafted from the surviving four, so any QF change
        // invalidates the entire SF round (and everything after it).
        for (let r = 1; r < rounds.length; r++) rounds[r] = rounds[r].map(() => "");
      } else {
        // Standard single-elim: only the descendant match in each later round
        // depended on this pick.
        let descendant = matchIdx;
        for (let r = roundIdx + 1; r < rounds.length; r++) {
          descendant = Math.floor(descendant / 2);
          if (rounds[r] && rounds[r][descendant] !== undefined) rounds[r][descendant] = "";
        }
      }
      return { ...prev, rounds, third: "" };
    });
    setCompletedBracketResult(null);
    setCompletedBracketMessage("");
  };
  const setCompletedThird = (value) => {
    setCompletedBracket((prev) => ({ ...prev, third: value }));
    setCompletedBracketResult(null);
    setCompletedBracketMessage("");
  };
  const hydrateCompletedBracketFromBracket = (bracket, n = bracketSize) => {
    if (!bracket) return;
    const totalRounds = bracketTotalRounds(n);
    const rounds = [];
    let anyPicked = false;
    for (let r = 0; r < totalRounds; r++) {
      const teamsInRound = n >> r;
      const matches = bracket[roundNameForTeams(teamsInRound)] || [];
      const winners = [];
      for (let i = 0; i < n >> (r + 1); i++) {
        const w = matches[i]?.winner ? String(matches[i].winner) : "";
        if (w) anyPicked = true;
        winners.push(w);
      }
      rounds.push(winners);
    }
    const thirdWinner = (bracket.third_place || [])[0]?.winner ? String(bracket.third_place[0].winner) : "";
    if (anyPicked || thirdWinner) setCompletedBracket({ rounds, third: thirdWinner });
  };
  const openScoringBreakdown = (row) => {
    if (!row) return;
    const pid = Number(row.player_id);
    setCompletedPlayerBreakdown({
      ...row,
      player_id: Number.isFinite(pid) ? pid : row.player_id,
      name: row.name || playerLookup[pid] || `Player ${row.player_id || ""}`,
      team_id: Number(row.team_id || playerTeamById[pid] || 0),
      points: Number(row.points ?? row.total_ev ?? 0),
    });
  };
  const openProjectedRosterBreakdown = (player, team) => {
    const roleNum = Number(player.role_name);
    const roleId =
      player.role_name != null && player.role_name !== "" && Number.isFinite(roleNum) ? roleNum : roleIdFromName(player.role_name);
    const assignments = (team?.booster_assignments || []).filter((a) => Number(a.player_id) === Number(player.player_id));
    const rosterCtx =
      (team?.booster_assignments || []).length > 0
        ? rosterRoleContext(
            players,
            { role_id: roleId, role_ev: Number(player.role_ev || 0), booster_ev: Number(player.booster_ev || 0), boosters: assignments },
            player.player_id
          )
        : null;
    if (playoffBestMode === "most_outcomes" && Array.isArray(team?.winning_outcome_indexes) && team.winning_outcome_indexes.length > 0) {
      api
        .post(
          "/playoff/winning-player-detail",
          {
            variant,
            outcome_indexes: team.winning_outcome_indexes,
            player_id: Number(player.player_id),
            role_id: roleId,
            booster_assignments: team.booster_assignments || [],
          },
          120000
        )
        .then((data) => {
          if (data?.detail) {
            openScoringBreakdown({ ...player, roster: rosterCtx });
            return;
          }
          openScoringBreakdown({ ...player, roster: rosterCtx, win_detail: data, win_odds: team.outcome_win_probability });
        })
        .catch(() => openScoringBreakdown({ ...player, roster: rosterCtx }));
      return;
    }
    if (playoffBestMode === "single_outcome" && team?.ceiling_outcome_index != null) {
      api
        .post(
          "/playoff/outcome-player-detail",
          {
            variant,
            outcome_index: Number(team.ceiling_outcome_index),
            player_id: Number(player.player_id),
            role_id: roleId,
            booster_assignments: team.booster_assignments || [],
          },
          60000
        )
        .then((data) => {
          if (data?.detail) {
            openScoringBreakdown({ ...player, roster: rosterCtx });
            return;
          }
          openScoringBreakdown({ ...player, roster: rosterCtx, ceiling_detail: data, ceiling_odds: team.ceiling_probability });
        })
        .catch(() => openScoringBreakdown({ ...player, roster: rosterCtx }));
      return;
    }
    openScoringBreakdown({ ...player, roster: rosterCtx });
  };
  // The stored run's stage stats in the shared modal's contract. The
  // double-elimination run stores it directly (stages / rounds / layout); the
  // knock-out runs' per-stage cells (opponent list, most likely booster) are
  // converted here, the elimination credit being the residual over the
  // opponents' conditional points.
  const adaptPlayoffBreakdown = (row) => {
    if (!row) return null;
    const stageStats = results?.stage_stats || null;
    const pid = Number(row.player_id);
    const tid = Number(row.team_id || 0);
    const stageRow = stageStats?.players?.[String(pid)] || null;
    const teamRow = stageStats?.teams?.[String(tid)] || null;
    const stageKeys = Array.isArray(stageStats?.stages) ? stageStats.stages : [];
    const layout = Array.isArray(stageStats?.layout) ? stageStats.layout : [];
    const compRow = results?.teams?.[String(tid)]?.players?.[String(pid)] || null;
    const roleInfo = row.role_info || compRow?.role_info || stageRow?.role || null;
    const slotByBooster = {};
    (row.booster_slots || compRow?.booster_slots || []).forEach((b) => {
      if (b.booster_id != null) slotByBooster[Number(b.booster_id)] = b.slot;
    });
    const team_ranks = {};
    Object.entries(stageStats?.teams || {}).forEach(([k, t]) => {
      if (t?.rank != null) team_ranks[k] = t.rank;
    });
    let stage_list = [];
    let stage_ev = {};
    let team_rounds = {};
    if (stageRow?.stages) {
      stage_ev = stageRow.stages;
      team_rounds = teamRow?.rounds || {};
      stage_list = stageKeys
        .filter((k) => stage_ev[k] || team_rounds[k])
        .map((k) => {
          const m = layout.find((l) => l.key === k) || {};
          return {
            key: k,
            label: k,
            full: m.full || STAGE_FULL[k] || k,
            phase: "playoff",
            section: m.section || "playoff",
            section_title: m.section_title || "Playoffs",
            bracket: m.bracket || null,
            opener: Boolean(m.opener),
            pad: Boolean(m.pad),
          };
        });
    } else if (stageRow) {
      stageKeys.forEach((k) => {
        const cell = stageRow[k];
        if (!cell || typeof cell !== "object") return;
        const opps = Array.isArray(cell.opponents) ? cell.opponents : [];
        const play = opps.reduce((a, o) => a + Number(o.prob || 0), 0);
        const winMass = opps.reduce((a, o) => a + Number(o.prob || 0) * Number(o.win_chance || 0), 0);
        const oppEv = opps.reduce((a, o) => a + Number(o.prob || 0) * Number(o.total || 0), 0);
        const reach = Number(teamRow?.reach?.[k] ?? play);
        const isBye = reach < 0.0005 && Math.abs(Number(cell.total || 0)) > 0.005;
        const elimPts = isBye ? 0 : Number(cell.total || 0) - oppEv;
        const elimProb = isBye ? 0 : Math.max(0, 1 - play);
        const opponentsEv = {};
        const opponentsRound = {};
        opps.forEach((o) => {
          const t = String(o.team_id);
          const p = Number(o.prob || 0);
          opponentsEv[t] = {
            rating: p * Number(o.rating || 0),
            win: p * Number(o.win || 0),
            role: p * Number(o.role || 0),
            booster: p * Number(o.booster || 0),
            total: p * Number(o.total || 0),
          };
          opponentsRound[t] = { play: p, win: Number(o.win_chance || 0) };
          if (o.rank != null) team_ranks[t] = o.rank;
        });
        stage_ev[k] = {
          rating: Number(cell.rating || 0),
          win: Number(cell.win || 0),
          role: Number(cell.role || 0),
          booster: Number(cell.booster || 0),
          total: Number(cell.total || 0),
          boosters:
            cell.booster_id != null
              ? [
                  {
                    booster_id: cell.booster_id,
                    booster_name: cell.booster_name,
                    booster_rate: Number(cell.booster_rate || 0),
                    slot: slotByBooster[Number(cell.booster_id)] ?? null,
                    share: 1,
                  },
                ]
              : [],
          opponents: opponentsEv,
        };
        team_rounds[k] = {
          play,
          win: play > 0 ? winMass / play : 0,
          opponents: opponentsRound,
          elim_before: { prob: Math.abs(elimPts) > 0.005 ? elimProb : 0, points: Math.abs(elimPts) > 0.005 ? elimPts : 0 },
          bye: isBye ? 1 : 0,
        };
        stage_list.push({ key: k, label: k, full: STAGE_FULL[k] || k, phase: "playoff", section: "playoff", section_title: "Playoffs", pad: isBye });
      });
    }
    const wd = row.win_detail || null;
    if (wd) {
      const c = wd.components || {};
      const layout = Array.isArray(wd.layout) ? wd.layout : [];
      const list = (wd.stages || [])
        .filter((k) => wd.stage_ev?.[k] || wd.team_rounds?.[k])
        .map((k) => {
          const m = layout.find((l) => l.key === k) || {};
          return {
            key: k,
            label: k,
            full: m.full || STAGE_FULL[k] || k,
            phase: "playoff",
            section: m.section || "playoff",
            section_title: m.section_title || "Playoffs",
            bracket: m.bracket || null,
            opener: Boolean(m.opener),
            pad: Boolean(m.pad),
          };
        });
      const pct = Number(row.win_odds || wd.mass || 0) * 100;
      return {
        player_id: pid,
        name: row.name || playerLookup[pid] || `Player ${pid}`,
        team_id: tid,
        role_id: row.roster?.role_id ?? row.role_id ?? compRow?.role_id ?? null,
        rating: Number(c.rating || 0),
        win: Number(c.win || 0),
        role: Number(c.role || 0),
        booster: Number(c.booster || 0),
        total: Number(c.total || 0),
        stage_list: list,
        stage_ev: wd.stage_ev || {},
        team_rounds: wd.team_rounds || {},
        team_ranks: { ...team_ranks, ...(wd.team_ranks || {}) },
        role_info: roleInfo,
        hero_note: `in the ${Number(wd.n_outcomes || 0).toLocaleString()} brackets this roster wins · ${pct.toFixed(1)}%`,
        runs: wd.runs || null,
        runs_rest: wd.runs_rest || null,
        padding: null,
        padding_prob: 0,
        playoff_padding: null,
        playoff_pad_prob: 0,
      };
    }
    const detail = row.ceiling_detail || null;
    if (detail) {
      const pct = Number(row.ceiling_odds || detail.probability || 0) * 100;
      const shown = pct >= 1 ? pct.toFixed(1) : pct >= 0.1 ? pct.toFixed(2) : pct.toFixed(3);
      const c = detail.components || {};
      return {
        player_id: pid,
        name: row.name || playerLookup[pid] || `Player ${pid}`,
        team_id: tid,
        role_id: row.roster?.role_id ?? row.role_id ?? compRow?.role_id ?? null,
        rating: Number(c.rating || 0),
        win: Number(c.win || 0),
        role: Number(c.role || 0),
        booster: Number(c.booster || 0),
        total: Number(c.total || 0),
        stage_list: [],
        stage_ev: {},
        team_rounds: {},
        team_ranks,
        role_info: roleInfo,
        hero_note: `this roster's best bracket · ${shown}% chance`,
        outcome_bracket: detail.bracket || null,
        match_rows: Array.isArray(detail.rows) ? detail.rows : [],
      };
    }
    const matchRows = Array.isArray(row.point_breakdown) && row.point_breakdown.length > 0 ? row.point_breakdown : [];
    return {
      player_id: pid,
      name: row.name || playerLookup[pid] || `Player ${pid}`,
      team_id: tid,
      role_id: row.role_id ?? compRow?.role_id ?? roleInfo?.role_id ?? null,
      rating: completedBreakdownValue(row, "rating"),
      win: completedBreakdownValue(row, "win"),
      role: completedBreakdownValue(row, "role"),
      booster: completedBreakdownValue(row, "booster"),
      total: completedBreakdownValue(row, "total"),
      stage_list: matchRows.length > 0 ? [] : stage_list,
      stage_ev,
      team_rounds,
      team_ranks,
      role_info: roleInfo,
      padding: null,
      padding_prob: 0,
      playoff_padding: null,
      playoff_pad_prob: 0,
      roster: row.roster || null,
      note:
        row.components_available === false
          ? "This stored bracket was generated before source components were saved. Re-run the playoff bracket to see the split."
          : matchRows.length > 0
            ? "Points for this exact bracket."
            : row.note || null,
      match_rows: matchRows,
    };
  };
  const seedIndexById = useMemo(() => {
    const m = {};
    slots.forEach((s, idx) => {
      const id = Number(s);
      if (Number.isFinite(id) && id > 0) m[id] = idx;
    });
    return m;
  }, [slots]);
  const draftedQfPairs = useMemo(() => {
    if (!isBounty) return null;
    const top = slots.slice(0, 4).map((s) => Number(s));
    const bottom = slots.slice(4, 8).map((s) => Number(s));
    if (top.some((id) => !id) || bottom.some((id) => !id)) return null;
    const picks = draftPicks.map((p) => Number(p));
    if (picks.some((p) => !p)) return null;
    if (new Set(picks).size !== 3 || picks.some((p) => !top.includes(p))) return null;
    const leftover = top.find((id) => !picks.includes(id));
    return bottom.map((drafter, idx) => [drafter, idx < 3 ? picks[idx] : leftover]);
  }, [isBounty, slots, draftPicks]);
  // The 16 possible QF-winner scenarios; in each, the two lowest-seeded
  // survivors re-draft: the higher of them picks first from the top two.
  const bountySfScenarios = useMemo(() => {
    if (!isBounty || !draftedQfPairs) return [];
    const scenarios = [];
    for (let mask = 0; mask < 16; mask++) {
      const winners = draftedQfPairs.map((pair, i) => pair[(mask >> i) & 1]);
      const ordered = [...winners].sort((a, b) => (seedIndexById[a] ?? 99) - (seedIndexById[b] ?? 99));
      const key = [...winners].sort((a, b) => a - b).join("-");
      scenarios.push({ key, winners, ordered, drafter: ordered[2], options: [ordered[0], ordered[1]] });
    }
    return scenarios;
  }, [isBounty, draftedQfPairs, seedIndexById]);
  const bountySfPairsFor = (winners) => {
    const ordered = [...winners.map(Number)].sort((a, b) => (seedIndexById[a] ?? 99) - (seedIndexById[b] ?? 99));
    const key = [...winners.map(Number)].sort((a, b) => a - b).join("-");
    const options = [ordered[0], ordered[1]];
    let pick = Number(sfPicks[key]);
    if (!options.includes(pick)) pick = ordered[1];
    const other = options.find((id) => id !== pick);
    return [
      [ordered[2], pick],
      [ordered[3], other],
    ];
  };
  const bountySfPicksPayload = () => {
    const out = {};
    bountySfScenarios.forEach((sc) => {
      out[sc.key] = bountySfPairsFor(sc.winners);
    });
    return out;
  };
  const completedBracketDerived = useMemo(() => {
    const isPickedFrom = (value, ids) => Boolean(value) && ids.some((id) => String(id) === String(value));
    const n = slots.length;
    const totalRounds = bracketTotalRounds(n);
    const picks = completedBracket.rounds || [];
    // The match-ups a user picks from, per round. Round 0 is the seeded field
    // (or the bounty draft); later rounds are formed from the previous round's
    // picked winners (bounty semis are re-drafted from the surviving four).
    const roundsPairs = [];
    for (let r = 0; r < totalRounds; r++) {
      const matches = n >> (r + 1);
      let pairs;
      if (r === 0) {
        pairs = isBounty
          ? (draftedQfPairs || Array.from({ length: matches }, () => ["", ""])).map((pair) =>
              pair.map((id) => (id ? String(id) : ""))
            )
          : Array.from({ length: matches }, (_, i) => [slots[2 * i] || "", slots[2 * i + 1] || ""]);
      } else if (isBounty && r === 1) {
        const qfPicks = picks[0] || [];
        pairs =
          qfPicks.length === n / 2 && qfPicks.every(Boolean)
            ? bountySfPairsFor(qfPicks).map((pair) => pair.map((id) => (id ? String(id) : "")))
            : Array.from({ length: matches }, () => ["", ""]);
      } else {
        const prev = picks[r - 1] || [];
        pairs = Array.from({ length: matches }, (_, i) => [prev[2 * i] || "", prev[2 * i + 1] || ""]);
      }
      roundsPairs.push(pairs);
    }
    // Third-place decider is between the two semi-final losers (the 4-team round).
    const semiRoundIdx = totalRounds - 2;
    let thirdPair = ["", ""];
    if (semiRoundIdx >= 0 && !isBounty) {
      const semiPairs = roundsPairs[semiRoundIdx] || [];
      const semiPicks = picks[semiRoundIdx] || [];
      thirdPair = semiPairs.map((pair, idx) => pair.find((id) => id && String(id) !== String(semiPicks[idx])) || "");
    }
    let complete = totalRounds > 0;
    for (let r = 0; r < totalRounds && complete; r++) {
      const roundPicks = picks[r] || [];
      const pairs = roundsPairs[r] || [];
      if (roundPicks.length !== pairs.length) complete = false;
      else complete = pairs.every((pair, i) => isPickedFrom(roundPicks[i], pair));
    }
    if (complete && hasThirdPlaceDecider && !isBounty) {
      complete = isPickedFrom(completedBracket.third, thirdPair);
    }
    return { roundsPairs, thirdPair, semiRoundIdx, complete };
  }, [slots, completedBracket, hasThirdPlaceDecider, isBounty, draftedQfPairs, sfPicks, seedIndexById]);
  // Double elimination: the teams in every match follow from the seeds and the
  // picks so far (a pick that is no longer one of its match's teams is dropped).
  const doubleDerived = useMemo(() => {
    const winners = {};
    const losers = {};
    const pairs = {};
    let complete = true;
    DOUBLE8_MATCHES.forEach(([key, _stage, fa, fb]) => {
      const feed = (f) => (f[0] === "seed" ? slots[f[1]] || "" : (f[0] === "win" ? winners[f[1]] : losers[f[1]]) || "");
      const a = String(feed(fa) || "");
      const b = String(feed(fb) || "");
      pairs[key] = [a, b];
      const pick = String(completedDouble[key] || "");
      if (pick && a && b && (pick === a || pick === b)) {
        winners[key] = pick;
        losers[key] = pick === a ? b : a;
      } else {
        winners[key] = "";
        losers[key] = "";
        complete = false;
      }
    });
    return { pairs, winners, losers, complete };
  }, [slots, completedDouble]);
  const completedComplete = isDouble ? doubleDerived.complete : completedBracketDerived["complete"];
  const setDoublePick = (key, value) => {
    setCompletedDouble((prev) => {
      const next = { ...prev, [key]: value };
      // every match fed (directly or through others) by this one depended on its result
      const dependents = new Set([key]);
      let grew = true;
      while (grew) {
        grew = false;
        DOUBLE8_MATCHES.forEach(([k, _s, fa, fb]) => {
          if (dependents.has(k)) return;
          if ((fa[0] !== "seed" && dependents.has(fa[1])) || (fb[0] !== "seed" && dependents.has(fb[1]))) {
            dependents.add(k);
            grew = true;
          }
        });
      }
      dependents.forEach((k) => {
        if (k !== key) delete next[k];
      });
      return next;
    });
    setCompletedBracketResult(null);
    setCompletedBracketMessage("");
  };
  const hydrateCompletedDoubleFromBracket = (bracket) => {
    if (!bracket) return;
    const picks = {};
    Object.values(bracket).forEach((matches) => {
      (matches || []).forEach((m) => {
        if (m?.key && m?.winner) picks[m.key] = String(m.winner);
      });
    });
    if (Object.keys(picks).length > 0) setCompletedDouble(picks);
  };
  // Upper and lower brackets of the double elimination, as the seeding view
  // (mode "seed": seeds + muted feeder placeholders) or the completed-bracket
  // picker (mode "pick": clickable winners).
  const renderDoubleBracket = (mode) => {
    const picker = mode === "pick";
    const metaFor = (key) => (key === "grand_final" ? `BO${grandFinalBo || 5}` : "BO3");
    const rowFor = (key, feeder, i) => {
      if (picker) {
        const teamId = (doubleDerived.pairs[key] || ["", ""])[i];
        return (
          <CompletedBracketTeamRow
            key={`${key}-${i}`}
            teamId={teamId}
            selected={Boolean(teamId) && String(completedDouble[key] || "") === String(teamId)}
            onSelect={(val) => setDoublePick(key, val)}
            placeholder={double8FeederLabel(feeder)}
          />
        );
      }
      if (feeder[0] === "seed") return <BracketTeamRow key={`${key}-${i}`} slotIndex={feeder[1]} />;
      return <BracketTeamRow key={`${key}-${i}`} placeholder={double8FeederLabel(feeder)} muted />;
    };
    const bracket = (rounds) => (
      <div className="pb-bracket">
        {rounds.map(([stage, keys, straight]) => (
          <div className={`pb-round${straight ? " straight" : ""}`} key={`${mode}-${stage}`}>
            <div className="pb-round-title">{STAGE_FULL[stage] || stage}</div>
            <div className="pb-matches">
              {keys.map((key) => {
                const m = DOUBLE8_BY_KEY[key];
                return (
                  <div className="pb-match-wrap" key={`${mode}-${key}`}>
                    <BracketMatchCard
                      title={DOUBLE8_MATCH_TITLE[key] || key}
                      meta={metaFor(key)}
                      rows={
                        <>
                          {rowFor(key, m[2], 0)}
                          {rowFor(key, m[3], 1)}
                        </>
                      }
                    />
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    );
    return (
      <div className="group-brackets">
        <div>
          <div className="group-bracket-title">Upper bracket</div>
          {bracket(DOUBLE8_UPPER_ROUNDS)}
        </div>
        <div>
          <div className="group-bracket-title">Lower bracket</div>
          {bracket(DOUBLE8_LOWER_ROUNDS)}
        </div>
      </div>
    );
  };
  // Request fields describing the picked bracket. The backend computes the
  // bracket deterministically from `round_winners` (any field size) or, for
  // double elimination, from the 14 `match_winners` in play order; the bounty
  // variant is still looked up among stored 8-team outcomes, so send the legacy
  // qf/sf/final fields for it too.
  const completedBracketPayloadFields = () => {
    if (isDouble) {
      return {
        match_winners: DOUBLE8_MATCHES.map(([key]) => Number(completedDouble[key] || 0)),
        third_place_winner: 0,
      };
    }
    const rounds = (completedBracket.rounds || []).map((r) => r.map((id) => Number(id)));
    const fields = {
      round_winners: rounds,
      third_place_winner: !isBounty && hasThirdPlaceDecider ? Number(completedBracket.third) : 0,
    };
    if (isBounty) {
      fields.qf_winners = rounds[0] || [];
      fields.sf_winners = rounds[1] || [];
      fields.final_winner = (rounds[2] || [])[0] || 0;
    }
    return fields;
  };
  const runCompletedBracket = async () => {
    setBusy(true);
    setCompletedBracketMessage("");
    setCompletedBracketResult(null);
    setCompletedBracketUpdatedAt("");
    setProcessedCombos(0);
    setTotalCombos(0);
    setCompletedEtaSeconds(null);
    try {
      if (PUBLIC_BUILD) return;
      const start = await api.post("/playoff/best-team/bracket-from-latest/start", {
        ...completedBracketPayloadFields(),
        include_player_ids: Array.from(effectiveAppliedFilters.include),
        exclude_player_ids: Array.from(effectiveAppliedFilters.exclude),
        variant,
      });
      if (start?.detail || start?.error) {
        setCompletedBracketMessage(String(start.detail || start.error));
        return;
      }
      const jobId = start?.job_id;
      if (!jobId) {
        setCompletedBracketMessage("Failed to start completed bracket job.");
        return;
      }
      if (start?.reused) {
        setCompletedBracketMessage("A completed bracket optimizer is already running. Reusing that job.");
      }
      let data = null;
      let done = false;
      const startedAtMs = Date.now();
      while (!done) {
        const status = await api.get(`/playoff/best-team/bracket-from-latest/job/${jobId}`);
        if (status?.detail || status?.error) {
          setCompletedBracketMessage(String(status.detail || status.error));
          return;
        }
        const processed = Number(status.processed_combinations || 0);
        const total = Number(status.total_combinations || 0);
        setProcessedCombos(processed);
        setTotalCombos(total);
        if (processed > 0 && total > processed) {
          const elapsedSec = Math.max(0.001, (Date.now() - startedAtMs) / 1000);
          const rate = processed / elapsedSec;
          if (rate > 0) setCompletedEtaSeconds((total - processed) / rate);
        } else if (total > 0 && processed >= total) {
          setCompletedEtaSeconds(0);
        }
        if (status.status === "failed") {
          setCompletedBracketMessage(status.error || "Completed bracket evaluation failed.");
          return;
        }
        if (status.status === "completed") {
          data = status.result || {};
          done = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      if (data?.detail || data?.error) {
        setCompletedBracketMessage(String(data.detail || data.error));
        return;
      }
      const outcomeCount = Number(data?.outcomes_count || 0);
      const playerValueCount = Array.isArray(data?.player_values) ? data.player_values.length : 0;
      if (outcomeCount <= 0 || playerValueCount === 0) {
        setCompletedBracketMessage(
          "Completed bracket result had no stored outcome/player values. Re-run Playoff Bracket, then try this bracket again."
        );
        return;
      }
      setCompletedBracketResult(data);
      setCompletedBracketUpdatedAt(new Date().toISOString());
    } catch (e) {
      setCompletedBracketMessage(e?.message || "Failed to evaluate completed bracket.");
    } finally {
      setBusy(false);
      setComboPhase("");
    }
  };
  const playoffBestModeLabel = {
    average: "Best Average Value",
    single_outcome: "Highest Single-Outcome Ceiling",
    most_outcomes: "Most Likely Winner",
  }[playoffBestMode] || "Best Average Value";
  const playoffTopSubtabs = [
    { key: "average", label: "Average Player Value" },
    { key: "single_outcome", label: "Best Single Outcome" },
    { key: "most_outcomes", label: "Most Likely Winner" },
    { key: "completed", label: "Completed Bracket" },
  ];
  const setPlayoffTopMode = (key) => {
    setPlayoffTopSubtab(key);
    if (key !== "completed") {
      setPlayoffBestMode(key);
      setTopMessage("");
    }
  };
  const playoffTeamMetric = (team) => {
    if (playoffBestMode === "single_outcome") return Number(team?.ceiling_points || 0);
    if (playoffBestMode === "most_outcomes") {
      // Percent chance this roster is the winning pick; older stored runs only
      // have the raw outcome-win count.
      const prob = team?.outcome_win_probability;
      if (prob !== undefined && prob !== null) return Number(prob) * 100;
      return Number(team?.outcome_wins || 0);
    }
    return Number(team?.average_ev ?? team?.total_ev ?? 0);
  };
  const playoffPlayerModeScore = (player) => Number(rosterPoints(player, playoffBestMode).total || 0);
  const playoffOutcomeCount = Number(results?.outcomes_count || (hasThirdPlaceDecider ? 256 : 128));
  const formatOutcomeWins = (value) => {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return "0";
    return Math.abs(n - Math.round(n)) < 1e-9 ? String(Math.round(n)) : n.toFixed(1);
  };
  // Roster card head: the mode's metric on the name line, its detail on a
  // second muted line (the head column is narrow, so no long single label).
  const playoffTeamMetricLabel = (team) => {
    if (playoffBestMode === "single_outcome") return `Ceiling ${Number(team?.ceiling_points || 0).toFixed(2)}`;
    if (playoffBestMode === "most_outcomes") return `Win chance ${(Number(team?.outcome_win_probability || 0) * 100).toFixed(1)}%`;
    return `EV ${Number(team?.total_ev || 0).toFixed(2)}`;
  };
  // Detail lines under the roster's metric (each on its own line).
  const playoffTeamMetricSub = (team) => {
    if (playoffBestMode === "single_outcome") {
      const pct = Number(team?.ceiling_probability || 0) * 100;
      const shown = pct >= 1 ? pct.toFixed(1) : pct >= 0.1 ? pct.toFixed(2) : pct.toFixed(3);
      return [`Bracket odds ${shown}%`];
    }
    if (playoffBestMode === "most_outcomes") {
      const cond = Number(team?.win_conditional_ev || 0);
      const lines = [`${formatOutcomeWins(team?.outcome_wins)} of ${playoffOutcomeCount.toLocaleString()} brackets`];
      if (cond > 0) lines.push(`${cond.toFixed(1)} pts when it wins`);
      return lines;
    }
    return [];
  };
  // Exact enumerations store an outcome as a result code (one bit per match in
  // play order, 0 = the first-listed team wins); rebuild its bracket from the
  // stored template: the double-elimination match list, or halving rounds.
  const decodeCodeOutcome = (code, template) => {
    const slots = (template?.team_slots || []).map(Number);
    const bitAt = (pos) => Math.floor(code / 2 ** pos) % 2;
    if (template?.kind === "double") {
      const winners = {};
      const losers = {};
      const byStage = {};
      const nM = DOUBLE8_MATCHES.length;
      DOUBLE8_MATCHES.forEach(([key, stage, fa, fb], m) => {
        const feed = (f) => (f[0] === "seed" ? slots[f[1]] : f[0] === "win" ? winners[f[1]] : losers[f[1]]);
        const a = feed(fa);
        const b = feed(fb);
        const bit = bitAt(nM - 1 - m);
        winners[key] = bit ? b : a;
        losers[key] = bit ? a : b;
        (byStage[stage] = byStage[stage] || []).push({ winner: winners[key], loser: losers[key], teams: [a, b] });
      });
      return byStage;
    }
    const nMatches = Math.max(0, slots.length - 1) + (template?.third_place ? 1 : 0);
    let alive = slots.slice();
    let m = 0;
    const byRound = {};
    const semiLosers = [];
    while (alive.length > 1) {
      const winners = [];
      const matches = [];
      for (let i = 0; i + 1 < alive.length; i += 2) {
        const a = alive[i];
        const b = alive[i + 1];
        const bit = bitAt(nMatches - 1 - m);
        m += 1;
        const w = bit ? b : a;
        const l = bit ? a : b;
        winners.push(w);
        matches.push({ winner: w, loser: l, teams: [a, b] });
        if (alive.length === 4) semiLosers.push(l);
      }
      byRound[roundNameForTeams(alive.length)] = matches;
      alive = winners;
    }
    if (template?.third_place && semiLosers.length === 2) {
      const bit = bitAt(nMatches - 1 - m);
      byRound.third_place = [{ winner: bit ? semiLosers[1] : semiLosers[0], loser: bit ? semiLosers[0] : semiLosers[1] }];
    }
    return byRound;
  };
  // Finishing position of every team in one decoded bracket, by bucket label.
  const outcomeFinishBuckets = (bracket) => {
    const out = {};
    const put = (id, label) => {
      if (id != null && out[id] === undefined) out[id] = label;
    };
    if (bracket.grand_final) {
      const gf = bracket.grand_final[0] || {};
      put(gf.winner, "Champion");
      put(gf.loser, "Final");
      const cf = (bracket.cons_final || [])[0];
      if (cf) put(cf.loser, "3rd");
      const lf = (bracket.lb_final || [])[0];
      if (lf) put(lf.loser, "4th");
      (bracket.lb_sf || []).forEach((m) => put(m.loser, "5th–6th"));
      (bracket.lb_r1 || []).forEach((m) => put(m.loser, "7th–8th"));
      return out;
    }
    const fin = (bracket.final || [])[0] || {};
    put(fin.winner, "Champion");
    put(fin.loser, "Final");
    const third = (bracket.third_place || [])[0];
    if (third) {
      put(third.winner, "3rd");
      put(third.loser, "4th");
    }
    (bracket.semis || []).forEach((m) => put(m.loser, "Semis"));
    (bracket.quarters || []).forEach((m) => put(m.loser, "Quarters"));
    (bracket.round_of_16 || []).forEach((m) => put(m.loser, "R16"));
    (bracket.round_of_32 || []).forEach((m) => put(m.loser, "R32"));
    return out;
  };
  const FINISH_ORDER = ["Champion", "Final", "3rd", "4th", "Semis", "5th–6th", "Quarters", "7th–8th", "R16", "R32"];
  // The pattern behind a roster's winning brackets: each team's finishing
  // position, weighted by bracket probability, across the outcomes it wins.
  const winningPatterns = (team) => {
    const idxs = Array.isArray(team?.winning_outcome_indexes) ? team.winning_outcome_indexes : [];
    const outcomes = results?.outcomes || [];
    const template = results?.bracket_template || null;
    if (idxs.length === 0 || outcomes.length === 0) return null;
    const mass = {};
    const labels = new Set();
    let total = 0;
    idxs.forEach((i) => {
      const o = outcomes[i];
      if (!o) return;
      const p = Number(o.probability || 0);
      const bracket = o.bracket || (o.code != null && template ? decodeCodeOutcome(Number(o.code), template) : null);
      if (!bracket) return;
      total += p;
      Object.entries(outcomeFinishBuckets(bracket)).forEach(([tid, label]) => {
        labels.add(label);
        const by = (mass[tid] = mass[tid] || {});
        by[label] = (by[label] || 0) + p;
      });
    });
    if (total <= 0) return null;
    const columns = FINISH_ORDER.filter((l) => labels.has(l));
    const rows = Object.entries(mass).map(([tid, by]) => ({
      team_id: Number(tid),
      shares: columns.map((c) => (by[c] || 0) / total),
    }));
    rows.sort((a, b) => {
      for (let k = 0; k < columns.length; k += 1) {
        const d = b.shares[k] - a.shares[k];
        if (Math.abs(d) > 1e-9) return d;
      }
      return 0;
    });
    return { columns, rows, count: idxs.length, total };
  };

  // Double elimination: a team's odds as an aligned grid — round headers
  // across, one row for the upper bracket and one for the lower, then the
  // grand final and the title spanning both rows.
  const renderDoubleReach = (teamStage) => {
    const pct = (v) => (Number(v || 0) >= 0.0005 ? `${Math.round(Number(v) * 100)}%` : "–");
    const r = teamStage.reach || {};
    const cell = (key, value, row, col, bye = false, extra = "") => (
      <div className={`reach-v${bye ? " bye" : ""}${extra ? ` ${extra}` : ""}`} key={key} style={{ gridRow: row, gridColumn: col }} title={STAGE_FULL[key] || ""}>
        {pct(value)}
      </div>
    );
    const heads = ["R1", "R2", "R3", "R4", "R5"];
    return (
      <div className="reach-grid">
        {heads.map((h, i) => (
          <span className="reach-h" key={h} style={{ gridRow: 1, gridColumn: i + 2 }}>
            {h}
          </span>
        ))}
        <span className="reach-h" style={{ gridRow: 1, gridColumn: 7 }}>
          Final
        </span>
        <span className="reach-h" style={{ gridRow: 1, gridColumn: 8 }}>
          Title
        </span>
        <span className="reach-h side" style={{ gridRow: 2, gridColumn: 1 }}>
          Upper
        </span>
        <span className="reach-h side" style={{ gridRow: 3, gridColumn: 1 }}>
          Lower
        </span>
        {cell("ub_r1", r.ub_r1, 2, 2)}
        {cell("ub_sf", r.ub_sf, 2, 3)}
        {cell("bye_r3", r.bye_r3, 2, 4, true)}
        {cell("ub_final", r.ub_final, 2, 5)}
        {cell("bye_r5", r.bye_r5, 2, 6, true)}
        {cell("lb_r1", r.lb_r1, 3, 3)}
        {cell("lb_sf", r.lb_sf, 3, 4)}
        {cell("lb_final", r.lb_final, 3, 5)}
        {cell("cons_final", r.cons_final, 3, 6)}
        {cell("grand_final", r.grand_final, "2 / span 2", 7, false, "final")}
        {cell("title", teamStage.champion, "2 / span 2", 8, false, "final title")}
      </div>
    );
  };

  const loadEventsForPlayoff = async (retriesLeft = 3) => {
    // A busy backend (giant combo-blob parses hold the GIL) can stall this
    // request; retry instead of leaving the event dropdown blank, which reads
    // as the active event having been unset.
    let data = null;
    try {
      data = await api.get("/events/");
    } catch (e) {
      if (retriesLeft > 0) setTimeout(() => loadEventsForPlayoff(retriesLeft - 1), 5000);
      return;
    }
    if (data?.detail) return;
    const allEvents = Array.isArray(data.events) ? data.events : [];
    setEvents(allEvents);
    const active = data.active_event_id;
    setSelectedEventId(active == null ? "" : String(active));
  };

  const loadEventTeams = async (eventId) => {
    if (!eventId) {
      setEventTeamNames(new Set());
      return;
    }
    const data = await api.get(`/events/${eventId}`);
    if (data?.detail) return;
    setEventTeamNames(new Set((data.teams || []).map((t) => normalizeTeamName(t.team_name))));
  };

  const loadLatestPlayoff = async () => {
    const data = await api.get(`/playoff/latest?variant=${variant}`, 120000);
    if (!data?.exists) {
      setLatestPayload(null);
      setResults(null);
      setUpdatedAt("");
      setStoredSize(0);
      return;
    }
    const payload = data.payload || {};
    setLatestPayload(payload);
    setSlots((payload.team_slots || []).map((x) => String(x)));
    const loadedSize = (payload.team_slots || []).length;
    const loadedKind = payload.bracket_kind === "double" ? "double" : "single";
    setBracketSize([6, 8, 16].includes(loadedSize) ? loadedSize : 8);
    setBracketKind(loadedKind);
    setStoredSize(loadedSize);
    setStoredKind(loadedKind);
    setMcSims(Number(payload.mc_sims) || 5000);
    setHasThirdPlaceDecider(Boolean(payload.has_third_place_decider));
    if (isBounty) {
      const savedPairs = payload.qf_pairs || [];
      if (savedPairs.length === 4) {
        setDraftPicks(savedPairs.slice(0, 3).map((pair) => String(pair?.[1] || "")));
      }
      const restored = {};
      Object.entries(payload.sf_picks || {}).forEach(([key, pairs]) => {
        const pick = pairs?.[0]?.[1];
        if (pick) restored[key] = String(pick);
      });
      setSfPicks(restored);
    }
    setResults(data.results || null);
    if (loadedKind === "double") {
      hydrateCompletedDoubleFromBracket(data.results?.bracket);
    } else if (loadedSize !== 6) {
      // The completed-bracket picker doesn't model the 6-team byes shape.
      hydrateCompletedBracketFromBracket(data.results?.bracket, loadedSize || bracketSize);
    }
    setUpdatedAt(data.updated_at ? new Date(Number(data.updated_at) * 1000).toISOString() : "");
  };

  const loadLatestCompletedBracket = async () => {
    const data = await api.get(`/playoff/best-team/bracket-from-latest/latest?variant=${variant}`, 120000);
    if (!data?.exists) return;
    const payload = data.payload || {};
    // Prefer the general round_winners; fall back to the legacy 8-team fields.
    let rounds;
    if (Array.isArray(payload.round_winners) && payload.round_winners.length) {
      rounds = payload.round_winners.map((r) => (r || []).map((id) => String(id || "")));
    } else {
      const savedQf = (payload.qf_winners || []).slice(0, 4).map((id) => String(id || ""));
      const savedSf = (payload.sf_winners || []).slice(0, 2).map((id) => String(id || ""));
      rounds = [
        [...savedQf, "", "", "", ""].slice(0, 4),
        [...savedSf, ""].slice(0, 2),
        [payload.final_winner ? String(payload.final_winner) : ""],
      ];
    }
    setCompletedBracket({ rounds, third: payload.third_place_winner ? String(payload.third_place_winner) : "" });
    setCompletedBracketUpdatedAt(data.updated_at ? new Date(Number(data.updated_at) * 1000).toISOString() : "");
  };

  const loadLatestSharedCombinations = async () => {
    const data = await api.get(`/playoff/best-team/from-latest/latest?variant=${variant}`, 120000);
    if (!data?.exists) return;
    setBaseTeams([]);
    setSharedComboCount(Number(data.total_teams || 0));
    setSharedCombosUpdatedAt(data.updated_at ? new Date(Number(data.updated_at) * 1000).toISOString() : "");
  };

  useEffect(() => {
    loadEventsForPlayoff();
    loadLatestPlayoff();
    loadLatestCompletedBracket();
    loadLatestSharedCombinations();
  }, []);

  useEffect(() => {
    loadEventTeams(selectedEventId);
  }, [selectedEventId]);

  useEffect(() => {
    // While the event's team list is still loading, filteredTeams is empty —
    // wiping then would erase slots freshly hydrated from the saved bracket.
    if (filteredTeams.length === 0) return;
    const allowed = new Set(filteredTeams.map((t) => t.team_id));
    setSlots((prev) => prev.map((v) => (allowed.has(Number(v)) ? v : "")));
  }, [filteredTeams]);

  const run = async () => {
    if (PUBLIC_BUILD) return;
    const ids = slots.map((s) => Number(s));
    if (ids.some((id) => !id)) return;
    if (isBounty && !draftedQfPairs) return;
    const sims = Math.max(500, Math.min(200000, Number(mcSims) || 5000));
    setBusy(true);
    setRunMessage("");
    setProcessedSims(0);
    setTotalSims(isDouble ? 16384 : slots.length >= 16 ? sims : !isBounty && hasThirdPlaceDecider ? 256 : 128);
    setEtaSeconds(null);

    const pollPlayoffJob = async (jobId, startedAtMs) => {
      if (!jobId) return;
      if (playoffPollingRef.current) return;
      playoffPollingRef.current = true;
      try {
        let done = false;
        while (!done) {
          const status = await api.get(`/playoff/job/${jobId}`);
          if (status?.detail) {
            setRunMessage(String(status.detail));
            return;
          }
          const processed = Number(status.processed_sims || 0);
          const total = Number(status.total_sims || 0);
          setProcessedSims(processed);
          setTotalSims(total);
          if (processed > 0 && total > processed) {
            const elapsedSec = Math.max(0.001, (Date.now() - startedAtMs) / 1000);
            const rate = processed / elapsedSec;
            if (rate > 0) setEtaSeconds((total - processed) / rate);
          } else if (total > 0 && processed >= total) {
            setEtaSeconds(0);
          }

          if (status.status === "failed") {
            setRunMessage(status.error || "Playoff run failed.");
            return;
          }
          if (status.status === "completed") {
            const data = status.result || null;
            setResults(data);
            setLatestPayload({ team_slots: ids, has_third_place_decider: !isDouble && hasThirdPlaceDecider, bracket_kind: bracketKind });
            setStoredKind(bracketKind);
            setUpdatedAt(new Date().toISOString());
            setTopTeams(null);
            setAllTeams(null);
            setBaseTeams(null);
            setTopMessage("");
            done = true;
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 200));
        }
      } finally {
        playoffPollingRef.current = false;
      }
    };

    try {
      const start = await api.post("/playoff/start", {
        team_slots: ids,
        bracket_kind: isBounty ? "single" : bracketKind,
        has_third_place_decider: isBounty || isDouble ? false : hasThirdPlaceDecider,
        mc_sims: sims,
        ...(isBounty ? { variant: "bounty", qf_pairs: draftedQfPairs, sf_picks: bountySfPicksPayload() } : {}),
      });
      if (start?.detail) {
        setRunMessage(String(start.detail));
        return;
      }
      const jobId = start?.job_id;
      if (!jobId) {
        setRunMessage("Failed to start playoff job.");
        return;
      }
      await pollPlayoffJob(jobId, Date.now());
    } finally {
      setBusy(false);
    }
  };

  // Self-running valuations: recompute automatically once the bracket is
  // seeded, and again whenever the seeds or the third-place rule change.
  // Stored valuations that already cover the exact same bracket are reused
  // as-is — no recompute on every visit to the tab.
  const lastAutoRunRef = useRef("");
  useEffect(() => {
    if (isBounty || busy) return;
    if (storedSize === null) return; // stored-sim hydration pending
    const ids = slots.map((s) => Number(s));
    if (ids.length === 0 || ids.some((id) => !id)) return;
    const sig = ids.join("-") + (!isDouble && hasThirdPlaceDecider ? ":3rd" : "") + (isDouble ? ":double" : "");
    if (lastAutoRunRef.current === sig) return;
    const storedIds = (latestPayload?.team_slots || []).map((x) => Number(x));
    const storedSig =
      storedIds.length > 0 && storedIds.every(Boolean)
        ? storedIds.join("-") +
          (latestPayload?.has_third_place_decider ? ":3rd" : "") +
          (latestPayload?.bracket_kind === "double" ? ":double" : "")
        : null;
    // stage_stats gate: stored results from before the round-source feature
    // are recomputed once so the popup has its data.
    if (results?.stage_stats && storedSig === sig) {
      lastAutoRunRef.current = sig;
      return;
    }
    lastAutoRunRef.current = sig;
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slots, hasThirdPlaceDecider, bracketKind, busy, storedSize, isBounty, results, latestPayload]);


  const resetStoredPlayoff = async () => {
    if (PUBLIC_BUILD) return;
    await api.delete(`/playoff/latest?variant=${variant}`);
    setLatestPayload(null);
    setResults(null);
    setUpdatedAt("");
    setTopTeams(null);
    setAllTeams(null);
    setBaseTeams(null);
    setSharedCombosUpdatedAt("");
    setSharedComboCount(0);
    setCompletedBracketResult(null);
    setCompletedBracketMessage("");
    setCompletedBracketUpdatedAt("");
    setShowCompletedBracketGraph(false);
    setCompletedPlayerBreakdown(null);
    setFilteredCount(0);
    setProcessedCombos(0);
    setTotalCombos(0);
    setTopEtaSeconds(null);
    setComboPhase("");
    setTopMessage("");
  };

  const modalMatches = useMemo(() => {
    const q = filterSearch.trim().toLowerCase();
    if (!q) return [];
    return players
      .filter((p) => String(p.name || "").toLowerCase().includes(q))
      .slice(0, 25);
  }, [players, filterSearch]);

  // Content-based signature so background team refreshes (new array/Set identities
  // with identical contents) do not re-fire the expensive combination query.
  const effectiveFiltersSignature = useMemo(
    () =>
      JSON.stringify({
        include: Array.from(effectiveAppliedFilters.include).sort((a, b) => a - b),
        exclude: Array.from(effectiveAppliedFilters.exclude).sort((a, b) => a - b),
      }),
    [effectiveAppliedFilters]
  );

  // Only query rosters while the Top 5 tab is showing (the query used to run
  // on every Tournament open, before the user had asked for it).
  useEffect(() => {
    if (!baseTeams || playoffTab !== "top5") return;
    querySharedCombinations(page);
  }, [baseTeams, effectiveFiltersSignature, comboSearch, sortKey, playoffBestMode, playoffTab]);

  useEffect(() => {
    if (!baseTeams || playoffTab !== "top5") return;
    if (playoffTopSubtab === "completed" && !completedComplete) {
      setCompletedBracketResult(null);
      return;
    }
    querySharedCombinations(0);
  }, [playoffTopSubtab, completedBracket, completedDouble, completedComplete, hasThirdPlaceDecider, playoffTab]);

  const querySharedCombinations = async (nextPage = 0) => {
    if (!baseTeams) return;
    // Overlapping queries resolve in arbitrary order; only the newest one may
    // apply its response, otherwise a slow stale result overwrites fresh data.
    const seq = ++comboQuerySeqRef.current;
    try {
      const endpoint = playoffTopSubtab === "completed"
        ? "/playoff/best-team/from-latest/completed-query"
        : "/playoff/best-team/from-latest/query";
      const body = {
        mode: playoffBestMode,
        include_player_ids: Array.from(effectiveAppliedFilters.include),
        exclude_player_ids: Array.from(effectiveAppliedFilters.exclude),
        search: comboSearch,
        sort: sortKey,
        page: nextPage,
        page_size: COMBO_PAGE_SIZE,
        variant,
      };
      if (playoffTopSubtab === "completed") {
        if (!completedComplete) return;
        Object.assign(body, completedBracketPayloadFields());
      }
      const data = await api.post(endpoint, body, 120000);
      if (seq !== comboQuerySeqRef.current) return;
      setTopTeams(data.top_teams || []);
      setAllTeams(data.page_teams || []);
      setFilteredCount(Number(data.filtered_count || 0));
      setSharedComboCount(data.total_teams != null ? Number(data.total_teams) : sharedComboCount);
      setCombosApproximate(Boolean(data.approximate));
      setPage(Number(data.page ?? nextPage ?? 0));
      if (playoffTopSubtab === "completed") {
        setCompletedBracketResult(data);
      }
    } catch (e) {
      if (seq !== comboQuerySeqRef.current) return;
      setTopMessage(e?.message || "Failed to load saved combinations.");
    }
  };

  const applyTop5ViewFilters = (teamsIn) => {
    const base = Array.isArray(teamsIn) ? teamsIn : [];
    let filtered = applyFilters(base, effectiveAppliedFilters.include, effectiveAppliedFilters.exclude);
    const q = comboSearch.trim().toLowerCase();
    if (q) {
      filtered = filtered.filter((team) =>
        (team.players || []).some((p) => String(p.name || "").toLowerCase().includes(q))
      );
    }
    const sorted = [...filtered].sort((a, b) => {
      if (sortKey === "cost_asc") return Number(a.cost || 0) - Number(b.cost || 0);
      if (sortKey === "cost_desc") return Number(b.cost || 0) - Number(a.cost || 0);
      if (sortKey === "cpp_asc") return playoffTeamMetric(a) / (Number(a.cost || 1)) - playoffTeamMetric(b) / (Number(b.cost || 1));
      if (sortKey === "cpp_desc") return playoffTeamMetric(b) / (Number(b.cost || 1)) - playoffTeamMetric(a) / (Number(a.cost || 1));
      if (sortKey === "ev_asc") return playoffTeamMetric(a) - playoffTeamMetric(b);
      return playoffTeamMetric(b) - playoffTeamMetric(a);
    });
    setAllTeams(sorted);
    setTopTeams(sorted.slice(0, 10));
    setFilteredCount(sorted.length);
    setPage(0);
  };
  const playoffPlayerValueData = useMemo(
    () => buildPlayerValueRowsFromSimulation(results, players),
    [results, players]
  );
  const activeCompletedBracketResult = completedBracketResult;
  const completedBracketValueData = useMemo(() => {
    const rows = (activeCompletedBracketResult?.player_values || [])
      .map((row) => ({
        player_id: Number(row.player_id),
        name: row.name || `Player ${row.player_id}`,
        team_id: Number(row.team_id || 0),
        price: Number(row.price || 0),
        points: Number(row.points || 0),
        rating: Number(row.rating || 0),
        win: Number(row.win || 0),
        role: Number(row.role || 0),
        booster: Number(row.booster || 0),
      }))
      .filter((row) => Number.isFinite(row.price) && Number.isFinite(row.points))
      .sort((a, b) => b.points - a.points);
    if (rows.length === 0) return { rows: [], slope: 0, intercept: 0 };
    const xMean = rows.reduce((sum, row) => sum + row.price, 0) / rows.length;
    const yMean = rows.reduce((sum, row) => sum + row.points, 0) / rows.length;
    const num = rows.reduce((sum, row) => sum + (row.price - xMean) * (row.points - yMean), 0);
    const den = rows.reduce((sum, row) => sum + (row.price - xMean) ** 2, 0);
    const slope = den > 0 ? num / den : 0;
    const intercept = yMean - slope * xMean;
    return {
      rows: rows.map((row) => {
        const onLine = intercept + slope * row.price;
        return { ...row, on_line: onLine, distance: row.points - onLine };
      }),
      slope,
      intercept,
    };
  }, [activeCompletedBracketResult?.player_values]);
  const completedBreakdownValue = (row, key) => {
    if (key === "total") {
      const total = ["rating", "win", "role", "booster"].reduce((sum, part) => sum + completedBreakdownValue(row, part), 0);
      if (row?.components_available === false && Number.isFinite(Number(row?.points))) return Number(row.points);
      return total;
    }
    const candidates = {
      rating: [row?.rating, row?.rating_ev],
      win: [row?.win, row?.win_ev],
      role: [row?.role, row?.role_ev],
      booster: [row?.booster, row?.booster_ev],
    }[key] || [];
    const value = candidates.map((v) => Number(v)).find((v) => Number.isFinite(v));
    return Number.isFinite(value) ? value : 0;
  };

  const formatEta = (seconds) => {
    if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "-";
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}h ${m}m ${r}s`;
    if (m > 0) return `${m}m ${r}s`;
    return `${r}s`;
  };

  return (
    <Section title={isBounty ? "Bounty Playoffs (BO3)" : "Playoff Bracket (BO3)"}>
      <div className="stack">
        <div className="tab-bar small">
          <button className={playoffTab === "stage" ? "tab active" : "tab"} onClick={() => setPlayoffTab("stage")}>
            Bracket Stage
          </button>
          <button className={playoffTab === "value" ? "tab active" : "tab"} onClick={() => setPlayoffTab("value")}>
            Player Value
          </button>
          <button className={playoffTab === "top5" ? "tab active" : "tab"} onClick={() => setPlayoffTab("top5")}>
            Top 5 Teams
          </button>
        </div>

        {playoffTab === "stage" && isBounty && (
          <>
            <div className="card sub">
              <h3>Seeding</h3>
              <p className="muted">
                Seeds 1-4 are the top half and get drafted; seeds 5-8 are the bottom half and pick their quarter-final
                opponent in seed order (highest-rated bottom-half team drafts first).
              </p>
              <div className="grid three">
                {slots.map((slotValue, idx) => (
                  <div className="field" key={`bounty-seed-${idx}`}>
                    <span>
                      Seed {idx + 1} {idx < 4 ? "(top half)" : "(bottom half)"}
                    </span>
                    <select value={slotValue} onChange={(e) => setSlot(idx, e.target.value)} disabled={busy}>
                      {playoffTeamOptions.map((option) => (
                        <option key={option.value || "empty"} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
            </div>
            <div className="card sub">
              <h3>Quarter-final Draft</h3>
              {slots.slice(4, 8).some((s) => !s) || slots.slice(0, 4).some((s) => !s) ? (
                <p className="muted">Fill in all 8 seeds above to run the draft.</p>
              ) : (
                <>
                  {[0, 1, 2].map((pickIdx) => {
                    const drafter = Number(slots[4 + pickIdx]);
                    const takenElsewhere = new Set(
                      draftPicks.filter((p, i) => i !== pickIdx && p).map((p) => String(p))
                    );
                    const options = slots
                      .slice(0, 4)
                      .map((s) => Number(s))
                      .filter((id) => id && !takenElsewhere.has(String(id)));
                    return (
                      <div className="field" key={`bounty-pick-${pickIdx}`}>
                        <span>
                          Pick {pickIdx + 1}: {teamLookup[drafter] || `Seed ${5 + pickIdx}`} plays
                        </span>
                        <select
                          value={draftPicks[pickIdx]}
                          onChange={(e) =>
                            setDraftPicks((prev) => {
                              const next = [...prev];
                              next[pickIdx] = e.target.value;
                              return next;
                            })
                          }
                          disabled={busy}
                        >
                          <option value="">Select opponent</option>
                          {options.map((id) => (
                            <option key={id} value={String(id)}>
                              {teamLookup[id] || id}
                            </option>
                          ))}
                        </select>
                      </div>
                    );
                  })}
                  <p className="muted">
                    {draftedQfPairs
                      ? `${teamLookup[draftedQfPairs[3][0]] || "Seed 8"} gets the remaining team: ${
                          teamLookup[draftedQfPairs[3][1]] || draftedQfPairs[3][1]
                        }`
                      : "Seed 8 automatically plays the remaining top-half team."}
                  </p>
                </>
              )}
              {draftedQfPairs && (
                <div className="playoff-bracket-shell">
                  <div className="playoff-bracket-column qf">
                    <h3>Drafted Quarter-finals</h3>
                    {draftedQfPairs.map((pair, idx) => (
                      <BracketMatchCard
                        key={`bounty-qf-${idx}`}
                        title={`QF ${idx + 1}`}
                        rows={
                          <>
                            {pair.map((teamId, rowIdx) => (
                              <div className="playoff-team-row" key={`bounty-qf-${idx}-${rowIdx}`}>
                                <TeamBadge teamId={teamId} />
                                <span>{teamLookup[teamId] || teamId}</span>
                              </div>
                            ))}
                          </>
                        }
                      />
                    ))}
                  </div>
                </div>
              )}
            </div>
            {draftedQfPairs && (
              <div className="card sub">
                <h3>Semi-final Drafts (per scenario)</h3>
                <p className="muted">
                  After the quarter-finals the two lowest-seeded survivors draft again. For each possible set of QF
                  winners, choose who the first drafter picks; the other pairing is forced. Unset scenarios default to
                  the drafter picking the lower-seeded top survivor.
                </p>
                <table>
                  <thead>
                    <tr>
                      <th>QF Winners</th>
                      <th>First Drafter</th>
                      <th>Picks</th>
                      <th>Other Semi-final</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bountySfScenarios.map((sc) => {
                      const pairs = bountySfPairsFor(sc.winners);
                      return (
                        <tr key={sc.key}>
                          <td>{sc.winners.map((id) => teamLookup[id] || id).join(", ")}</td>
                          <td>{teamLookup[sc.drafter] || sc.drafter}</td>
                          <td>
                            <select
                              value={String(Number(sfPicks[sc.key]) && sc.options.includes(Number(sfPicks[sc.key])) ? sfPicks[sc.key] : pairs[0][1])}
                              onChange={(e) => setSfPicks((prev) => ({ ...prev, [sc.key]: e.target.value }))}
                              disabled={busy}
                            >
                              {sc.options.map((id) => (
                                <option key={id} value={String(id)}>
                                  {teamLookup[id] || id}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td>
                            {(teamLookup[pairs[1][0]] || pairs[1][0]) + " vs " + (teamLookup[pairs[1][1]] || pairs[1][1])}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            <div className="actions">
              {!PUBLIC_BUILD && (<button className="primary" onClick={run} disabled={busy || !draftedQfPairs}>
                {busy ? "Running..." : "Run Bounty Playoffs And Store Valuations"}
              </button>)}
              {!PUBLIC_BUILD && (<button className="danger" onClick={resetStoredPlayoff} disabled={busy || !results}>
                Reset Stored Valuations
              </button>)}
              {updatedAt && <p className="muted">Stored: {new Date(updatedAt).toLocaleString()}</p>}
            </div>
          </>
        )}
        {playoffTab === "stage" && !isBounty && (
          <>
            {playoffAutofillMessage && <p className="muted">{playoffAutofillMessage}</p>}
            {renderSeedingBracket()}
            {routingError && <p className="muted">{routingError}</p>}
            {!PUBLIC_BUILD && bracketSize !== 6 && !isDouble && (
              <div className="actions">
                <label className="checkbox-inline">
                  <input
                    type="checkbox"
                    checked={hasThirdPlaceDecider}
                    onChange={(e) => setHasThirdPlaceDecider(e.target.checked)}
                    disabled={busy}
                  />
                  <span>Third-place decider match</span>
                </label>
              </div>
            )}
          </>
        )}

        {playoffTab === "top5" && (
          <>
            {!results && (
              <div className="card sub">
                <p className="muted">{PUBLIC_BUILD ? NOT_PUBLISHED : "Run Playoff Bracket in the Bracket Stage tab first."}</p>
              </div>
            )}
            {results && (
              <>
                {busy && (
                  <div className="card sub">
                    {comboPhase === "saving" ? (
                      <>
                        <p className="muted">
                          Saving combinations: {processedCombos.toLocaleString()} / {totalCombos.toLocaleString()}
                        </p>
                        <p className="muted">This stores the generated roster universe for the tabs below.</p>
                      </>
                    ) : totalCombos > 0 ? (
                      <>
                        <p className="muted">
                          Processing combinations: {processedCombos.toLocaleString()} / {totalCombos.toLocaleString()}
                        </p>
                        <p className="muted">ETA: {formatEta(topEtaSeconds)}</p>
                      </>
                    ) : (
                      <p className="muted">Starting combination generator...</p>
                    )}
                    <div className="progress">
                      <div
                        className={`progress-bar ${totalCombos > 0 ? "determinate" : ""}`}
                        style={totalCombos > 0 ? { width: `${Math.min(100, (processedCombos / totalCombos) * 100)}%` } : undefined}
                      />
                    </div>
                  </div>
                )}
                <div className="top5-mode-row">
                  <div className="seg">
                    {playoffTopSubtabs
                      .filter((t) => t.key !== "completed")
                      .map((t) => (
                        <button
                          key={t.key}
                          className={`seg-btn${playoffTopSubtab !== "completed" && playoffBestMode === t.key ? " on" : ""}`}
                          onClick={() => setPlayoffTopMode(t.key)}
                        >
                          {t.label}
                        </button>
                      ))}
                  </div>
                  <button
                    className={`seg-standalone${playoffTopSubtab === "completed" ? " on" : ""}`}
                    onClick={() => setPlayoffTopMode(playoffTopSubtab === "completed" ? playoffBestMode : "completed")}
                  >
                    Completed Bracket
                  </button>
                </div>
                <div className="card sub">
                  <h3>{playoffTopSubtab === "completed" ? "Completed Bracket Filters" : `Top Teams - ${playoffBestModeLabel}`}</h3>
                  <div className="top5-filters">
                    <div className="field">
                      <span>Bracket Teams</span>
                      <div className="filter-team-grid">
                        {playoffTeamsForFilters.length === 0 && <span className="muted">Select playoff slots first.</span>}
                        {playoffTeamsForFilters.map((t) => {
                          const tid = Number(t.team_id);
                          const pids = teamPlayerIds(tid);
                          const isExcluded = pids.length > 0 && pids.every((pid) => excludeSet.has(pid));
                          return (
                            <div key={`filter-team-${tid}`} className={`filter-team-card${isExcluded ? " exc" : ""}`}>
                              <TeamLogo hltvTeamId={t.hltv_team_id} name={t.name} size={26} />
                              <span className="filter-team-name">{t.name}</span>
                              <div className="filter-team-toggles">
                                <button
                                  className={`filter-toggle out${isExcluded ? " on" : ""}`}
                                  onClick={() => {
                                    // Toggle the team's five players in the shared
                                    // exclude list (they show as chips like any other).
                                    const update = (setter) =>
                                      setter((prev) => {
                                        const next = new Set(prev);
                                        pids.forEach((pid) => (isExcluded ? next.delete(pid) : next.add(pid)));
                                        return next;
                                      });
                                    update(setExcludeSet);
                                    update(setAppliedExcludeSet);
                                    if (!isExcluded) {
                                      const dropInc = (setter) =>
                                        setter((prev) => {
                                          const next = new Set(prev);
                                          pids.forEach((pid) => next.delete(pid));
                                          return next;
                                        });
                                      dropInc(setIncludeSet);
                                      dropInc(setAppliedIncludeSet);
                                    }
                                  }}
                                >
                                  {isExcluded ? "Excluded" : "Exclude"}
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                    {(includeSet.size > 0 || excludeSet.size > 0) && (
                      <div className="grid two">
                        {includeSet.size > 0 && (
                          <div className="filter-group in">
                            <div className="filter-group-title">Included Players</div>
                            <div className="chips">
                              {Array.from(includeSet).sort((a, b) => a - b).map((pid) => (
                                <button
                                  key={`p-inc-${pid}`}
                                  className="filter-chip in"
                                  title="Remove filter"
                                  onClick={() => {
                                    const drop = (setter) =>
                                      setter((prev) => {
                                        const next = new Set(prev);
                                        next.delete(pid);
                                        return next;
                                      });
                                    drop(setIncludeSet);
                                    drop(setAppliedIncludeSet);
                                  }}
                                >
                                  <PlayerPhoto playerId={pid} name={playerNameById[pid]} size={20} />
                                  {playerNameById[pid] || `Player ${pid}`} <span className="x">&times;</span>
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                        {excludeSet.size > 0 && (
                          <div className="filter-group out">
                            <div className="filter-group-title">Excluded Players</div>
                            <div className="chips">
                              {Array.from(excludeSet).sort((a, b) => a - b).map((pid) => (
                                <button
                                  key={`p-exc-${pid}`}
                                  className="filter-chip out"
                                  title="Remove filter"
                                  onClick={() => {
                                    const drop = (setter) =>
                                      setter((prev) => {
                                        const next = new Set(prev);
                                        next.delete(pid);
                                        return next;
                                      });
                                    drop(setExcludeSet);
                                    drop(setAppliedExcludeSet);
                                  }}
                                >
                                  <PlayerPhoto playerId={pid} name={playerNameById[pid]} size={20} />
                                  {playerNameById[pid] || `Player ${pid}`} <span className="x">&times;</span>
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                    <div className="top5-controls-row">
                      <div className="top5-search">
                        <SuggestInput
                          label="Search / Filter Players"
                          value={comboSearch}
                          onChange={setComboSearch}
                          placeholder="Player name"
                          suggestions={comboSearchNames}
                          actions={[
                            { key: "in", label: "Include", onPick: (name) => addPlayerFilterByName(name, "include") },
                            { key: "out", label: "Exclude", onPick: (name) => addPlayerFilterByName(name, "exclude") },
                          ]}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </>
            )}
          </>
        )}

        {playoffTab === "top5" && playoffTopSubtab === "completed" && (
          <div className="stack">
            {!results && (
              <div className="card sub">
                <p className="muted">{PUBLIC_BUILD ? NOT_PUBLISHED : "Run Playoff Bracket in the Bracket Stage tab first."}</p>
              </div>
            )}
            {results && (
              <>
                {isDouble ? (
                  renderDoubleBracket("pick")
                ) : slots.length === 6 ? (
                  <p className="muted">
                    Completed-bracket scoring isn't supported for the 6-team byes bracket yet.
                  </p>
                ) : (
                  renderCompletedPicker()
                )}
                <div className="actions">
                  {!baseTeams && (
                    <p className="muted">
                      Roster rankings for this bracket have not been published yet.
                    </p>
                  )}
                  {baseTeams && !completedComplete && <p className="muted">Complete the bracket to score the saved combinations.</p>}
                  {!PUBLIC_BUILD && baseTeams && completedComplete && (
                    <>
                      <button className="primary" onClick={runCompletedBracket} disabled={busy}>
                        {busy
                          ? `Computing Boosters... ${processedCombos.toLocaleString()} / ${totalCombos.toLocaleString()}`
                          : "Compute Exact Booster Ranking"}
                      </button>
                      <span className="muted">
                        The table below updates instantly but ranks without roster booster assignment. This recomputes
                        booster-exact totals for the picked bracket.
                      </span>
                    </>
                  )}
                </div>
                {busy && totalCombos > 0 && (
                  <div className="card sub">
                    <p className="muted">
                      Booster ranking: {processedCombos.toLocaleString()} / {totalCombos.toLocaleString()}
                      {totalCombos > processedCombos ? ` | ETA: ${formatBatchEta(completedEtaSeconds)}` : ""}
                    </p>
                    <div className="progress">
                      <div
                        className="progress-bar determinate"
                        style={{ width: `${Math.min(100, Math.max(0, (processedCombos / totalCombos) * 100))}%` }}
                      />
                    </div>
                  </div>
                )}
                {completedBracketMessage && (
                  <div className="card sub">
                    <p className="muted">{completedBracketMessage}</p>
                  </div>
                )}
                {activeCompletedBracketResult && (
                  <div className="stack">
                    <div className="card sub">
                    <h3>
                      Bracket probability {(Number(activeCompletedBracketResult.bracket_probability || 0) * 100).toFixed(2)}%
                    </h3>
                    <p className="muted">
                      Matched 1 of {Number(activeCompletedBracketResult.outcomes_count || 0).toLocaleString()} stored outcomes.
                    </p>
                    <div className="actions">
                      <button className="secondary" onClick={() => setShowCompletedBracketGraph((prev) => !prev)}>
                        {showCompletedBracketGraph ? "Hide Player Value Graph" : "Show Player Value Graph"}
                      </button>
                    </div>
                    </div>
                    {showCompletedBracketGraph && completedBracketValueData.rows.length > 0 && (
                      <PriceVsPointsPanel
                        title="Player Price vs Points (Completed Bracket)"
                        rows={completedBracketValueData.rows}
                        slope={completedBracketValueData.slope}
                        intercept={completedBracketValueData.intercept}
                        showTable={false}
                        onPointClick={setCompletedPlayerBreakdown}
                      />
                    )}
                    {(activeCompletedBracketResult.top_teams || [])[0] && (
                      <div className="card sub">
                        <h3>Best Team</h3>
                        {(() => {
                          const team = (activeCompletedBracketResult.top_teams || [])[0];
                          return (
                            <>
                              <h4>
                                Bracket score {Number(team.total_ev || 0).toFixed(2)} | Cost {team.cost}
                              </h4>
                              <table>
                                <thead>
                                  <tr>
                                    <th>Player</th>
                                    <th>Team</th>
                                    <th>Assigned Role</th>
                                    <th>Cost</th>
                                    <th>Bracket Score</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {(team.players || []).map((p) => (
                                    <tr key={p.player_id}>
                                      <td>
                                        <button
                                          className="inline-link-btn"
                                          onClick={() => setCompletedPlayerBreakdown(rosterAssignedBoosterPlayer(p, team.players || []))}
                                        >
                                          {p.name}
                                        </button>
                                      </td>
                                      <td>{teamLookup[p.team_id] || p.team_id}</td>
                                      <td>{roleLabel(p.role_name)}</td>
                                      <td>{p.price}</td>
                                      <td>{Number(rosterPoints(p, "completed").total || 0).toFixed(2)}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </>
                          );
                        })()}
                      </div>
                    )}
                    <div className="card sub">
                      <h3>Player Values</h3>
                      <table>
                        <thead>
                          <tr>
                            <th>Player</th>
                            <th>Team</th>
                            <th>Cost</th>
                            <th>Bracket Score</th>
                            <th>Value +/-</th>
                          </tr>
                        </thead>
                        <tbody>
                          {completedBracketValueData.rows.map((row) => (
                            <tr key={row.player_id}>
                              <td>
                                <button className="inline-link-btn" onClick={() => setCompletedPlayerBreakdown(row)}>
                                  {row.name}
                                </button>
                              </td>
                              <td>{teamLookup[row.team_id] || row.team_id}</td>
                              <td>{Number(row.price || 0).toLocaleString()}</td>
                              <td>{Number(row.points || 0).toFixed(2)}</td>
                              <td>{Number(row.distance || 0).toFixed(2)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="card sub">
                      <h3>Top Teams</h3>
                      {(activeCompletedBracketResult.top_teams || []).length === 0 && (
                        <p className="muted">No valid fantasy team matched the budget, per-team, and role constraints.</p>
                      )}
                      {(activeCompletedBracketResult.top_teams || []).map((team, idx) => (
                      <div key={`completed-team-${idx}`} className="card sub">
                        <h4>
                          #{idx + 1} Bracket score {Number(team.total_ev || 0).toFixed(2)} | Cost {team.cost}
                        </h4>
                        <table>
                          <thead>
                            <tr>
                              <th>Player</th>
                              <th>Team</th>
                              <th>Assigned Role</th>
                              <th>Cost</th>
                              <th>Bracket Score</th>
                            </tr>
                          </thead>
                          <tbody>
                            {(team.players || []).map((p) => (
                              <tr key={p.player_id}>
                                <td>
                                  <button
                                    className="inline-link-btn"
                                    onClick={() => setCompletedPlayerBreakdown(rosterAssignedBoosterPlayer(p, team.players || []))}
                                  >
                                    {p.name}
                                  </button>
                                </td>
                                <td>{teamLookup[p.team_id] || p.team_id}</td>
                                <td>{roleLabel(p.role_name)}</td>
                                <td>{p.price}</td>
                                <td>{Number(rosterPoints(p, "completed").total || 0).toFixed(2)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
      {playoffTab === "stage" && busy && (
        <div className="card sub">
          <p className="muted">
            Calculated outcomes: {processedSims.toLocaleString()} / {totalSims.toLocaleString() || (hasThirdPlaceDecider ? 256 : 128)}
          </p>
          <p className="muted">ETA: {formatEta(etaSeconds)}</p>
          <div className="progress">
            <div
              className="progress-bar determinate"
              style={{ width: `${totalSims > 0 ? Math.min(100, (processedSims / totalSims) * 100) : 0}%` }}
            />
          </div>
        </div>
      )}
      {playoffTab === "stage" && runMessage && (
        <div className="card sub">
          <p className="muted">{runMessage}</p>
        </div>
      )}
      {playoffTab === "stage" && results && (
        <div className="stack">
          <div className="card sub">
            <h3>Player Expected Values</h3>
          </div>
          <div className="event-team-rows">
            {Object.entries(results.teams).map(([tid, data]) => {
              const info = (teams || []).find((t) => Number(t.team_id) === Number(tid));
              const stageStats = results.stage_stats || null;
              const stageList = stageStats?.stages || [];
              const teamStage = stageStats?.teams?.[String(tid)] || null;
              const rows = Object.entries(data.players || {}).sort(
                (a, b) => Number(b[1].total_points ?? 0) - Number(a[1].total_points ?? 0)
              );
              return (
                <div className={`event-team-row${stageList.includes("bye_r3") ? " double-head" : ""}`} key={tid}>
                  <div className="event-team-head">
                    <TeamLogo hltvTeamId={info?.hltv_team_id} name={teamLookup[Number(tid)]} size={42} />
                    <div className="event-team-headtext">
                      <span className="event-team-name">{teamLookup[Number(tid)] || `Team ${tid}`}</span>
                      {teamStage && stageList.includes("bye_r3") && renderDoubleReach(teamStage)}
                      {teamStage && !stageList.includes("bye_r3") && (
                        <div className="event-team-reach">
                          {stageList
                            .filter((s) => Number(teamStage.reach?.[s] || 0) > 0.0005)
                            .map((s) => (
                              <div className="event-player-stat" key={s}>
                                <div className="event-player-mini">
                                  {Math.round(Number(teamStage.reach[s]) * 100)}%
                                </div>
                                <div className="event-player-stat-label">{STAGE_SHORT[s] || s}</div>
                              </div>
                            ))}
                          <div className="event-player-stat">
                            <div className="event-player-mini">
                              {Math.round(Number(teamStage.champion || 0) * 100)}%
                            </div>
                            <div className="event-player-stat-label">Title</div>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                  <div className="event-team-players">
                    {rows.map(([pid, comps]) => {
                      const total = Number(comps.total_points ?? 0);
                      const pStages = stageStats?.players?.[String(pid)] || null;
                      return (
                        <div
                          className="event-player-card clickable"
                          key={pid}
                          onClick={() =>
                            openScoringBreakdown({
                              player_id: Number(pid),
                              name: playerLookup[Number(pid)] || pid,
                              team_id: Number(tid),
                              points: total,
                              rating: Number(comps.rating_points_total || 0),
                              win: Number(comps.win_points_total || 0),
                              role: Number(comps.role_points_total || 0),
                              booster: Number(comps.booster_points_total || 0),
                              role_id: comps.role_id,
                              stage_ev: pStages,
                              stage_list: stageList,
                              team_reach: teamStage,
                              booster_slots: comps.booster_slots,
                              components_available: true,
                              point_breakdown: comps.point_breakdown || [],
                            })
                          }
                        >
                          <PlayerPhoto playerId={Number(pid)} name={playerLookup[Number(pid)]} size={52} />
                          <div className="event-player-name">{playerLookup[Number(pid)] || pid}</div>
                          {comps.role_id != null && (
                            <div className="event-player-role">
                              <RoleBadge roleId={comps.role_id} size={16} />
                              <span>{ROLE_NAMES[comps.role_id] || `Role ${comps.role_id}`}</span>
                            </div>
                          )}
                          <div className="event-player-rating">{total.toFixed(2)}</div>
                          <div className="event-player-stats ev-mini">
                            {[
                              ["Rating", comps.rating_points_total],
                              ["Win", comps.win_points_total],
                              ["Role", comps.role_points_total],
                              ["Boost", comps.booster_points_total],
                            ].map(([label, value]) => (
                              <div className="event-player-stat" key={label}>
                                <div className="event-player-mini">{Number(value || 0).toFixed(1)}</div>
                                <div className="event-player-stat-label">{label}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {playoffTab === "top5" && playoffTopSubtab !== "completed" && (allTeams || topTeams) && (allTeams || topTeams).length > 0 && (
        <div className="card sub">
          <h3>Top Teams</h3>
          <ComboPager page={page} pageSize={COMBO_PAGE_SIZE} total={filteredCount} onPage={querySharedCombinations} />
          <div className="event-team-rows">
            {(allTeams && allTeams.length > 0 ? allTeams : topTeams).map((team, idx) => (
              <div key={idx} className="stack combo-team">
                <div className="event-team-row">
                  <div className="event-team-head">
                    <div className="combo-rank">#{idx + 1 + page * COMBO_PAGE_SIZE}</div>
                    <div className="event-team-headtext">
                      <span className="event-team-name">{playoffTeamMetricLabel(team)}</span>
                      {playoffTeamMetricSub(team).map((line) => (
                        <span className="combo-cost" key={line}>
                          {line}
                        </span>
                      ))}
                      <span className="combo-cost">Cost ${Number(team.cost || 0).toLocaleString()}</span>
                    </div>
                  </div>
                  <div className="event-team-players">
                    {team.players.map((p) => {
                      const roleNum = Number(p.role_name);
                      const roleId =
                        p.role_name != null && p.role_name !== "" && Number.isFinite(roleNum) ? roleNum : roleIdFromName(p.role_name);
                      return (
                        <div
                          className="event-player-card clickable"
                          key={p.player_id}
                          onClick={() => openProjectedRosterBreakdown(p, team)}
                        >
                          <PlayerPhoto playerId={Number(p.player_id)} name={p.name} size={52} />
                          <div className="event-player-name">{p.name}</div>
                          <div className="event-player-team">{teamLookup[p.team_id] || p.team_id}</div>
                          <div className="event-player-role">
                            {roleId != null && <RoleBadge roleId={roleId} size={16} />}
                            <span>{roleLabel(p.role_name)}</span>
                          </div>
                          <div className="event-player-rating">{playoffPlayerModeScore(p).toFixed(2)}</div>
                          <div className="event-player-stats ev-mini">
                            {POINTS_PARTS.map(([label, part]) => [label, rosterPoints(p, playoffBestMode)[part]]).map(([label, value]) => (
                              <div className="event-player-stat" key={label}>
                                <div className="event-player-mini">{Number(value || 0).toFixed(1)}</div>
                                <div className="event-player-stat-label">{label}</div>
                              </div>
                            ))}
                          </div>
                          <div className="event-player-price combo-price">${Math.round(Number(p.price || 0) / 1000)}k</div>
                        </div>
                      );
                    })}
                  </div>
                </div>
                {playoffBestMode === "most_outcomes" &&
                  (() => {
                    const pat = winningPatterns(team);
                    if (!pat) return null;
                    return (
                      <div className="combo-outcomes">
                        <p className="muted">
                          Finishing positions when this roster wins · {pat.count.toLocaleString()} brackets · {(pat.total * 100).toFixed(1)}%
                        </p>
                        <div className="pattern-bars">
                          <div className="pattern-legend">
                            {pat.columns.map((c, k) => (
                              <span className="pattern-legend-item" key={c}>
                                <i className={`pattern-swatch c${k}`} />
                                {c}
                              </span>
                            ))}
                          </div>
                          {pat.rows.map((r) => (
                            <div className="pattern-bar-row" key={r.team_id}>
                              <span className="pattern-team">
                                <TeamBadge teamId={r.team_id} />
                                <span>{teamLookup[r.team_id] || r.team_id}</span>
                              </span>
                              <div className="pattern-bar">
                                {r.shares.map((share, k) =>
                                  share > 0.0005 ? (
                                    <span
                                      className={`pattern-seg c${k}`}
                                      style={{ width: `${share * 100}%` }}
                                      title={`${pat.columns[k]} ${Math.round(share * 100)}%`}
                                      key={k}
                                    >
                                      {share >= 0.07 ? `${Math.round(share * 100)}%` : ""}
                                    </span>
                                  ) : null
                                )}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  })()}
              </div>
            ))}
          </div>
        </div>
      )}
      {playoffTab === "top5" && playoffTopSubtab !== "completed" && baseTeams && filteredCount === 0 && (
        <div className="card sub">
          <p className="muted">No team combinations match current include/exclude/search filters.</p>
        </div>
      )}
      {playoffTab === "value" && results && (
        <PriceVsPointsPanel
          title="Player Price vs Points (Playoff)"
          rows={playoffPlayerValueData.rows}
          slope={playoffPlayerValueData.slope}
          intercept={playoffPlayerValueData.intercept}
        />
      )}
      {playoffTab === "value" && !results && (
        <div className="card sub">
          <p className="muted">Run Playoff Bracket first.</p>
        </div>
      )}
      {playoffTab === "top5" && playoffTopSubtab !== "completed" && topMessage && (
        <div className="card sub">
          <p className="muted">{topMessage}</p>
        </div>
      )}

      {showFilterModal && (
        <div className="modal-backdrop" onClick={() => setShowFilterModal(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-header">
              <h3>Select Players to Include/Exclude</h3>
              <button className="close" onClick={() => setShowFilterModal(false)}>
                &times;
              </button>
            </header>
            <div className="modal-body">
              <Input
                label="Search player name"
                value={filterSearch}
                onChange={setFilterSearch}
                placeholder="Type e.g. zy to find ZywOo"
              />
              {filterSearch.trim().length === 0 && <p className="muted">Start typing a player name.</p>}
              {filterSearch.trim().length > 0 && modalMatches.length === 0 && <p className="muted">No matching players.</p>}
              {modalMatches.length > 0 && (
                <table>
                  <thead>
                    <tr>
                      <th>Player</th>
                      <th>ID</th>
                      <th>Include</th>
                      <th>Exclude</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modalMatches.map((p) => {
                      const pid = Number(p.player_id);
                      const isIncluded = includeSet.has(pid);
                      const isExcluded = excludeSet.has(pid);
                      return (
                        <tr key={pid}>
                          <td>{p.name}</td>
                          <td>{pid}</td>
                          <td>
                            <button
                              className={isIncluded ? "chip active" : "chip"}
                              onClick={() => {
                                setIncludeSet((prev) => {
                                  const next = new Set(prev);
                                  if (next.has(pid)) next.delete(pid);
                                  else next.add(pid);
                                  return next;
                                });
                                setExcludeSet((prev) => {
                                  const next = new Set(prev);
                                  next.delete(pid);
                                  return next;
                                });
                              }}
                            >
                              {isIncluded ? "Included" : "Include"}
                            </button>
                          </td>
                          <td>
                            <button
                              className={isExcluded ? "chip active" : "chip"}
                              onClick={() => {
                                setExcludeSet((prev) => {
                                  const next = new Set(prev);
                                  if (next.has(pid)) next.delete(pid);
                                  else next.add(pid);
                                  return next;
                                });
                                setIncludeSet((prev) => {
                                  const next = new Set(prev);
                                  next.delete(pid);
                                  return next;
                                });
                              }}
                            >
                              {isExcluded ? "Excluded" : "Exclude"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
            <div className="actions">
              <button className="primary" onClick={() => setShowFilterModal(false)}>
                Done
              </button>
            </div>
          </div>
        </div>
      )}
      <GroupPlayerBreakdownModal
        player={adaptPlayoffBreakdown(completedPlayerBreakdown)}
        teamLookup={teamLookup}
        teams={teams}
        onClose={() => setCompletedPlayerBreakdown(null)}
      />

    </Section>
  );
}

// Human labels for detected tournament structures. Built from everything the
// kind detector reports (kind + group format + combined playoff bracket), so
// "groups with a combined playoff" reads as exactly that instead of a bare
// "groups"/"playoff" pick.
// The Valuation column's word for an event's lifecycle state
// (event_pipeline.event_status): the long operator label stays as the
// tooltip. Published means the valuations are there, whatever the stage.
// The Status column: where the event is in its life — Upcoming, In
// progress or Finished — with a tone to match; an unsupported format shows
// as such for the operator. The pipeline's long label stays the operator's
// tooltip.
const eventStatus = (v) => {
  if (!v) return { label: "-", cls: "" };
  if (!PUBLIC_BUILD && v.status === "unsupported") return { label: "Unsupported", cls: "off" };
  if (v.finished) return { label: "Finished", cls: "finished" };
  if (v.started) return { label: "In progress", cls: "live" };
  return { label: "Upcoming", cls: "upcoming" };
};

const describeEventFormat = (k) => {
  if (!k || !k.kind) return null;
  // The backend now composes an authoritative label from the event page's
  // structured data (variant, group count/size, bracket size) — prefer it.
  if (k.label) return { key: k.label, short: k.label, long: "" };
  const gf = String(k.group_format || "");
  const po = Number(k.playoff_size || 0);
  const teams = Number(k.team_count || 0);
  if (k.kind === "swiss") {
    return {
      key: "swiss",
      short: "Swiss Stage",
      long: `Swiss system${teams ? ` (${teams} teams)` : ""} — win three rounds to advance, lose three and you're out.`,
    };
  }
  if (k.kind === "bounty") {
    return {
      key: "bounty",
      short: "Bounty",
      long: "BLAST Bounty draft — seeded teams pick their quarter-final opponents in a single-elimination bracket.",
    };
  }
  if (k.kind === "groups") {
    const gfShort = gf === "gsl4" ? "GSL Groups" : gf === "de8" ? "Double-Elim Groups" : "Group Stage";
    const gfLong =
      gf === "gsl4"
        ? "Four-team GSL groups — opening, winners', elimination and decider matches (double elimination inside each group)."
        : gf === "de8"
        ? "Eight-team double-elimination groups — an upper and lower bracket inside each group."
        : "Round-robin style group stage.";
    if (po > 0) {
      return {
        key: `groups-${gf || "rr"}-po`,
        short: `${gfShort} + Playoff`,
        long: `${gfLong} Combined with a ${po}-team playoff bracket that the group placings feed into.`,
      };
    }
    return { key: `groups-${gf || "rr"}`, short: gfShort, long: gfLong };
  }
  if (k.kind === "playoff") {
    return {
      key: `playoff-${teams || po || 0}`,
      short: `Playoff${teams ? ` (${teams} teams)` : ""}`,
      long: `Single-elimination playoff bracket${teams ? ` with ${teams} priced teams` : ""}.`,
    };
  }
  return { key: String(k.kind), short: String(k.kind), long: "" };
};

// Live: every current fantasy event as it actually stands, from the stored
// results (services/live_state). The Swiss board and the bracket board are
// the completed-stage pickers in read-only mode, filled with real winners.
const LIVE_GROUPS = [
  { key: "live", title: "Live" },
  { key: "upcoming", title: "Upcoming" },
  { key: "finished", title: "Previous" },
];
// An open event's page: the event as it stands, or the user's own HLTV
// fantasy team in it (MyTeamPanel; the account is connected on Settings).
const LIVE_EVENT_SUBTABS = [
  { key: "event", label: "Event" },
  { key: "myteam", label: "My Team" },
  { key: "boosters", label: "Boosters" },
  { key: "leaderboard", label: "Leaderboard" },
];

function LiveTab({ onOpenSettings }) {
  const [events, setEvents] = useState(null);
  const [openId, setOpenId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [sub, setSub] = useState("event");
  const load = async () => {
    try {
      const d = await api.get("/live/events", 60000);
      setEvents(Array.isArray(d?.events) ? d.events : []);
    } catch {
      setEvents((prev) => prev || []);
    }
  };
  const loadDetail = async (id) => {
    if (!id) return;
    try {
      const d = await api.get(`/live/events/${id}`, 60000);
      if (d?.event) setDetail(d.event);
    } catch {
      /* the list row stays; the modal shows what it has */
    }
  };
  const openIdRef = useRef(null);
  useEffect(() => {
    load();
    const t = setInterval(load, 120000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    openIdRef.current = openId;
    setDetail(null);
    setSub("event");
    loadDetail(openId);
    if (!openId) return undefined;
    const t = setInterval(() => loadDetail(openId), 60000);
    return () => clearInterval(t);
  }, [openId]);
  // The server says an event's pages changed: the list and, if it is the
  // open one, the page re-read at once.
  useLiveStream((change) => {
    load();
    if (openIdRef.current && Number(change.event_id) === Number(openIdRef.current)) loadDetail(openIdRef.current);
  });
  const dm = (ts) => {
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}`;
  };
  const formatOf = (ev) => (ev.group_format === "swiss" ? "Swiss" : ev.group_format === "bracket" ? (ev.kind === "double_elim" ? "Double elimination" : "Single elimination") : "Groups");
  if (events === null) return <div className="stack" />;
  const open = events.find((e) => e.event_id === openId) || null;
  if (openId) {
    const ev = detail || open;
    return (
      <div className="stack">
        <div className="live-detail-head">
          <button className="secondary live-back" onClick={() => setOpenId(null)} aria-label="Back to the tournament list">
            &larr; Back
          </button>
          <h2 className="live-detail-title">{ev?.name || ""}</h2>
        </div>
        <div className="tab-bar small">
          {LIVE_EVENT_SUBTABS.map((t) => (
            <button key={t.key} className={sub === t.key ? "tab active" : "tab"} onClick={() => setSub(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
        {sub === "myteam" ? (
          <MyTeamPanel ev={ev} onOpenSettings={onOpenSettings} />
        ) : sub === "boosters" ? (
          <BoosterPanel ev={ev} onOpenSettings={onOpenSettings} />
        ) : sub === "leaderboard" ? (
          <LeaderboardPanel ev={ev} />
        ) : detail ? (
          <LiveEventBody ev={detail} />
        ) : null}
      </div>
    );
  }
  return (
    <div className="stack">
      {LIVE_GROUPS.map((g) => {
        const rows = events.filter((e) => e.status === g.key);
        return (
          <Section title={g.title} key={g.key}>
            {rows.length === 0 ? (
              <p className="muted">None</p>
            ) : (
              <table className="live-list">
                <thead>
                  <tr>
                    <th>Event</th>
                    <th>Format</th>
                    <th>Dates</th>
                    <th className="num">Teams</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((ev) => (
                    <tr key={ev.event_id} onClick={() => setOpenId(ev.event_id)}>
                      <td className="live-list-name">{ev.name}</td>
                      <td>{formatOf(ev)}</td>
                      <td>
                        {dm(ev.start_at)} – {dm(ev.end_at)}
                      </td>
                      <td className="num">{ev.team_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>
        );
      })}
    </div>
  );
}

// Settings: the app user's own preferences. For now the HLTV account that
// the My Team tab of a live event page reads with (services/my_team).
// Season: the HLTV fantasy season as the user's own account sees it — their
// standing, the season points every game earned (with the team / role /
// booster points behind them), the leaders and the users around them, and
// the season-points table (services/fantasy_season: stored per user,
// refreshed by the nightly batch after each event and when opened stale).
function SeasonTab({ onOpenSettings }) {
  const [data, setData] = useState(null);
  const [state, setState] = useState("loading"); // loading | ready | disconnected | error
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const poll = useRef(null);
  const load = async (refresh = false) => {
    clearTimeout(poll.current);
    if (refresh) setBusy(true);
    try {
      const d = await api.get(`/my-team/season${refresh ? "?refresh=1" : ""}`, 60000);
      if (d?.season_name) {
        setData(d);
        setState("ready");
        setError("");
      }
      if (d?.pending) poll.current = setTimeout(() => load(false), 4000);
      else {
        setBusy(false);
        if (!d?.season_name) {
          setState("error");
          setError(d?.error || "Could not read HLTV");
        }
      }
    } catch (e) {
      const msg = String(e?.message || "");
      setBusy(false);
      if (/HLTV login|Sign in|no longer accepts/i.test(msg)) {
        setState("disconnected");
        setError(msg);
      } else {
        setState("error");
        setError(msg || "Could not read HLTV");
      }
    }
  };
  useEffect(() => {
    load(false);
    return () => clearTimeout(poll.current);
  }, []);
  const n = (v) => (v == null ? "-" : Number(v).toLocaleString());
  const fmtDate = (s) => (s ? new Date(Number(s) * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "-");
  const me = data?.me || null;
  const games = data?.games || [];
  const finishedTotal = games.filter((g) => !g.live).reduce((a, g) => a + Number(g.season_points || 0), 0);
  const leaders = data?.leaders || [];
  const around = (data?.around || []).filter((r) => !leaders.some((l) => l.placement === r.placement));
  const standingRow = (r) => (
    <tr key={`${r.placement}-${r.user_id}`} className={r.is_user ? "change" : ""}>
      <td className="num tight">{n(r.placement)}</td>
      <td>
        <strong>{r.username}</strong>
      </td>
      <td className="num">{n(r.leagues)}</td>
      <td className="num">{n(r.live_points)}</td>
      <td className="num">
        <strong>{n(r.season_points)}</strong>
      </td>
      <td>{r.prize ? r.prize : <span className="muted">-</span>}</td>
    </tr>
  );
  const part = (g, key) => (g.parts && g.parts[key] ? (
    <span>
      {n(g.parts[key].points)} <span className="muted">{g.parts[key].pct}</span>
    </span>
  ) : (
    <span className="muted">-</span>
  ));
  return (
    <div className="stack">
      <Section title={data?.season_name || "Season"}>
        {(state === "disconnected" || state === "error") && (
          <div className="bp-head">
            <span className="pill warn">{error || "No HLTV account connected"}</span>
            {state === "disconnected" && onOpenSettings && (
              <button className="secondary" onClick={onOpenSettings}>
                Settings
              </button>
            )}
            {state === "error" && (
              <button className="secondary" onClick={() => load(true)} disabled={busy}>
                Refresh
              </button>
            )}
          </div>
        )}
        {data && (
          <div className="mt-overview">
            <div className="card sub mt-box">
              <h3>Standing</h3>
              <div className="mt-rows">
                <div className="mt-row">
                  <span className="mt-row-label">Placement</span>
                  <span className="mt-row-value">{me ? `${n(me.placement)} of ${n(data.total_users)}` : "-"}</span>
                </div>
                <div className="mt-row">
                  <span className="mt-row-label">Season points</span>
                  <span className="mt-row-value">{me ? n(me.season_points) : "-"}</span>
                </div>
                <div className="mt-row">
                  <span className="mt-row-label">Live points</span>
                  <span className="mt-row-value">{me ? n(me.live_points) : "-"}</span>
                </div>
                <div className="mt-row">
                  <span className="mt-row-label">Games</span>
                  <span className="mt-row-value">{me ? `${n(me.leagues)} of ${n(data.finished_games)}` : n(data.finished_games)}</span>
                </div>
                <div className="mt-row">
                  <span className="mt-row-label">Season ends</span>
                  <span className="mt-row-value">{fmtDate(data.ends_at)}</span>
                </div>
                <div className="mt-row">
                  <span className="mt-row-label">Prizes</span>
                  <span className="mt-row-value">{(data.prizes || []).join(" · ") || "-"}</span>
                </div>
              </div>
            </div>
            <div className="card sub mt-box">
              <h3>Season points</h3>
              <div className="mt-rows">
                {(data.tiers || []).map((t) => (
                  <div className="mt-row" key={t.top_pct}>
                    <span className="mt-row-label">Top {t.top_pct}</span>
                    <span className="mt-row-value">{n(t.points)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </Section>
      {data && (
        <Section title="Games">
          <table className="bp-table lb-table">
            <thead>
              <tr>
                <th>Event</th>
                <th className="num">Placement</th>
                <th className="num">Top</th>
                <th className="num">Team pts</th>
                <th className="num">Role pts</th>
                <th className="num">Booster pts</th>
                <th className="num">Season pts</th>
              </tr>
            </thead>
            <tbody>
              {games.map((g) => (
                <tr key={`${g.event_id}-${g.team_id}`} className={g.live ? "change" : ""}>
                  <td>
                    <strong>{g.name}</strong>
                  </td>
                  <td className="num">
                    {n(g.placement)} <span className="muted">of {n(g.total)}</span>
                  </td>
                  <td className="num">{g.top_pct || "-"}</td>
                  <td className="num">{part(g, "team")}</td>
                  <td className="num">{part(g, "role")}</td>
                  <td className="num">{part(g, "boost")}</td>
                  <td className="num">
                    <strong>{n(g.season_points)}</strong>
                  </td>
                </tr>
              ))}
              <tr className="total">
                <td>Total</td>
                <td colSpan={5} />
                <td className="num">
                  <strong>{n(finishedTotal)}</strong>
                </td>
              </tr>
            </tbody>
          </table>
        </Section>
      )}
      {data && (
        <Section title="Standings">
          <table className="bp-table lb-table">
            <thead>
              <tr>
                <th className="num tight">#</th>
                <th>User</th>
                <th className="num">Games</th>
                <th className="num">Live pts</th>
                <th className="num">Season pts</th>
                <th>Prize</th>
              </tr>
            </thead>
            <tbody>
              {leaders.map(standingRow)}
              {leaders.length > 0 && around.length > 0 ? (
                <tr className="pad">
                  <td className="num tight">…</td>
                  <td colSpan={5} />
                </tr>
              ) : null}
              {around.map(standingRow)}
            </tbody>
          </table>
        </Section>
      )}
    </div>
  );
}

function SettingsTab() {
  return (
    <div className="stack">
      <HltvAccountSettings />
    </div>
  );
}

// The user's HLTV account: its connection state from the server, the
// sign-in window (or a pasted autologin cookie) and Disconnect. The server
// verifies a new login in the background; the status polls meanwhile.
function HltvAccountSettings() {
  const [account, setAccount] = useState(null); // {connected, username, days_left, verifying, error}
  const [autoAll, setAutoAll] = useState(false); // boosters set by the app in every event
  const [autoBusy, setAutoBusy] = useState(false);
  const [connecting, setConnecting] = useState("");
  const [pasted, setPasted] = useState("");
  const [showPaste, setShowPaste] = useState(false); // the pasted-cookie route while the sign-in window exists
  const poll = useRef(null);
  const loadStatus = () => {
    clearTimeout(poll.current);
    api
      .get("/my-team/session", 30000)
      .then((d) => {
        setAccount({ connected: Boolean(d?.connected), username: d?.username || "", days_left: d?.days_left, verifying: Boolean(d?.verifying), error: d?.error || "" });
        setAutoAll(Boolean(d?.auto_all));
        if (d?.verifying) poll.current = setTimeout(loadStatus, 3000);
      })
      .catch((e) => setAccount({ connected: false, error: e?.message || "" }));
  };
  useEffect(() => {
    loadStatus();
    return () => clearTimeout(poll.current);
  }, []);
  // Connect: the app opens hltv.org in a window of its own, the user signs in
  // there, and the session cookies go to the server for this user. Without
  // the bridge, or when the user chose to paste it, an autologin cookie
  // copied from their own browser.
  const connectAccount = async () => {
    setConnecting("Signing in…");
    try {
      let payload = null;
      if (window.api?.connectHltv && !(showPaste && pasted.trim())) {
        const r = await window.api.connectHltv();
        if (r?.status === "ok" && Array.isArray(r.cookies)) payload = { cookies: r.cookies };
        else if (r?.status === "busy") {
          setConnecting("");
          return;
        } else {
          setConnecting(r?.status === "timeout" ? "Timed out" : "");
          return;
        }
      } else if (pasted.trim()) {
        payload = { autologin: pasted.trim() };
      } else {
        setConnecting("");
        return;
      }
      setConnecting("Saving…");
      const d = await api.post("/my-team/session", payload, 60000);
      if (d?.detail) {
        setConnecting(String(d.detail));
        return;
      }
      setPasted("");
      setConnecting("");
      loadStatus();
    } catch (e) {
      setConnecting(e?.message || "Could not connect");
    }
  };
  const disconnectAccount = async () => {
    try {
      await api.delete("/my-team/session");
      if (window.api?.forgetHltv) await window.api.forgetHltv();
    } catch {
      /* the status reload shows the truth */
    }
    loadStatus();
  };
  const toggleAutoAll = async (enabled) => {
    setAutoBusy(true);
    try {
      await api.post("/my-team/settings", { auto_all: enabled }, 30000);
      setAutoAll(enabled);
    } catch {
      /* the next status load shows the truth */
    } finally {
      setAutoBusy(false);
    }
  };
  const hasBridge = Boolean(window.api?.connectHltv);
  const busy = Boolean(connecting) && connecting.endsWith("…");
  return (
    <Section title="HLTV account">
      <div className="myteam-connect">
        {account === null ? null : account.connected ? (
          <>
            <span className="pill ok">{account.username || (account.verifying ? "Verifying…" : "Connected")}</span>
            {account.days_left != null && <span className="muted">{Math.max(0, Math.round(account.days_left))} days left</span>}
            <button className="secondary" onClick={disconnectAccount}>
              Disconnect
            </button>
          </>
        ) : (
          <>
            <span className="pill warn">{account.error || "No HLTV account connected"}</span>
            {(!hasBridge || showPaste) && (
              <input
                id="settings-hltv-autologin"
                type="password"
                placeholder="HLTV autologin cookie"
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                className="myteam-cookie"
              />
            )}
            <button className="primary" onClick={connectAccount} disabled={busy}>
              {busy ? connecting : hasBridge && !showPaste ? "Connect HLTV account" : "Connect"}
            </button>
            {hasBridge && !showPaste && (
              <button className="secondary" onClick={() => setShowPaste(true)}>
                Paste cookie
              </button>
            )}
            {connecting && !busy && <span className="muted">{connecting}</span>}
          </>
        )}
      </div>
      {account?.connected && (
        <div className="bp-head settings-auto">
          <label className="switch" title={autoAll ? "Auto-apply on" : "Auto-apply off"}>
            <input type="checkbox" checked={Boolean(autoAll)} onChange={(e) => toggleAutoAll(e.target.checked)} disabled={autoBusy} />
            <span className="switch-track" />
          </label>
          <span className="bp-head-label">Auto-apply boosters in every event</span>
        </div>
      )}
    </Section>
  );
}

// My Team on a live event page: the user's HLTV fantasy team in THIS event
// — what HLTV has scored (per player and component) against the
// valuation's expectation, each player's next match with its predicted
// points, the round-by-round breakdown and every scored match (a row opens
// the match). The server reads HLTV in the background; the panel polls
// while the response says `pending`.
function MyTeamPanel({ ev, onOpenSettings }) {
  const eventId = ev?.event_id;
  const [report, setReport] = useState(null);
  const [state, setState] = useState("loading"); // loading | ready | none | disconnected | error
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [matchRow, setMatchRow] = useState(null);
  const [cardDetail, setCardDetail] = useState(null); // the match card whose breakdown is open
  const poll = useRef(null);
  const idRef = useRef(null);
  const load = async (id, refresh = false, quiet = false) => {
    if (!id) return;
    clearTimeout(poll.current);
    if (!quiet) setBusy(true);
    try {
      const d = await api.get(`/my-team/${id}${refresh ? "?refresh=1" : ""}`, 60000);
      if (idRef.current !== id) return; // the user opened another event meanwhile
      if (d?.players) {
        setReport(d);
        setState("ready");
      }
      if (d?.pending) poll.current = setTimeout(() => load(id, false, true), 4000);
      else setBusy(false);
    } catch (e) {
      if (idRef.current !== id) return;
      const msg = String(e?.message || "");
      setBusy(false);
      if (/No team of yours/i.test(msg)) setState("none");
      else if (/HLTV login|Sign in|no longer accepts/i.test(msg)) {
        setState("disconnected");
        setError(msg);
      } else {
        setState("error");
        setError(msg || "Could not read HLTV");
      }
    }
  };
  useEffect(() => {
    idRef.current = eventId;
    setReport(null);
    setState("loading");
    setError("");
    load(eventId);
    return () => clearTimeout(poll.current);
  }, [eventId]);
  // The event's pages changed on the server: ask for the report again (the
  // server re-reads it when it is old enough, keeping the shown one meanwhile).
  useLiveStream((change) => {
    if (idRef.current && Number(change.event_id) === Number(idRef.current)) load(idRef.current, false, true);
  });
  const fmt1 = (v) => (v == null || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(1));
  const signed = (v) => (v == null ? "-" : `${Number(v) > 0 ? "+" : ""}${Number(v)}`);
  const when = (ts) => {
    if (!(Number(ts) > 0)) return "";
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };
  const teamRows = useMemo(() => (report?.players || []).map((p) => ({ team_id: p.team_id, name: p.team })), [report]);
  // A scored match opens its scoreboard: the stored row when the nightly
  // import has it, else the page's scoreboards read on demand.
  const openMatch = async (t) => {
    const url = String(t.match_url || "");
    if (!url) return;
    const own = (report?.players || []).find((p) => p.player_id === t.player_id) || {};
    const base = { match_url: url, team1: own.team || t.name, team2: t.opponent || "", score1: null, score2: null };
    setMatchRow(base);
    if (PUBLIC_BUILD) return;
    try {
      const d = await api.get(`/events/hltv-results/match-details?match_url=${encodeURIComponent(url)}`, 60000);
      const stored = d?.stored || {};
      const next = { ...base, ...stored, maps_json: JSON.stringify(Array.isArray(d?.maps) ? d.maps : []) };
      if (!next.player_stats_json && Array.isArray(d?.player_stats) && d.player_stats.length > 0) next.player_stats_json = JSON.stringify(d.player_stats);
      if (!next.map_player_stats_json && d?.map_player_stats && Object.keys(d.map_player_stats).length > 0) next.map_player_stats_json = JSON.stringify(d.map_player_stats);
      setMatchRow((cur) => (cur && cur.match_url === url ? next : cur));
    } catch {
      /* the modal keeps the stored row */
    }
  };
  if (!report) {
    return (
      <Section title="My Team">
        <div className="myteam-connect">
          {state === "loading" && <span className="pill">Reading…</span>}
          {state === "none" && <span className="pill">No team in this event</span>}
          {(state === "disconnected" || state === "error") && <span className="pill warn">{error || "No HLTV account connected"}</span>}
          {state === "disconnected" && onOpenSettings && (
            <button className="secondary" onClick={onOpenSettings}>
              Settings
            </button>
          )}
          {state === "error" && (
            <button className="secondary" onClick={() => load(eventId, true)} disabled={busy}>
              Refresh
            </button>
          )}
        </div>
      </Section>
    );
  }
  const key = report.key || {};
  const players = report.players || [];
  const tracker = report.tracker || [];
  const actualTotal = key.total_points ?? players.reduce((a, p) => a + Number(p.points || 0), 0);
  const predictedNext = players.reduce((a, p) => a + Number(p.next_match?.predicted?.total || 0), 0);
  const expectedSoFar = report.expected_so_far != null ? report.expected_so_far : report.expected_total || 0;
  const hasPrediction = players.some((p) => p.next_match?.predicted);
  // HLTV's key numbers minus the money-league placement (only the league
  // placement matters here).
  const keyRows = (Array.isArray(key.rows) && key.rows.length ? key.rows : [
    { label: "League placement", value: key.placement != null ? `${Number(key.placement).toLocaleString()} / ${Number(key.league_size || 0).toLocaleString()}` : "-" },
    { label: "Role / boosters", value: `${key.roles_assigned ?? "-"} / ${key.boosters_assigned ?? "-"}` },
    { label: "Total points", value: key.total_points ?? "-" },
  ]).filter((r) => !/money/i.test(String(r.label || "")));
  // Where the team should stand when this round is done: the points now plus
  // the model's prediction for every player whose match in it is still to be
  // played or in play.
  const endOfRound = Number(actualTotal) + predictedNext;
  // HLTV's messages, each one carrying the match's points: a line about a
  // player's match that omits the number gets it from the tracker.
  const messageWithPoints = (msg) => {
    if (/\d+\s*points?/i.test(msg)) return msg;
    const entry = tracker.find((t) => t.name && t.opponent && t.points != null && msg.startsWith(t.name) && msg.includes(t.opponent));
    return entry ? `${msg} · ${entry.points} points` : msg;
  };
  // The player's next match line, HLTV's way: "Today - 17:00" then "vs. X";
  // our live pages take precedence when they know the match.
  const nextLine = (p) => {
    const n = p.next_match;
    if (n) return { when: n.status === "live" ? "In match" : when(n.start_at), opp: n.opponent, live: n.status === "live", bo: n.bo };
    if (p.next_opponent) return { when: p.next_start_text || "", opp: p.next_opponent, live: Boolean(p.in_match), bo: null };
    return null;
  };
  const resultWord = (t) => (t.result === "WON" ? "Won" : t.result === "LOST" ? "Lost" : t.result === "NEXT" ? "Next match" : t.result ? t.result.charAt(0) + t.result.slice(1).toLowerCase() : "-");
  // One player's match card: portrait, name and result; on the right the
  // points scored, the model's expectation for the round and the difference
  // (a coming match: its start time and the expectation). Clicking a card
  // opens the breakdown by component (rating, team, role, booster).
  const COMPS = [
    ["rating", "Rating"],
    ["team", "Team"],
    ["role", "Role"],
    ["booster", "Booster"],
    ["total", "Total"],
  ];
  const actualOf = (t, k) => (k === "total" ? t.points : k === "rating" ? t.rating_points : k === "team" ? t.team_points : k === "role" ? t.role_points : t.booster_points);
  const diffClass = (v) => (v == null ? "" : Number(v) < 0 ? " neg" : Number(v) > 0 ? " pos" : "");
  const trackCard = (t, i) => {
    const played = t.result === "WON" || t.result === "LOST";
    const openable = Boolean(t.expected_parts) || played;
    return (
      <div className={`mt-track${openable ? " clickable" : ""}${t.result === "NEXT" ? " next" : ""}`} key={`${t.player_id}-${i}`} onClick={() => openable && setCardDetail(t)}>
        <div className="mt-track-top">
          <PlayerPhoto playerId={t.player_id} name={t.name} size={56} className="mt-track-photo" />
          <div className="mt-track-text">
            <div className="mt-track-name">{t.name}</div>
            <div className={`mt-track-result${t.result === "WON" ? " won" : t.result === "LOST" ? " lost" : ""}`}>
              {t.opponent_hltv_team_id ? <TeamLogo hltvTeamId={t.opponent_hltv_team_id} name={t.opponent} size={18} /> : null}
              {resultWord(t)}
              {t.opponent && t.result !== "NEXT" ? ` vs ${t.opponent}` : ""}
              {t.result === "NEXT" && t.opponent ? ` · ${t.opponent}` : ""}
            </div>
          </div>
          {t.result === "NEXT" ? (
            <div className="mt-track-pts">
              <div className="mt-track-time">{(t.start_text || "").replace(" - ", "\n").split("\n").map((part, k) => <div key={k}>{part}</div>)}</div>
              {t.expected != null && <div className="mt-track-exp">exp {fmt1(t.expected)}</div>}
            </div>
          ) : (
            <div className={`mt-track-points${Number(t.points) < 0 ? " neg" : ""}`}>{t.points ?? 0} pts</div>
          )}
        </div>
        {played && (
          <div className="event-player-stats ev-mini mt-track-stats">
            {[
              ["Rating", t.rating_points],
              ["Team", t.team_points],
              ["Role", t.role_points],
              ["Booster", t.booster_points],
            ].map(([label, v]) => (
              <div className="event-player-stat" key={label}>
                <span className="event-player-mini">{signed(v)}</span>
                <span className="event-player-stat-label">{label}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };
  // The card's breakdown: the match's points by component against the
  // model's expectation for the round.
  const cardDetailModal = cardDetail && (
    <div className="modal-backdrop" onClick={() => setCardDetail(null)}>
      <div className="modal mt-card-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <div className="modal-title-wrap">
            <PlayerPhoto playerId={cardDetail.player_id} name={cardDetail.name} size={44} />
            <div>
              <h3>{cardDetail.name}</h3>
              <div className={`mt-track-result${cardDetail.result === "WON" ? " won" : cardDetail.result === "LOST" ? " lost" : ""}`}>
                {cardDetail.opponent_hltv_team_id ? <TeamLogo hltvTeamId={cardDetail.opponent_hltv_team_id} name={cardDetail.opponent} size={18} /> : null}
                {resultWord(cardDetail)}
                {cardDetail.opponent ? ` ${cardDetail.result === "NEXT" ? "·" : "vs"} ${cardDetail.opponent}` : ""}
                {cardDetail.result === "NEXT" && cardDetail.start_text ? ` · ${cardDetail.start_text}` : ""}
              </div>
            </div>
          </div>
          <button className="close" onClick={() => setCardDetail(null)} aria-label="Close">
            ×
          </button>
        </header>
        <div className="modal-body">
          <table className="mt-mini mt-detail-table">
            <thead>
              <tr>
                <th></th>
                {COMPS.map(([k, label]) => (
                  <th key={k} className={k === "total" ? "strong" : ""}>
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(cardDetail.result === "WON" || cardDetail.result === "LOST") && (
                <tr>
                  <td className="mt-mini-label">Actual</td>
                  {COMPS.map(([k]) => (
                    <td key={k} className={k === "total" ? "strong" : ""}>
                      {signed(actualOf(cardDetail, k))}
                    </td>
                  ))}
                </tr>
              )}
              <tr>
                <td className="mt-mini-label">Expected</td>
                {COMPS.map(([k]) => (
                  <td key={k} className={k === "total" ? "strong" : ""}>
                    {cardDetail.expected_parts && cardDetail.expected_parts[k] != null ? fmt1(cardDetail.expected_parts[k]) : "-"}
                  </td>
                ))}
              </tr>
              {cardDetail.difference_parts && (
                <tr>
                  <td className="mt-mini-label">Difference</td>
                  {COMPS.map(([k]) => (
                    <td key={k} className={`${diffClass(cardDetail.difference_parts[k])}${k === "total" ? " strong" : ""}`}>
                      {cardDetail.difference_parts[k] != null ? signed(cardDetail.difference_parts[k]) : "-"}
                    </td>
                  ))}
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
  return (
    <div className="stack">
      <Section title={report.team?.team_name || "My Team"}>
        <div className="mt-lineup">
          {players.map((p) => {
            const n = nextLine(p);
            const live = Boolean(p.in_match || n?.live);
            const x = p.expected || null;
            return (
              <div className={`mt-card${live ? " live" : ""} level-${p.level || "none"}`} key={p.player_id || p.name}>
                <div className={`mt-role${p.role ? "" : " none"}`}>{p.role || "No role"}</div>
                <div className="mt-photo-wrap">
                  <PlayerPhoto playerId={p.player_id} name={p.name} size={112} className="mt-photo" />
                  {p.hltv_team_id ? <TeamLogo hltvTeamId={p.hltv_team_id} name={p.team} size={28} className="mt-card-logo" /> : null}
                  {live && <div className="mt-inmatch">In match</div>}
                </div>
                <div className="mt-name">{p.name}</div>
                <div className="mt-team">{p.team || ""}</div>
                <div className={`mt-points${Number(p.points) < 0 ? " neg" : ""}`}>{p.points ?? 0} points</div>
                <div className="mt-expected">expected {x ? fmt1(x.so_far != null ? x.so_far : x.total) : "-"}</div>
                <div className="mt-next">
                  {n ? (
                    <>
                      <div className="mt-next-when">{n.when || ""}</div>
                      <div className="mt-next-opp">
                        vs. {n.opp}
                        {n.bo ? <span className="muted"> · Bo{n.bo}</span> : null}
                      </div>
                      {p.next_match?.predicted ? <div className="mt-next-pred">predicted {fmt1(p.next_match.predicted.total)}</div> : null}
                    </>
                  ) : (
                    <div className="mt-next-none">No upcoming match</div>
                  )}
                </div>
                <div className={`mt-booster${p.booster ? " assigned" : ""}`}>{p.booster || "No booster"}</div>
              </div>
            );
          })}
        </div>
      </Section>
      <div className="mt-overview">
        <div className="card sub mt-box">
          <h3>Key numbers</h3>
          <div className="mt-rows">
            {keyRows.map((r) => (
              <div className="mt-row" key={r.label}>
                <span className="mt-row-label">{r.label}</span>
                <span className="mt-row-value">{r.value}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card sub mt-box">
          <h3>Model</h3>
          <div className="mt-rows">
            <div className="mt-row">
              <span className="mt-row-label">Points</span>
              <span className="mt-row-value">{actualTotal}</span>
            </div>
            <div className="mt-row">
              <span className="mt-row-label">Expected so far</span>
              <span className="mt-row-value">{fmt1(report.expected_so_far != null ? report.expected_so_far : report.expected_total)}</span>
            </div>
            <div className="mt-row">
              <span className="mt-row-label">Against expectation</span>
              <span className={`mt-row-value${Number(actualTotal) - Number(expectedSoFar) < 0 ? " neg" : " pos"}`}>{signed(Math.round((Number(actualTotal) - Number(expectedSoFar)) * 10) / 10)}</span>
            </div>
            <div className="mt-row">
              <span className="mt-row-label">End of round predicted</span>
              <span className="mt-row-value">{hasPrediction ? fmt1(endOfRound) : actualTotal}</span>
            </div>
          </div>
        </div>
        {(report.messages || []).length > 0 && (
          <div className="card sub mt-box">
            <h3>Messages</h3>
            <div className="mt-messages">
              {report.messages.map((msg, i) => (
                <div className="mt-message" key={i}>
                  {messageWithPoints(String(msg))}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
      {(report.rounds || []).length > 0 && (
        <Section title="Round by round">
          <div className="mt-rounds">
            {report.rounds.map((r) => {
              const cards = Array.isArray(r.players) ? r.players : [];
              return (
                <div className={`mt-round${r.status === "LIVE" ? " live" : ""}`} key={r.name}>
                  <div className="mt-round-head">
                    <span className="mt-round-name">{r.name}</span>
                  </div>
                  {cards.length > 0 && <div className="mt-tracker">{cards.map((t, i) => trackCard(t, i))}</div>}
                  <div className="mt-round-foot">
                    {[
                      ["Expected", r.expected_total != null ? fmt1(r.expected_total) : "-", ""],
                      ["Actual", r.status === "TBA" ? "-" : signed(r.subtotal), r.difference != null ? diffClass(r.difference) : ""],
                      ["Placement", r.placement != null ? `${Number(r.placement).toLocaleString()} / ${Number(key.league_size || 0).toLocaleString()}` : "-", ""],
                      ["Total", r.status === "TBA" ? "-" : r.total ?? "-", " strong"],
                      ["Expected total", r.running_expected_parts && r.running_expected_parts.total != null ? fmt1(r.running_expected_parts.total) : "-", ""],
                    ].map(([label, v, cls]) => (
                      <div className={`mt-round-num${cls}`} key={label}>
                        <span className="mt-round-num-value">{v}</span>
                        <span className="mt-round-num-label">{label}</span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </Section>
      )}
      {(report.rounds || []).every((r) => !(Array.isArray(r.players) && r.players.length)) && tracker.length > 0 && (
        <Section title="Player tracker">
          <div className="mt-tracker">{tracker.map((t, i) => trackCard(t, i))}</div>
        </Section>
      )}
      {matchRow && <MatchDetailModal row={matchRow} teams={teamRows} players={[]} onClose={() => setMatchRow(null)} />}
      {cardDetailModal}
    </div>
  );
}

// Boosters on a live event page: which booster each of the user's players
// should carry into his coming match (the roster-wide plan over the
// boosters still free, GET /my-team/{id}/boosters), what is on HLTV now,
// Apply for one or all, and the auto switch that has the server set them
// for the rest of the event. The server reads in the background; the
// panel polls while pending and re-asks when the live stream reports the
// event changed.
function BoosterPanel({ ev, onOpenSettings }) {
  const eventId = ev?.event_id;
  const [data, setData] = useState(null);
  const [state, setState] = useState("loading"); // loading | ready | disconnected | error
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const poll = useRef(null);
  const idRef = useRef(null);
  const load = async (id, refresh = false, quiet = false) => {
    if (!id) return;
    clearTimeout(poll.current);
    try {
      const d = await api.get(`/my-team/${id}/boosters${refresh ? "?refresh=1" : ""}`, 60000);
      if (idRef.current !== id) return;
      if (Array.isArray(d?.players)) {
        setData(d);
        setState("ready");
      } else if (d?.error) {
        setError(String(d.error));
        setState(/HLTV login|Sign in|no longer accepts/i.test(String(d.error)) ? "disconnected" : "error");
      }
      if (d?.pending) poll.current = setTimeout(() => load(id, false, true), 4000);
    } catch (e) {
      if (idRef.current !== id) return;
      const msg = String(e?.message || "");
      setError(msg || "Could not read HLTV");
      setState(/HLTV login|Sign in|no longer accepts/i.test(msg) ? "disconnected" : "error");
    }
  };
  useEffect(() => {
    idRef.current = eventId;
    setData(null);
    setState("loading");
    setError("");
    load(eventId);
    return () => clearTimeout(poll.current);
  }, [eventId]);
  useLiveStream((change) => {
    if (idRef.current && Number(change.event_id) === Number(idRef.current)) load(idRef.current, false, true);
  });
  const applyAll = async () => {
    setBusy("all");
    try {
      await api.post(`/my-team/${eventId}/boosters/apply`, { all: true }, 120000);
      await load(eventId, true, true);
    } catch (e) {
      setError(e?.message || "Could not set the boosters");
    } finally {
      setBusy("");
    }
  };
  const applyOne = async (p) => {
    if (!p?.suggestion || !p?.match) return;
    setBusy(String(p.player_id));
    try {
      await api.post(`/my-team/${eventId}/boosters/apply`, { assignments: [{ player_id: p.player_id, match_id: p.match.match_id, booster_index: p.suggestion.index }] }, 120000);
      await load(eventId, true, true);
    } catch (e) {
      setError(e?.message || "Could not set the booster");
    } finally {
      setBusy("");
    }
  };
  const toggleAuto = async (enabled) => {
    setBusy("auto");
    try {
      await api.post(`/my-team/${eventId}/boosters/auto`, { enabled }, 30000);
      setData((cur) => (cur ? { ...cur, auto: enabled } : cur));
      if (enabled) poll.current = setTimeout(() => load(eventId, true, true), 8000);
    } catch (e) {
      setError(e?.message || "Could not change auto mode");
    } finally {
      setBusy("");
    }
  };
  const when = (ts) => {
    if (!(Number(ts) > 0)) return "";
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };
  const pct = (v) => (v == null ? "-" : `${Math.round(Number(v) * 100)}%`);
  const fmt1 = (v) => (v == null || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(1));
  const stageName = (s) => (typeof s === "string" && /^r\d+$/.test(s) ? `Round ${s.slice(1)}` : s || "");
  if (!data) {
    return (
      <Section title="Boosters">
        <div className="myteam-connect">
          {state === "loading" && <span className="pill">Reading…</span>}
          {(state === "disconnected" || state === "error") && <span className="pill warn">{error || "No HLTV account connected"}</span>}
          {state === "disconnected" && onOpenSettings && (
            <button className="secondary" onClick={onOpenSettings}>
              Settings
            </button>
          )}
          {state === "error" && (
            <button className="secondary" onClick={() => load(eventId, true)}>
              Refresh
            </button>
          )}
        </div>
      </Section>
    );
  }
  const players = data.players || [];
  const changes = players.filter((p) => p.change);
  return (
    <div className="stack">
      <Section title="Boosters">
        <div className="bp-head">
          <label className="switch" title={data.auto ? "Auto-apply on" : "Auto-apply off"}>
            <input type="checkbox" checked={Boolean(data.auto)} onChange={(e) => toggleAuto(e.target.checked)} disabled={busy === "auto" || Boolean(data.auto_all)} />
            <span className="switch-track" />
          </label>
          <span className="bp-head-label">Auto-apply for the rest of the event</span>
          <span className="pill">{data.free_count ?? 0} boosters left</span>
          {data.pending && <span className="pill">Reading…</span>}
          <button className="primary bp-apply-all" onClick={applyAll} disabled={Boolean(busy) || changes.length === 0}>
            {busy === "all" ? "Applying…" : changes.length > 0 ? `Apply ${changes.length} suggested` : "Suggestions applied"}
          </button>
        </div>
        {error && <span className="pill warn">{error}</span>}
        <table className="bp-table">
          <thead>
            <tr>
              <th>Player</th>
              <th>Next match</th>
              <th>On HLTV</th>
              <th>Suggested</th>
              <th className="num">Rate (Bo3)</th>
              <th className="num">This series</th>
              <th className="num">Expected</th>
              <th className="tight"></th>
            </tr>
          </thead>
          <tbody>
            {players.map((p) => {
              const m = p.match;
              const s = p.suggestion;
              const a = p.assigned;
              return (
                <tr key={p.player_id || p.name} className={p.change ? "change" : ""}>
                  <td>
                    <div className="table-player-cell">
                      <PlayerPhoto playerId={p.player_id} name={p.name} size={28} />
                      <strong>{p.name}</strong>
                    </div>
                  </td>
                  <td>
                    {m ? (
                      <span>
                        <strong>{m.opponent || "-"}</strong>
                        <span className="muted">
                          {" "}
                          · {m.live ? "live" : when(m.start_at)}
                          {m.bo ? ` · Bo${m.bo}` : ""}
                        </span>
                      </span>
                    ) : p.next_round ? (
                      <span>
                        <strong>{stageName(p.next_round.stage)}</strong>
                        {p.next_round.bo ? <span className="muted"> · Bo{p.next_round.bo}</span> : null}
                      </span>
                    ) : (
                      <span className="muted">No upcoming match</span>
                    )}
                  </td>
                  <td>{a?.name ? <span className={`pill${p.change ? "" : " ok"}`}>{a.name}</span> : <span className="muted">-</span>}</td>
                  <td>{s?.name ? <span className={`pill${p.change ? " caution" : " ok"}`}>{s.name}</span> : <span className="muted">-</span>}</td>
                  <td className="num">{s ? pct(s.rate) : "-"}</td>
                  <td className="num">{s ? pct(s.match_rate != null ? s.match_rate : s.rate) : "-"}</td>
                  <td className="num">{s ? fmt1(s.expected) : "-"}</td>
                  <td className="tight">
                    {p.change && (
                      <button className="secondary" onClick={() => applyOne(p)} disabled={Boolean(busy)}>
                        {busy === String(p.player_id) ? "Applying…" : "Apply"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Section>
    </div>
  );
}

// Leaderboard on a live event page: every player in the event scored the
// way HLTV scores them from the matches played so far — rating and win
// points to start (services/leaderboard). Re-read when the event's pages
// change on the server.
function LeaderboardPanel({ ev }) {
  const eventId = ev?.event_id;
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const idRef = useRef(null);
  const poll = useRef(null);
  const load = async (id) => {
    if (!id) return;
    clearTimeout(poll.current);
    try {
      const d = await api.get(`/live/events/${id}/leaderboard`, 60000);
      if (idRef.current !== id) return;
      setData(d);
      setError("");
      if (d?.field) {
        try {
          const b = await api.get(`/live/events/${id}/best-team`, 60000);
          if (idRef.current === id) setBest(b);
        } catch (e) {
          if (idRef.current === id) setBest(null);
        }
      } else {
        setBest(null);
      }
      // matches whose scoreboards are being read fill in within minutes
      if (Number(d?.matches_pending || 0) > 0) poll.current = setTimeout(() => load(id), 30000);
    } catch (e) {
      if (idRef.current !== id) return;
      setError(e?.message || "Could not read the leaderboard");
    }
  };
  useEffect(() => {
    idRef.current = eventId;
    setData(null);
    setError("");
    load(eventId);
    return () => clearTimeout(poll.current);
  }, [eventId]);
  useLiveStream((change) => {
    if (idRef.current && Number(change.event_id) === Number(idRef.current)) load(idRef.current);
  });
  const signed = (v) => (v == null ? "-" : `${Number(v) > 0 ? "+" : ""}${Number(v)}`);
  const players = data?.players || [];
  const field = !!data?.field;
  const [openId, setOpenId] = useState(null);
  const [sortBy, setSortBy] = useState("total_desc");
  const [best, setBest] = useState(null);
  const openRow = openId != null ? players.find((x) => x.player_id === openId) : null;
  const teamIds = useMemo(() => {
    const m = {};
    players.forEach((p) => {
      if (p.team && p.hltv_team_id) m[p.team] = p.hltv_team_id;
    });
    return m;
  }, [players]);
  const sorted = useMemo(() => {
    const [key, dir] = sortBy.split("_");
    const sign = dir === "asc" ? 1 : -1;
    const val = (r) => {
      switch (key) {
        case "name":
          return String(r.name || "");
        case "team":
          return String(r.team || "");
        case "owned":
          return Number(r.owned || 0);
        case "score":
          return Number(r.wins || 0) * 100 - (Number(r.matches || 0) - Number(r.wins || 0));
        case "rating":
          return Number(r.rating_avg || 0);
        case "ratingpts":
          return Number(r.rating_points || 0);
        case "winpts":
          return Number(r.win_points || 0);
        case "expected":
          return r.expected == null ? -1e9 : Number(r.expected);
        case "vsexp":
          return r.vs_expected == null ? -1e9 : Number(r.vs_expected);
        case "role":
          return Number(r.max?.role_points || 0);
        case "boosters":
          return Number(r.max?.booster_points || 0);
        default:
          return Number(r.total || 0);
      }
    };
    return [...players].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const c = typeof va === "string" ? va.localeCompare(vb) : va - vb;
      return c !== 0 ? c * sign : Number(b.total || 0) - Number(a.total || 0);
    });
  }, [players, sortBy]);
  const head = (label, key, opts = {}) => (
    <SortHeader
      sortValue={sortBy}
      asc={`${key}_asc`}
      desc={`${key}_desc`}
      defaultDirection={opts.asc ? "asc" : "desc"}
      onChange={setSortBy}
      className={opts.num ? "num" : undefined}
    >
      {label}
    </SortHeader>
  );
  const bestPlayers = best?.players || [];
  const slotCount = bestPlayers.reduce((n, p) => Math.max(n, (p.matches || []).length), 0);
  const slots = Array.from({ length: slotCount }, (_, i) => i);
  return (
    <div className="stack">
      {openRow && openRow.breakdown ? <LeaderboardPlayerModal p={openRow} teamIds={teamIds} onClose={() => setOpenId(null)} /> : null}
      {bestPlayers.length > 0 ? (
        <Section title="Best Team">
          <table className="bp-table lb-table">
            <thead>
              <tr>
                <th>Player</th>
                <th>Team</th>
                <th className="num">Price</th>
                <th>Role</th>
                {slots.map((i) => (
                  <th key={i}>{i + 1}</th>
                ))}
                <th className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {bestPlayers.map((p) => (
                <tr key={p.player_id}>
                  <td>
                    <div className="table-player-cell">
                      <PlayerPhoto playerId={p.player_id} name={p.name} size={28} />
                      <strong>{p.name}</strong>
                    </div>
                  </td>
                  <td>
                    <div className="table-player-cell">
                      <TeamLogo hltvTeamId={p.hltv_team_id} name={p.team} size={20} />
                      <span>{p.team}</span>
                    </div>
                  </td>
                  <td className="num">{Number(p.price || 0).toLocaleString()}</td>
                  <td>
                    {p.role_name ? (
                      <span>
                        {p.role_name} <span className="muted">{signed(p.role_points)}</span>
                      </span>
                    ) : (
                      <span className="muted">-</span>
                    )}
                  </td>
                  {slots.map((i) => {
                    const m = (p.matches || [])[i];
                    return (
                      <td className="lb-slot" key={i}>
                        {m ? (
                          <>
                            <span className="lb-slot-opp">vs {m.opponent}</span>
                            {m.booster ? (
                              <span>
                                {m.booster} <span className="muted">+5</span>
                              </span>
                            ) : (
                              <span className="muted">-</span>
                            )}
                          </>
                        ) : null}
                      </td>
                    );
                  })}
                  <td className="num">
                    <strong>{signed(p.total)}</strong>
                  </td>
                </tr>
              ))}
              <tr className="total">
                <td>Total</td>
                <td />
                <td className="num">{Number(best.cost || 0).toLocaleString()}</td>
                <td>{signed(best.role_points)}</td>
                {slotCount > 0 ? <td className="lb-slot" colSpan={slotCount}>{signed(best.booster_points)}</td> : null}
                <td className="num">
                  <strong>{signed(best.total)}</strong>
                </td>
              </tr>
            </tbody>
          </table>
        </Section>
      ) : null}
      <Section title="Leaderboard">
      {error && <span className="pill warn">{error}</span>}
      <table className="bp-table lb-table">
        <thead>
          <tr>
            {head("Player", "name", { asc: true })}
            {head("Team", "team", { asc: true })}
            {field ? head("Owned", "owned", { num: true }) : null}
            {head("Score", "score", { num: true })}
            {head("Rating", "rating", { num: true })}
            {head("Rating pts", "ratingpts", { num: true })}
            {head("Win pts", "winpts", { num: true })}
            {head("Expected", "expected", { num: true })}
            {head("vs Expected", "vsexp", { num: true })}
            {field ? head("Role", "role") : null}
            {field ? head("Boosters", "boosters") : null}
            {head("Total", "total", { num: true })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((p) => (
            <tr key={p.player_id} className={p.breakdown ? "row-link" : ""} onClick={() => p.breakdown && setOpenId(p.player_id)}>
              <td>
                <div className="table-player-cell">
                  <PlayerPhoto playerId={p.player_id} name={p.name} size={28} />
                  <strong>{p.name}</strong>
                </div>
              </td>
              <td>
                <div className="table-player-cell">
                  <TeamLogo hltvTeamId={p.hltv_team_id} name={p.team} size={20} />
                  <span>{p.team}</span>
                </div>
              </td>
              {field ? (
                <td className="num">
                  {Number(p.owned || 0).toLocaleString()}
                  {data.field.teams > 0 ? <span className="muted"> {Math.round((100 * Number(p.owned || 0)) / Number(data.field.teams))}%</span> : null}
                </td>
              ) : null}
              <td className="num">
                {Number(p.wins || 0)}-{Number(p.matches || 0) - Number(p.wins || 0)}
              </td>
              <td className="num">{p.rating_avg == null ? "-" : Number(p.rating_avg).toFixed(2)}</td>
              <td className="num">{p.matches > 0 ? signed(p.rating_points) : "-"}</td>
              <td className="num">{p.matches > 0 ? signed(p.win_points) : "-"}</td>
              <td className="num">{p.expected == null ? "-" : Number(p.expected).toFixed(1)}</td>
              <td className={`num ${p.vs_expected == null ? "" : p.vs_expected >= 0 ? "pos" : "neg"}`}>
                {p.vs_expected == null ? "-" : `${p.vs_expected > 0 ? "+" : ""}${Number(p.vs_expected).toFixed(1)}`}
              </td>
              {field ? (
                <td>
                  {p.max?.role_name ? (
                    <span>
                      {p.max.role_name} <span className="muted">{signed(p.max.role_points)}</span>
                    </span>
                  ) : (
                    <span className="muted">-</span>
                  )}
                </td>
              ) : null}
              {field ? (
                <td className="lb-boosters">
                  {p.max && (p.max.boosters || []).some(Boolean) ? (
                    <span>
                      {(p.max.boosters || []).map((b) => b || "none").join(", ")} <span className="muted">{signed(p.max.booster_points)}</span>
                    </span>
                  ) : (
                    <span className="muted">-</span>
                  )}
                </td>
              ) : null}
              <td className="num">
                <strong>{p.matches > 0 ? signed(p.total) : "-"}</strong>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </Section>
    </div>
  );
}

// A leaderboard row opens the player match by match, from the field: HLTV's
// rating and win points, the points of every role some team gave him, which
// boosters triggered, the padding rounds, and the theoretical maximum — the
// best role plus a different booster for every match one triggered in.
function LeaderboardPlayerModal({ p, teamIds, onClose }) {
  const b = p.breakdown || {};
  const ms = b.matches || [];
  const pad = b.padding || { count: 0 };
  const mx = p.max || {};
  const signed = (v) => (v == null ? "-" : `${Number(v) > 0 ? "+" : ""}${Number(v)}`);
  const cell = (v) => (v == null ? <span className="muted">-</span> : signed(v));
  const padRows = Array.from({ length: Number(pad.count || 0) }, (_, i) => i);
  const matchTotal = (m) => Number(m.rating || 0) + Number(m.team || 0) + Number(m.role || 0) + (m.booster ? 5 : 0);
  const padTotal = Number(pad.rating || 0) + Number(pad.team || 0) + Number(pad.role || 0);
  const oppHead = ms.map((m, i) => (
    <th className="num" key={m.match_url || i}>
      {m.opponent}
    </th>
  ));
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal player-sources-modal lb-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h3>Match Points</h3>
          <button className="close" onClick={onClose}>
            &times;
          </button>
        </header>
        <div className="modal-body">
          <BreakdownHero
            playerId={p.player_id}
            name={p.name}
            teamName={p.team}
            hltvTeamId={p.hltv_team_id}
            roleId={mx.role_index}
            note="maximum"
            stats={[
              ["Rating", p.rating_points],
              ["Win", p.win_points],
              ["Role", mx.role_points],
              ["Booster", mx.booster_points],
            ]}
            total={mx.total}
            decimals={0}
          />
          <div className="stack">
            <h4>Matches</h4>
            <table className="bp-table lb-table">
              <thead>
                <tr>
                  <th className="num tight">#</th>
                  <th>Opponent</th>
                  <th>Result</th>
                  <th className="num">Rating pts</th>
                  <th className="num">Win pts</th>
                  <th className="num">{mx.role_name || "Role"}</th>
                  <th>Booster</th>
                  <th className="num">Total</th>
                </tr>
              </thead>
              <tbody>
                {ms.map((m, i) => (
                  <tr key={m.match_url || i}>
                    <td className="num tight">{i + 1}</td>
                    <td>
                      <div className="table-player-cell">
                        <TeamLogo hltvTeamId={teamIds[m.opponent]} name={m.opponent} size={20} />
                        <span>{m.opponent}</span>
                      </div>
                    </td>
                    <td className={m.result === "WON" ? "pos" : m.result === "LOST" ? "neg" : ""}>{m.result === "WON" ? "Win" : m.result === "LOST" ? "Loss" : m.result || "-"}</td>
                    <td className="num">{signed(m.rating)}</td>
                    <td className="num">{signed(m.team)}</td>
                    <td className="num">{cell(m.role)}</td>
                    <td>
                      {m.booster ? (
                        <span>
                          {m.booster} <span className="muted">+5</span>
                        </span>
                      ) : (
                        <span className="muted">-</span>
                      )}
                    </td>
                    <td className="num">
                      <strong>{signed(matchTotal(m))}</strong>
                    </td>
                  </tr>
                ))}
                {padRows.map((i) => (
                  <tr key={`pad-${i}`} className="pad">
                    <td className="num tight">{ms.length + i + 1}</td>
                    <td>Padding</td>
                    <td />
                    <td className="num">{signed(pad.rating)}</td>
                    <td className="num">{signed(pad.team)}</td>
                    <td className="num">{signed(pad.role)}</td>
                    <td>
                      <span className="muted">-</span>
                    </td>
                    <td className="num">
                      <strong>{signed(padTotal)}</strong>
                    </td>
                  </tr>
                ))}
                <tr className="total">
                  <td className="num tight" />
                  <td>Total</td>
                  <td />
                  <td className="num">{signed(p.rating_points)}</td>
                  <td className="num">{signed(p.win_points)}</td>
                  <td className="num">{signed(mx.role_points)}</td>
                  <td>{signed(mx.booster_points)}</td>
                  <td className="num">
                    <strong>{signed(mx.total)}</strong>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div className="stack">
            <h4>Roles</h4>
            <table className="bp-table lb-table">
              <thead>
                <tr>
                  <th>Role</th>
                  {oppHead}
                  {pad.count > 0 ? <th className="num">Padding</th> : null}
                  <th className="num">Total</th>
                </tr>
              </thead>
              <tbody>
                {(b.roles || []).map((r) => (
                  <tr key={r.role_name} className={r.role_name === mx.role_name ? "change" : ""}>
                    <td>
                      <span className="breakdown-id-role">
                        {r.role_index != null ? <RoleBadge roleId={r.role_index} size={18} /> : null}
                        {r.role_name}
                      </span>
                    </td>
                    {(r.per_match || []).map((v, i) => (
                      <td className="num" key={i}>
                        {cell(v)}
                      </td>
                    ))}
                    {pad.count > 0 ? (
                      <td className="num">
                        {pad.count} × {signed(r.padding)}
                      </td>
                    ) : null}
                    <td className="num">
                      <strong>{r.complete ? signed(r.total) : "-"}</strong>
                      {!r.complete ? (
                        <span className="muted">
                          {" "}
                          {r.seen} of {ms.length}
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="stack">
            <h4>Boosters</h4>
            <table className="bp-table lb-table">
              <thead>
                <tr>
                  <th>Booster</th>
                  {oppHead}
                  <th className="num">Triggers</th>
                </tr>
              </thead>
              <tbody>
                {(b.boosters || []).map((bo) => (
                  <tr key={bo.booster_name}>
                    <td>{bo.booster_name}</td>
                    {(bo.per_match || []).map((v, i) => (
                      <td className="num" key={i}>
                        {v == null ? <span className="muted">-</span> : Number(v) > 0 ? ms[i]?.booster === bo.booster_name ? <strong>+5</strong> : "+5" : <span className="muted">0</span>}
                      </td>
                    ))}
                    <td className="num">{bo.triggers}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

function LiveEventBody({ ev }) {
  const teamLookup = useMemo(() => {
    const m = {};
    Object.entries(ev.teams || {}).forEach(([tid, t]) => {
      m[Number(tid)] = t?.name || String(tid);
    });
    return m;
  }, [ev]);
  const teamRows = useMemo(() => Object.entries(ev.teams || {}).map(([tid, t]) => ({ team_id: Number(tid), hltv_team_id: t?.hltv_team_id, name: t?.name })), [ev]);
  const hltvId = (tid) => ev.teams?.[String(tid)]?.hltv_team_id;
  const dm = (ts) => {
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}`;
  };
  const hhmm = (ts) => {
    const d = new Date(Number(ts) * 1000);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };
  const results = [...(ev.matches || [])].reverse();
  // A result opens the match's scoreboard: the stored row when the nightly
  // import has it, else the page's scoreboards read on demand.
  const [matchRow, setMatchRow] = useState(null);
  const openResult = async (m) => {
    const url = String(m.url || "");
    if (!url) return;
    const base = {
      match_url: url,
      match_id: m.match_id,
      team1: teamLookup[m.a] || String(m.a),
      team2: teamLookup[m.b] || String(m.b),
      score1: m.score_a,
      score2: m.score_b,
      winner: m.winner ? teamLookup[m.winner] : null,
      match_date: m.date,
    };
    setMatchRow(base);
    if (PUBLIC_BUILD) return;
    try {
      const d = await api.get(`/events/hltv-results/match-details?match_url=${encodeURIComponent(url)}`, 60000);
      const stored = d?.stored || {};
      const next = { ...base, ...stored, maps_json: JSON.stringify(Array.isArray(d?.maps) ? d.maps : []) };
      if (!next.player_stats_json && Array.isArray(d?.player_stats) && d.player_stats.length > 0) next.player_stats_json = JSON.stringify(d.player_stats);
      if (!next.map_player_stats_json && d?.map_player_stats && Object.keys(d.map_player_stats).length > 0) next.map_player_stats_json = JSON.stringify(d.map_player_stats);
      setMatchRow((cur) => (cur && cur.match_url === url ? next : cur));
    } catch {
      /* the modal keeps the stored row */
    }
  };
  // A board match opens the same scoreboard as its row: the played, live or
  // upcoming match between those two teams (a Swiss pair meets once).
  const pairKey = (a, b) => [Number(a), Number(b)].sort((x, y) => x - y).join("-");
  const matchByPair = useMemo(() => {
    const out = {};
    [...(ev.matches || []), ...(ev.live_matches || []).map((m) => ({ ...m, score_a: m.maps_a, score_b: m.maps_b }))].forEach((m) => {
      if (m.url && !out[pairKey(m.a, m.b)]) out[pairKey(m.a, m.b)] = m;
    });
    return out;
  }, [ev]);
  const openBoardMatch = (m) => {
    const found = matchByPair[pairKey(m.a, m.b)];
    if (found) openResult(found);
  };
  // The board (Swiss or bracket) already shows every result; the results
  // table only stands in for events without one.
  const boardShown = Boolean((ev.group_format === "swiss" && ev.swiss) || (ev.group_format === "bracket" && ev.bracket));
  const swissShown = Boolean(ev.group_format === "swiss" && ev.swiss);
  const today = new Date().toDateString();
  const upcomingToday = (ev.upcoming_matches || []).filter((m) => m.start_at && new Date(Number(m.start_at) * 1000).toDateString() === today);
  return (
    <div className="stack">
      {ev.group_format === "swiss" && ev.swiss && (
        <div className="card sub">
          <h3>Swiss Stage</h3>
          <SwissBoard walk={ev.swiss} teams={teamRows} teamLookup={teamLookup} picks={ev.swiss.picks || {}} onPick={() => {}} readOnly onOpen={openBoardMatch} />
        </div>
      )}
      {ev.group_format === "bracket" && ev.bracket && (
        <div className="card sub">
          <h3>Bracket</h3>
          <BracketPicker bracket={ev.bracket} teamLookup={teamLookup} picks={ev.bracket_picks || {}} onPick={() => {}} readOnly />
        </div>
      )}
      {Array.isArray(ev.groups) && ev.groups.length > 0 && (
        <div className="live-groups">
          {ev.groups.map((g, i) => (
            <div className="card sub live-group" key={i}>
              <h3>Group {i + 1}</h3>
              <table>
                <thead>
                  <tr>
                    <th>Team</th>
                    <th className="num">W</th>
                    <th className="num">L</th>
                  </tr>
                </thead>
                <tbody>
                  {g.teams.map((tid) => (
                    <tr key={tid}>
                      <td>
                        <div className="table-player-cell">
                          <TeamLogo hltvTeamId={hltvId(tid)} name={teamLookup[tid]} size={22} />
                          <span>{teamLookup[tid] || tid}</span>
                        </div>
                      </td>
                      <td className="num">{g.records?.[String(tid)]?.[0] ?? 0}</td>
                      <td className="num">{g.records?.[String(tid)]?.[1] ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}

      {!swissShown && (ev.live_matches || []).length > 0 && (
        <div className="card sub">
          <h3>In play</h3>
          <table className="live-results clickable compact">
            <thead>
              <tr>
                <th colSpan={3}>Match</th>
                <th className="num">Map</th>
              </tr>
            </thead>
            <tbody>
              {ev.live_matches.map((m) => (
                <tr key={m.match_id} onClick={() => openResult({ ...m, score_a: m.maps_a, score_b: m.maps_b })}>
                  <td className="live-a">
                    <span className="live-side win">
                      <TeamLogo hltvTeamId={hltvId(m.a)} name={teamLookup[m.a]} size={22} />
                      {teamLookup[m.a] || m.a}
                    </span>
                  </td>
                  <td className="live-mid">
                    <span className="live-score live">
                      {m.maps_a ?? 0}–{m.maps_b ?? 0}
                    </span>
                  </td>
                  <td className="live-b">
                    <span className="live-side win">
                      {teamLookup[m.b] || m.b}
                      <TeamLogo hltvTeamId={hltvId(m.b)} name={teamLookup[m.b]} size={22} />
                    </span>
                  </td>
                  <td className="num">
                    {m.map_a ?? "-"}–{m.map_b ?? "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!swissShown && upcomingToday.length > 0 && (
        <div className="card sub">
          <h3>Upcoming today</h3>
          <table className="live-results compact">
            <thead>
              <tr>
                <th>Start</th>
                <th colSpan={3}>Match</th>
              </tr>
            </thead>
            <tbody>
              {upcomingToday.map((m) => (
                <tr key={m.match_id}>
                  <td className="num">{hhmm(m.start_at)}</td>
                  <td className="live-a">
                    <span className="live-side">
                      <TeamLogo hltvTeamId={hltvId(m.a)} name={teamLookup[m.a]} size={22} />
                      {teamLookup[m.a] || m.a}
                    </span>
                  </td>
                  <td className="live-mid">
                    <span className="live-score muted">vs</span>
                  </td>
                  <td className="live-b">
                    <span className="live-side">
                      {teamLookup[m.b] || m.b}
                      <TeamLogo hltvTeamId={hltvId(m.b)} name={teamLookup[m.b]} size={22} />
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {results.length > 0 && !boardShown && (
        <div className="card sub">
          <h3>Results</h3>
          <table className="live-results clickable">
            <thead>
              <tr>
                <th>Round</th>
                <th>Match</th>
                <th className="num">Maps</th>
              </tr>
            </thead>
            <tbody>
              {results.map((m) => {
                const aWon = Number(m.winner) === Number(m.a);
                const bWon = Number(m.winner) === Number(m.b);
                return (
                  <tr key={m.url || `${m.date}-${m.a}-${m.b}`} onClick={() => openResult(m)}>
                    <td>{m.stage || formatDMY(m.date)}</td>
                    <td>
                      <div className="live-match">
                        <span className={`live-side${aWon ? " win" : bWon ? " lose" : ""}`}>
                          <TeamLogo hltvTeamId={hltvId(m.a)} name={teamLookup[m.a]} size={22} />
                          {teamLookup[m.a] || m.a}
                        </span>
                        <span className="live-score">
                          {m.score_a ?? "-"}–{m.score_b ?? "-"}
                        </span>
                        <span className={`live-side right${bWon ? " win" : aWon ? " lose" : ""}`}>
                          {teamLookup[m.b] || m.b}
                          <TeamLogo hltvTeamId={hltvId(m.b)} name={teamLookup[m.b]} size={22} />
                        </span>
                      </div>
                    </td>
                    <td className="num">{m.maps || "-"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {matchRow && <MatchDetailModal row={matchRow} teams={teamRows} players={[]} onClose={() => setMatchRow(null)} />}
    </div>
  );
}

function EventsTab({ refreshData, notify, players, teams = [], onOpenPlayer, onOpenTeam, viewEventId = null, onViewEvent = null }) {
  const [events, setEvents] = useState([]);
  const [activeEventId, setActiveEventId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  // Full kind-detection result per event (kind, group format, playoff size,
  // team count) — drives the Type column and the Formats sub-tab.
  const [eventKinds, setEventKinds] = useState({});
  useEffect(() => {
    let cancelled = false;
    (events || []).forEach((ev) => {
      const id = Number(ev.event_id);
      if (!id || eventKinds[id] !== undefined) return;
      api
        .get(`/events/${id}/kind`, 60000)
        .then((d) => {
          if (!cancelled && d?.kind) setEventKinds((prev) => ({ ...prev, [id]: d }));
        })
        .catch(() => {});
    });
    return () => {
      cancelled = true;
    };
  }, [events]);

  const loadEvents = async () => {
    try {
      const res = await api.get("/events/");
      if (res?.detail) {
        setMessage(String(res.detail));
        return;
      }
      setEvents(res.events || []);
      setActiveEventId(res.active_event_id ?? null);
      setMessage("");
    } catch (e) {
      setMessage(`Could not load events: ${e?.message || "unknown error"}`);
    }
  };

  useEffect(() => {
    loadEvents();
  }, []);

  // Teams & Prices below the table shows the ACTIVE event (operator) or the
  // event the user is viewing (public build).
  const shownEventId = PUBLIC_BUILD ? viewEventId ?? activeEventId : activeEventId;
  const [activeEventDetail, setActiveEventDetail] = useState(null);
  useEffect(() => {
    if (!shownEventId) {
      setActiveEventDetail(null);
      return undefined;
    }
    let cancelled = false;
    api
      .get(`/events/${shownEventId}`)
      .then((d) => {
        if (!cancelled && d && !d.detail) setActiveEventDetail(d);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [shownEventId]);

  // Active event rosters: teams in HLTV-rank order (best first, unranked
  // last), players by price, each joined with their overall rating.
  const eventTeamGroups = useMemo(() => {
    const byTeam = {};
    (activeEventDetail?.players || []).forEach((p) => {
      const teamName = p.team_name || "Unknown Team";
      if (!byTeam[teamName]) byTeam[teamName] = [];
      byTeam[teamName].push(p);
    });
    const teamByName = {};
    (teams || []).forEach((t) => {
      teamByName[String(t.name || "").trim().toLowerCase()] = t;
    });
    const ratingById = {};
    (players || []).forEach((p) => {
      ratingById[Number(p.player_id)] = p.rating;
    });
    const groups = Object.entries(byTeam).map(([teamName, teamPlayers]) => {
      const info = teamByName[teamName.trim().toLowerCase()] || null;
      return {
        teamName,
        info,
        rank: Number(info?.hltv_rank) > 0 ? Number(info.hltv_rank) : null,
        players: teamPlayers
          .slice()
          .sort((a, b) => Number(b.price || 0) - Number(a.price || 0))
          .map((p) => ({ ...p, rating: ratingById[Number(p.player_id)] })),
      };
    });
    groups.sort((a, b) => (a.rank ?? 9999) - (b.rank ?? 9999) || a.teamName.localeCompare(b.teamName));
    return groups;
  }, [activeEventDetail, teams, players]);

  const removeEvent = async (ev) => {
    if (PUBLIC_BUILD) return;
    if (!window.confirm(`Remove ${ev.name || ev.event_id} from the event list?`)) return;
    setBusy(true);
    setMessage("");
    try {
      const res = await api.delete(`/events/${Number(ev.event_id)}`);
      if (res?.detail) {
        setMessage(String(res.detail));
        return;
      }
      await refreshData();
    } catch (e) {
      setMessage(`Could not remove: ${e?.message || "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  const activateEvent = async (targetEventId) => {
    if (PUBLIC_BUILD) return;
    setBusy(true);
    setMessage("");
    try {
      const res = await api.post("/events/activate", { event_id: Number(targetEventId) });
      if (res?.detail) {
        setMessage(String(res.detail));
        return;
      }
      setActiveEventId(res.active_event_id);
      await refreshData();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <Section title="Events">
        <div className="stack">
          {events.length > 0 && (
            <div className="card sub">
              <h4>Imported Events</h4>
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Fantasy Event</th>
                    <th>Type</th>
                    <th>Teams</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((ev) => (
                    <tr key={ev.event_id}>
                      <td>{ev.name || "-"}</td>
                      <td>{ev.event_id}</td>
                      <td>{describeEventFormat(eventKinds[ev.event_id])?.short || "..."}</td>
                      <td>{ev.team_count ?? 0}</td>
                      <td>
                        {ev.valuation ? (
                          <span className={`valuation-badge ${eventStatus(ev.valuation).cls}`} title={PUBLIC_BUILD ? undefined : ev.valuation.label}>
                            {eventStatus(ev.valuation).label}
                          </span>
                        ) : (
                          <span className="muted">-</span>
                        )}
                      </td>
                      <td>
                        {PUBLIC_BUILD ? (
                          <div className="actions" style={{ marginTop: 0 }}>
                            <button
                              className={shownEventId === ev.event_id ? "primary" : "secondary"}
                              onClick={() => onViewEvent && onViewEvent(ev.event_id === activeEventId ? null : ev.event_id)}
                              disabled={shownEventId === ev.event_id}
                            >
                              {shownEventId === ev.event_id ? "Active" : "Set Active"}
                            </button>
                          </div>
                        ) : (
                          <div className="actions" style={{ marginTop: 0 }}>
                            <button
                              className={activeEventId === ev.event_id ? "primary" : "secondary"}
                              onClick={() => activateEvent(ev.event_id)}
                              disabled={busy || activeEventId === ev.event_id}
                            >
                              {activeEventId === ev.event_id ? "Active" : "Set Active"}
                            </button>
                            <button
                              className="danger"
                              onClick={() => removeEvent(ev)}
                              disabled={busy || activeEventId === ev.event_id}
                            >
                              Remove
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {activeEventDetail && (
            <div className="card sub">
              <h4>
                {events.find((e) => e.event_id === activeEventDetail.event_id)?.name ||
                  `Event ${activeEventDetail.event_id}`}{" "}
                — Teams &amp; Prices
              </h4>
              <div className="event-team-rows">
                {eventTeamGroups.map((g) => (
                  <div className="event-team-row" key={g.teamName}>
                    <div
                      className={`event-team-head ${g.info?.team_id && onOpenTeam ? "clickable" : ""}`}
                      onClick={() => g.info?.team_id && onOpenTeam && onOpenTeam(g.info.team_id)}
                    >
                      <TeamLogo hltvTeamId={g.info?.hltv_team_id} name={g.teamName} size={42} />
                      <div className="event-team-headtext">
                        <span className="event-team-name">{g.teamName}</span>
                        {g.rank && <span className="event-team-rank">HLTV #{g.rank}</span>}
                      </div>
                    </div>
                    <div className="event-team-players">
                      {g.players.map((p) => (
                        <div
                          className={`event-player-card ${onOpenPlayer ? "clickable" : ""}`}
                          key={`${g.teamName}-${p.player_id}`}
                          onClick={() => onOpenPlayer && onOpenPlayer(p.player_id)}
                        >
                          <PlayerPhoto playerId={p.player_id} name={p.player_name} size={56} />
                          <div className="event-player-name">{p.player_name || `Player ${p.player_id}`}</div>
                          <div className="event-player-stats">
                            <div className="event-player-stat">
                              <div className="event-player-rating">
                                {Number.isFinite(Number(p.rating)) ? Number(p.rating).toFixed(2) : "-"}
                              </div>
                              <div className="event-player-stat-label">Rating</div>
                            </div>
                            <div className="event-player-stat">
                              <div className="event-player-price">${Number(p.price || 0).toLocaleString()}</div>
                              <div className="event-player-stat-label">Price</div>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {message && <p className="muted">{message}</p>}
        </div>
      </Section>
    </div>
  );
}

function MatchesDataPanel({ notify, teams = [], players = [], onOpenPlayer, onOpenTeam }) {
  const [recentResults, setRecentResults] = useState([]);
  const [recentResultsLoading, setRecentResultsLoading] = useState(false);
  const [recentResultsError, setRecentResultsError] = useState("");
  const [recentResultsOffset, setRecentResultsOffset] = useState(0);
  const [selectedMatchUrl, setSelectedMatchUrl] = useState("");
  const [selectedMatchRow, setSelectedMatchRow] = useState(null);
  const [showMatchModal, setShowMatchModal] = useState(false);
  const initialLoadRef = useRef(false);
  const [matchSearch, setMatchSearch] = useState("");
  const [matchSort, setMatchSort] = useState("date_desc");
  const [matchRankValue, setMatchRankValue] = useState("");
  const [matchRankScope, setMatchRankScope] = useState("both");
  const [matchRankDir, setMatchRankDir] = useState("within");
  const loadStoredRecentResults = async (offset = 0, searchOverride = null, sortOverride = null, rankOverride = {}) => {
    const safeOffset = Math.max(0, Number(offset) || 0);
    const q = searchOverride !== null ? searchOverride : matchSearch;
    const s = sortOverride !== null ? sortOverride : matchSort;
    const rv = rankOverride.value !== undefined ? rankOverride.value : matchRankValue;
    const rs = rankOverride.scope !== undefined ? rankOverride.scope : matchRankScope;
    const rd = rankOverride.dir !== undefined ? rankOverride.dir : matchRankDir;
    const rankParams =
      Number(rv) > 0 ? `&vrs_rank=${Number(rv)}&vrs_scope=${encodeURIComponent(rs)}&vrs_dir=${encodeURIComponent(rd)}` : "";
    setRecentResultsLoading(true);
    setRecentResultsError("");
    try {
      const res = await api.get(
        `/events/hltv-results?limit=100&offset=${safeOffset}&search=${encodeURIComponent(q)}&sort=${encodeURIComponent(s)}${rankParams}`
      );
      if (res?.detail) {
        setRecentResults([]);
        setRecentResultsError(String(res.detail));
      } else {
        setRecentResults(Array.isArray(res?.results) ? res.results : []);
        setRecentResultsOffset(safeOffset);
      }
    } catch (e) {
      setRecentResults([]);
      setRecentResultsError("Failed to load stored HLTV results.");
    } finally {
      setRecentResultsLoading(false);
    }
  };
  const changeMatchSearch = (value) => {
    setMatchSearch(value);
    loadStoredRecentResults(0, value, null);
  };
  const changeMatchSort = (value) => {
    setMatchSort(value);
    loadStoredRecentResults(0, null, value);
  };
  const changeMatchRankValue = (value) => {
    setMatchRankValue(value);
    loadStoredRecentResults(0, null, null, { value });
  };
  const changeMatchRankScope = (value) => {
    setMatchRankScope(value);
    loadStoredRecentResults(0, null, null, { scope: value });
  };
  const changeMatchRankDir = (value) => {
    setMatchRankDir(value);
    loadStoredRecentResults(0, null, null, { dir: value });
  };
  const hltvIdByTeamName = useMemo(() => {
    const m = {};
    (teams || []).forEach((t) => {
      if (t.hltv_team_id) m[String(t.name || "").trim().toLowerCase()] = Number(t.hltv_team_id);
    });
    return m;
  }, [teams]);
  const logoForTeamName = (name) => hltvIdByTeamName[String(name || "").trim().toLowerCase()];

  const openMatchModal = async (row) => {
    const url = String(row?.match_url || "").trim();
    if (!url) return;
    setSelectedMatchUrl(url);
    setSelectedMatchRow(row || null);
    setShowMatchModal(true);
    // The live detail refresh fetches the match page from HLTV; the public
    // build only reads what the server has stored (the row already carries it).
    if (PUBLIC_BUILD) return;
    try {
      const details = await api.get(`/events/hltv-results/match-details?match_url=${encodeURIComponent(url)}`);
      const maps = Array.isArray(details?.maps) ? details.maps : [];
      const stored = details?.stored || {};
      const nextRow = {
        ...(row || {}),
        ...(stored || {}),
        maps_json: JSON.stringify(maps),
      };
      setSelectedMatchRow(nextRow);
      setRecentResults((prev) => prev.map((item) => (String(item?.match_url || "") === url ? nextRow : item)));
    } catch {
      // Keep the stored row visible if live detail refresh fails.
    }
  };

  useEffect(() => {
    if (initialLoadRef.current) return;
    initialLoadRef.current = true;
    loadStoredRecentResults(0);
  }, []);

  return (
    <div className="stack">
      <div className="grid two">
        <Input label="Search Matches" value={matchSearch} onChange={changeMatchSearch} placeholder="Team or day/month/year" className="search-field" />
        <div className="match-rank-filter">
          <Input label="VRS Rank" value={matchRankValue} onChange={changeMatchRankValue} placeholder="e.g. 50" />
          <Select
            label="Direction"
            value={matchRankDir}
            onChange={changeMatchRankDir}
            options={[
              { value: "within", label: "Inside top N" },
              { value: "outside", label: "Outside top N" },
            ]}
          />
          <Select
            label="Teams"
            value={matchRankScope}
            onChange={changeMatchRankScope}
            options={[
              { value: "both", label: "Both teams" },
              { value: "either", label: "At least one team" },
            ]}
          />
        </div>
      </div>
      <div className="actions" style={{ marginTop: 0 }}>
        <button
          className="secondary"
          onClick={() => loadStoredRecentResults(Math.max(0, recentResultsOffset - 100))}
          disabled={recentResultsLoading || recentResultsOffset <= 0}
        >
          Previous
        </button>
        <button
          className="secondary"
          onClick={() => loadStoredRecentResults(recentResultsOffset + 100)}
          disabled={recentResultsLoading || recentResults.length < 100}
        >
          Next
        </button>
      </div>
      {recentResultsError && <p className="error">{recentResultsError}</p>}
      {!recentResultsLoading && !recentResultsError && recentResults.length === 0 && <p className="muted">No matches found.</p>}
      {recentResults.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Team A</th>
              <th>Score</th>
              <th>Team B</th>
              <SortHeader sortValue={matchSort} asc="date_asc" desc="date_desc" defaultDirection="desc" onChange={changeMatchSort}>Date</SortHeader>
            </tr>
          </thead>
          <tbody>
            {recentResults.map((r, idx) => {
              const s1 = Number(r?.score1);
              const s2 = Number(r?.score2);
              const haveScore = Number.isFinite(s1) && Number.isFinite(s2);
              return (
                <tr
                  key={`hltv-res-${idx}-${r?.match_url || ""}`}
                  className={`row-link ${String(r?.match_url || "") === selectedMatchUrl ? "row-active" : ""}`}
                  onClick={() => openMatchModal(r)}
                >
                  <td><TeamLogo hltvTeamId={logoForTeamName(r?.team1)} name={r?.team1} size={22} />{r?.team1 || "-"}</td>
                  <td className="score-cell">
                    {haveScore ? (
                      <>
                        <span className={s1 > s2 ? "score-win" : s1 < s2 ? "score-loss" : ""}>{s1}</span>
                        {" - "}
                        <span className={s2 > s1 ? "score-win" : s2 < s1 ? "score-loss" : ""}>{s2}</span>
                      </>
                    ) : (
                      "-"
                    )}
                  </td>
                  <td><TeamLogo hltvTeamId={logoForTeamName(r?.team2)} name={r?.team2} size={22} />{r?.team2 || "-"}</td>
                  <td>{formatDMY(r?.match_date)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div className="actions" style={{ marginTop: 0 }}>
        <button
          className="secondary"
          onClick={() => loadStoredRecentResults(Math.max(0, recentResultsOffset - 100))}
          disabled={recentResultsLoading || recentResultsOffset <= 0}
        >
          Previous
        </button>
        <button
          className="secondary"
          onClick={() => loadStoredRecentResults(recentResultsOffset + 100)}
          disabled={recentResultsLoading || recentResults.length < 100}
        >
          Next
        </button>
      </div>
      {showMatchModal && selectedMatchRow && (
        <MatchDetailModal
          row={selectedMatchRow}
          teams={teams}
          players={players}
          onClose={() => setShowMatchModal(false)}
          onOpenPlayer={
            onOpenPlayer
              ? (pid) => {
                  setShowMatchModal(false);
                  onOpenPlayer(pid);
                }
              : undefined
          }
          onOpenTeam={
            onOpenTeam
              ? (tid) => {
                  setShowMatchModal(false);
                  onOpenTeam(tid);
                }
              : undefined
          }
        />
      )}
    </div>
  );
}

function MatchDetailModal({ row: listRow, onClose, teams = [], players = [], onOpenPlayer, onOpenTeam }) {
  // Stored-match detail view shared by the Matches browser and the player
  // card's Recent Matches list. The Matches browser's list rows come without
  // the detail blobs (they are ~8 KB a row and only this modal reads them),
  // so fetch the full stored row on open when they are missing.
  const [fullRow, setFullRow] = useState(null);
  useEffect(() => {
    setFullRow(null);
    if (!listRow?.match_url || listRow.maps_json !== undefined) return;
    let cancelled = false;
    api
      .get(`/events/hltv-results/stored?match_url=${encodeURIComponent(listRow.match_url)}`, 30000)
      .then((data) => {
        if (!cancelled && data && !data.detail) setFullRow(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [listRow?.match_url]);
  const row = fullRow ? { ...listRow, ...fullRow } : listRow;
  let maps = [];
  try {
    const parsed = JSON.parse(String(row?.maps_json || "[]"));
    if (Array.isArray(parsed)) maps = parsed;
  } catch {}
  let playerStats = [];
  try {
    const parsed = JSON.parse(String(row?.player_stats_json || "[]"));
    if (Array.isArray(parsed)) playerStats = parsed;
  } catch {}
  // Per-map scoreboards ({map: [entries]}), filled by the map-scoreboards
  // backfill; "{}" means the page has no per-map tabs.
  let mapPlayerStats = {};
  try {
    const parsed = JSON.parse(String(row?.map_player_stats_json || "{}"));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) mapPlayerStats = parsed;
  } catch {}
  const sbMapNames = Object.keys(mapPlayerStats).filter(
    (k) => Array.isArray(mapPlayerStats[k]) && mapPlayerStats[k].length > 0
  );
  const [sbMap, setSbMap] = useState("all");
  useEffect(() => setSbMap("all"), [row?.match_url]);
  const activeStats = sbMap !== "all" && mapPlayerStats[sbMap] ? mapPlayerStats[sbMap] : playerStats;
  const hltvIdByName = useMemo(() => {
    const m = {};
    (teams || []).forEach((t) => {
      if (t.hltv_team_id) m[String(t.name || "").trim().toLowerCase()] = Number(t.hltv_team_id);
    });
    return m;
  }, [teams]);
  const logoIdFor = (name) => hltvIdByName[String(name || "").trim().toLowerCase()];
  // Click-through to stored player/team cards — only for ids we actually have.
  const knownPlayerIds = useMemo(
    () => new Set((players || []).map((p) => Number(p.player_id))),
    [players]
  );
  const teamIdByName = useMemo(() => {
    const m = {};
    (teams || []).forEach((t) => {
      if (t.team_id) m[String(t.name || "").trim().toLowerCase()] = Number(t.team_id);
    });
    return m;
  }, [teams]);
  const playerLinkable = (pid) => Boolean(onOpenPlayer) && knownPlayerIds.has(Number(pid));
  const teamIdFor = (name) => teamIdByName[String(name || "").trim().toLowerCase()] || 0;
  const teamLinkable = (name) => Boolean(onOpenTeam) && teamIdFor(name) > 0;
  // Group the selected scoreboard by team, team1's block first, best rating on top.
  const groups = new Map();
  activeStats.forEach((p) => {
    const key = String(p?.team || "").trim() || "Unknown";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  });
  const t1Name = String(row?.team1 || "").trim().toLowerCase();
  const scoreboards = Array.from(groups.entries()).map(([team, entries]) => ({
    team,
    entries: entries.slice().sort((a, b) => (Number(b?.rating) || 0) - (Number(a?.rating) || 0)),
  }));
  scoreboards.sort(
    (a, b) =>
      (a.team.trim().toLowerCase() === t1Name ? 0 : 1) -
      (b.team.trim().toLowerCase() === t1Name ? 0 : 1)
  );
  const ratingClass = (v) => {
    if (!Number.isFinite(v)) return "";
    if (v >= 1.05) return "score-win";
    if (v < 0.95) return "score-loss";
    return "";
  };
  // One rank per team next to its name: HLTV rank when both teams have one,
  // otherwise VRS ranks (many lower-tier matches never have an HLTV rank).
  const toRank = (v) => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  const useHltvRanks = toRank(row?.hltv_rank_1) !== null && toRank(row?.hltv_rank_2) !== null;
  const rankLabel = useHltvRanks ? "HLTV" : "VRS";
  const rank1 = useHltvRanks ? toRank(row?.hltv_rank_1) : toRank(row?.vrs_rank_1);
  const rank2 = useHltvRanks ? toRank(row?.hltv_rank_2) : toRank(row?.vrs_rank_2);
  const s1 = Number(row?.score1);
  const s2 = Number(row?.score2);
  const haveScore = Number.isFinite(s1) && Number.isFinite(s2);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal match-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header match-hero-header">
          <button className="close" onClick={onClose}>
            &times;
          </button>
          <div className="match-hero">
            <div
              className={`match-hero-team away-from-center ${teamLinkable(row?.team1) ? "clickable" : ""}`}
              onClick={() => teamLinkable(row?.team1) && onOpenTeam(teamIdFor(row?.team1))}
            >
              <span className="match-team-block">
                <span className="match-team-name">{row?.team1 || "-"}</span>
                {rank1 !== null && <span className="match-team-rank">{rankLabel} #{rank1}</span>}
              </span>
              <TeamLogo hltvTeamId={logoIdFor(row?.team1)} name={row?.team1} size={58} />
            </div>
            <div className="match-hero-score">
              {haveScore ? (
                <>
                  <span className={s1 > s2 ? "score-win" : s1 < s2 ? "score-loss" : ""}>{s1}</span>
                  <span className="match-hero-dash">:</span>
                  <span className={s2 > s1 ? "score-win" : s2 < s1 ? "score-loss" : ""}>{s2}</span>
                </>
              ) : (
                <span className="match-hero-dash">vs</span>
              )}
            </div>
            <div
              className={`match-hero-team ${teamLinkable(row?.team2) ? "clickable" : ""}`}
              onClick={() => teamLinkable(row?.team2) && onOpenTeam(teamIdFor(row?.team2))}
            >
              <TeamLogo hltvTeamId={logoIdFor(row?.team2)} name={row?.team2} size={58} />
              <span className="match-team-block">
                <span className="match-team-name">{row?.team2 || "-"}</span>
                {rank2 !== null && <span className="match-team-rank">{rankLabel} #{rank2}</span>}
              </span>
            </div>
          </div>
          <p className="match-hero-sub">
            {formatDMY(row?.match_date)}
            {row?.event || row?.event_name ? ` | ${row?.event || row?.event_name}` : ""}
          </p>
        </header>
        <div className="modal-body">
          <h4>Maps Played</h4>
          {maps.length === 0 ? (
            <p className="muted">No map details stored for this match.</p>
          ) : (
            <div className="match-maps">
              {maps.map((m, i) => {
                const ms1 = Number(m?.score1);
                const ms2 = Number(m?.score2);
                const haveMapScore = Number.isFinite(ms1) && Number.isFinite(ms2);
                const halves = Array.isArray(m?.halves) ? m.halves : [];
                return (
                  <div
                    className="match-map-row"
                    key={`map-${i}-${m?.map || ""}`}
                    style={{ "--map-color": MAP_BAR_COLORS[m?.map] || "#3a4452" }}
                  >
                    <span className="match-map-name">{m?.map || "-"}</span>
                    <span className="match-map-score">
                      <TeamLogo hltvTeamId={logoIdFor(row?.team1)} name={row?.team1} size={18} />
                      <span className={`match-map-num ${haveMapScore ? (ms1 > ms2 ? "score-win" : ms1 < ms2 ? "score-loss" : "") : ""}`}>
                        {m?.score1 ?? "-"}
                      </span>
                      <span className="match-map-score-dash">-</span>
                      <span className={`match-map-num away ${haveMapScore ? (ms2 > ms1 ? "score-win" : ms2 < ms1 ? "score-loss" : "") : ""}`}>
                        {m?.score2 ?? "-"}
                      </span>
                      <TeamLogo hltvTeamId={logoIdFor(row?.team2)} name={row?.team2} size={18} />
                    </span>
                    <span className="match-map-halves">
                      {halves.map((h, j) => (
                        <span key={`half-${j}`} className="match-map-half">
                          {j > 0 && <span className="match-map-half-sep">·</span>}
                          <span className={`match-map-half-num side-${String(h?.side1 || "").toLowerCase()}`}>{h?.score1 ?? "-"}</span>
                          <span className="match-map-half-colon">:</span>
                          <span className={`match-map-half-num side-${String(h?.side2 || "").toLowerCase()}`}>{h?.score2 ?? "-"}</span>
                        </span>
                      ))}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          <h4>Scoreboard</h4>
          {sbMapNames.length > 0 && (
            <div className="sb-map-tabs">
              <button
                className={`sb-map-tab ${sbMap === "all" ? "active" : ""}`}
                onClick={() => setSbMap("all")}
              >
                All maps
              </button>
              {sbMapNames.map((m) => (
                <button
                  key={`sbt-${m}`}
                  className={`sb-map-tab ${sbMap === m ? "active" : ""}`}
                  style={{ "--map-color": MAP_BAR_COLORS[m] || "#ff6b1a" }}
                  onClick={() => setSbMap(m)}
                >
                  {m}
                </button>
              ))}
            </div>
          )}
          {scoreboards.length === 0 ? (
            <p className="muted">No player stats stored for this match yet.</p>
          ) : (
            scoreboards.map((sb) => (
              <div className="scoreboard-card" key={`sb-${sb.team}`}>
                <table className="scoreboard-table">
                  <thead>
                    <tr>
                      <th
                        className={`scoreboard-team ${teamLinkable(sb.team) ? "clickable" : ""}`}
                        onClick={() => teamLinkable(sb.team) && onOpenTeam(teamIdFor(sb.team))}
                      >
                        <TeamLogo hltvTeamId={logoIdFor(sb.team)} name={sb.team} size={24} />
                        <span>{sb.team}</span>
                      </th>
                      <th className="num">K-D</th>
                      <th className="num">+/-</th>
                      <th className="num">ADR</th>
                      <th className="num">KAST</th>
                      <th className="num">Rating</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sb.entries.map((p, i) => {
                      const kills = Number(p?.kills);
                      const deaths = Number(p?.deaths);
                      const pm = Number(p?.plus_minus);
                      const adr = Number(p?.adr);
                      const kast = Number(p?.kast);
                      const rating = Number(p?.rating);
                      return (
                        <tr key={`sb-p-${p?.player_id || i}`}>
                          <td className="scoreboard-player">
                            <PlayerPhoto playerId={p?.player_id} name={p?.player} size={24} />
                            {playerLinkable(p?.player_id) ? (
                              <button
                                className="sb-player-link"
                                onClick={() => onOpenPlayer(Number(p.player_id))}
                              >
                                {p?.player || "-"}
                              </button>
                            ) : (
                              <span>{p?.player || "-"}</span>
                            )}
                          </td>
                          <td className="num">
                            {Number.isFinite(kills) && Number.isFinite(deaths)
                              ? `${kills} - ${deaths}`
                              : "-"}
                          </td>
                          <td
                            className={`num ${
                              Number.isFinite(pm)
                                ? pm > 0
                                  ? "score-win"
                                  : pm < 0
                                  ? "score-loss"
                                  : ""
                                : ""
                            }`}
                          >
                            {Number.isFinite(pm) ? (pm > 0 ? `+${pm}` : `${pm}`) : "-"}
                          </td>
                          <td className="num">{Number.isFinite(adr) ? adr.toFixed(1) : "-"}</td>
                          <td className="num">{Number.isFinite(kast) ? `${kast.toFixed(1)}%` : "-"}</td>
                          <td className={`num scoreboard-rating ${ratingClass(rating)}`}>
                            {Number.isFinite(rating) ? rating.toFixed(2) : "-"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ))
          )}
        </div>
        <div className="actions">
          <button className="secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function DatabaseTab({ players, teams, loading, error, refresh, notify, openPlayerId, onOpenPlayerHandled, openTeamId, onOpenTeamHandled, mapStats, mapStatsModalRefreshRef }) {
  const [dbTab, setDbTab] = useState("players");
  const [playerSearch, setPlayerSearch] = useState("");
  const [playerSort, setPlayerSort] = useState("name_asc");
  // "event": players with a fantasy price (they were in an event); "all" adds
  // the roster players the nightly team-data task creates from HLTV team pages.
  const [teamSearch, setTeamSearch] = useState("");
  const [teamSort, setTeamSort] = useState("name_asc");
  const [selectedPlayer, setSelectedPlayer] = useState(null);
  const [showPlayerModal, setShowPlayerModal] = useState(false);
  const [playerTab, setPlayerTab] = useState("info");
  const [playerForm, setPlayerForm] = useState({
    player_id: "",
    name: "",
    rating: "",
  });
  const [playerCurve, setPlayerCurve] = useState(null);
  const [playerCurveLoading, setPlayerCurveLoading] = useState(false);
  const [playerCurveError, setPlayerCurveError] = useState("");

  const [selectedTeam, setSelectedTeam] = useState(null);
  const [showTeamModal, setShowTeamModal] = useState(false);
  const [teamForm, setTeamForm] = useState({
    team_id: "",
    hltv_team_id: "",
    name: "",
    hltv_rank: "",
    hltv_points: "",
    vrs_rank: "",
    vrs_points: "",
    win_rate: "",
    map_stats_json: "",
    map_stats_imported_at: "",
    map_stats_source_url: "",
    p1: "",
    p2: "",
    p3: "",
    p4: "",
    p5: "",
    formerly: [],
    renamed_to: null,
  });
  const playerLookup = useMemo(() => {
    const m = {};
    players.forEach((p) => (m[p.player_id] = p.name));
    return m;
  }, [players]);
  // A player can sit on several stored rosters (old event teams are kept);
  // only their CURRENT team is shown: the roster of the most recent EVENT they
  // appeared in (backend /events/player-latest-teams). Team-row creation order
  // is no signal — a player can move to a team whose row is older than their
  // previous team's. Players never priced into an event fall back to any
  // stored roster containing them. One-element arrays keep join()/[0]
  // consumers working unchanged.
  const [eventLatestTeams, setEventLatestTeams] = useState({});
  useEffect(() => {
    let cancelled = false;
    api
      .get("/events/player-latest-teams", 30000)
      .then((data) => {
        if (!cancelled) setEventLatestTeams(data?.teams || {});
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [players]);
  const latestTeamByPlayer = useMemo(() => {
    const teamIdByName = {};
    teams.forEach((t) => {
      teamIdByName[String(t.name || "").trim().toLowerCase()] = Number(t.team_id) || 0;
    });
    const best = {};
    Object.entries(eventLatestTeams).forEach(([pid, row]) => {
      const name = String(row?.team_name || "").trim();
      if (!name) return;
      best[pid] = { team_id: teamIdByName[name.toLowerCase()] || 0, team_name: name };
    });
    // Fallback for players outside any event's rosters.
    teams.forEach((t) => {
      const tid = Number(t.team_id) || 0;
      [t.player1_id, t.player2_id, t.player3_id, t.player4_id, t.player5_id].filter(Boolean).forEach((pid) => {
        if (!best[pid]) best[pid] = { team_id: tid, team_name: t.name || `Team ${tid}` };
      });
    });
    return best;
  }, [teams, eventLatestTeams]);
  const playerTeamLookup = useMemo(() => {
    const m = {};
    Object.entries(latestTeamByPlayer).forEach(([pid, team]) => {
      m[pid] = [team.team_name];
    });
    return m;
  }, [latestTeamByPlayer]);
  const playerTeamLinks = useMemo(() => {
    const m = {};
    Object.entries(latestTeamByPlayer).forEach(([pid, team]) => {
      m[pid] = [team];
    });
    return m;
  }, [latestTeamByPlayer]);

  const boosterNames = {
    0: "Best Pistol Round",
    1: "Bottom of scoreboard",
    2: "Clutch",
    3: "Top of scoreboard",
    4: "Avenger",
    5: "Bait",
    6: "Rambo",
    7: "Flash",
    8: "Mister consistent",
    9: "Kobe",
    10: "Saver",
    11: "Assist",
    12: "Aim bot",
    13: "Quad",
    14: "Carry",
    15: "Cannon fodder",
    16: "Farmer",
    17: "Hellcase",
  };

  const roleNames = {
    0: "Main AWP",
    1: "Support",
    2: "Attacker",
    3: "Leader",
    4: "Stathunter",
    5: "Entry Fragger",
    6: "Camper",
    7: "Defender",
    8: "HS Machine",
    9: "Noob",
    10: "Multi Fragger",
    11: "Eco Friendly",
  };

  const [boosterForm, setBoosterForm] = useState({});
  const [roleForm, setRoleForm] = useState({});
  const boosterAverages = useMemo(() => {
    const sums = {};
    const counts = {};
    for (const p of players || []) {
      let parsed = {};
      try {
        parsed = p.boosters_json ? JSON.parse(p.boosters_json) : {};
      } catch {
        parsed = {};
      }
      for (const key of Object.keys(boosterNames)) {
        const v = Number(parsed?.[key]);
        if (!Number.isFinite(v)) continue;
        sums[key] = (sums[key] || 0) + v;
        counts[key] = (counts[key] || 0) + 1;
      }
    }
    const avg = {};
    for (const key of Object.keys(boosterNames)) {
      if (counts[key] > 0) avg[key] = sums[key] / counts[key];
    }
    return avg;
  }, [players]);

  // Hero enrichment: the selected player's current team (with HLTV id for the
  // logo), headline chips, and their team's recent match form.
  const heroTeam = useMemo(() => {
    const link = (playerTeamLinks[playerForm.player_id] || [])[0];
    if (!link) return null;
    const t = teams.find((x) => Number(x.team_id) === Number(link.team_id));
    return { ...link, hltv_team_id: t?.hltv_team_id };
  }, [playerTeamLinks, playerForm.player_id, teams]);
  const heroExtras = useMemo(() => {
    const pid = Number(selectedPlayer);
    const p = players.find((x) => Number(x.player_id) === pid);
    if (!p) return null;
    const normalizeRate = (v) => {
      const n = Number(v);
      if (!Number.isFinite(n)) return null;
      if (n < 0) return 0;
      return n > 1 ? n / 100 : n;
    };
    const rated = players.filter((x) => Number(x.rating) > 0);
    const rank = 1 + rated.filter((x) => Number(x.rating) > Number(p.rating || 0)).length;
    let bestRole = null;
    Object.entries(roleNames).forEach(([id, label]) => {
      const maj = normalizeRate(roleForm[id]?.major ?? roleForm[id]?.major_win_pct);
      const min = normalizeRate(roleForm[id]?.minor ?? roleForm[id]?.minor_win_pct);
      if (maj === null || min === null) return;
      const expected = 5 * maj + 2 * min - 2 * (1 - maj - min);
      if (!bestRole || expected > bestRole.expected) bestRole = { id: Number(id), label, expected };
    });
    // Best booster = the one that beats the field-wide average by the most —
    // a high raw rate on a booster everyone triggers isn't an edge.
    let bestBooster = null;
    Object.entries(boosterNames).forEach(([id, label]) => {
      const v = Number(boosterForm[id]);
      const avg = Number(boosterAverages[id]);
      if (!Number.isFinite(v) || !Number.isFinite(avg)) return;
      const delta = v - avg;
      if (!bestBooster || delta > bestBooster.delta) bestBooster = { id: Number(id), label, value: v, delta };
    });
    return { price: Number(p.price || 0), rank, ratedCount: rated.length, bestRole, bestBooster };
  }, [players, selectedPlayer, roleForm, boosterForm, boosterAverages]);
  // Recent form: the player's last matches with their per-match rating,
  // extracted server-side from the archived match-page scoreboards.
  const [playerRecentForm, setPlayerRecentForm] = useState([]);
  const [playerMatchDetail, setPlayerMatchDetail] = useState(null);
  const openStoredMatch = async (matchUrl) => {
    if (!matchUrl) return;
    try {
      const row = await api.get(`/events/hltv-results/stored?match_url=${encodeURIComponent(matchUrl)}`, 15000);
      if (row && row.team1) setPlayerMatchDetail(row);
      else notify("No stored details for that match.");
    } catch (e) {
      notify(`Could not load match: ${e?.message || "unknown error"}`);
    }
  };
  useEffect(() => {
    const pid = Number(selectedPlayer);
    if (!showPlayerModal || !Number.isFinite(pid) || pid <= 0) {
      setPlayerRecentForm([]);
      return;
    }
    let cancelled = false;
    api
      .get(`/players/${pid}/recent-form?limit=5`, 30000)
      .then((res) => {
        if (!cancelled) setPlayerRecentForm(Array.isArray(res?.matches) ? res.matches : []);
      })
      .catch(() => {
        if (!cancelled) setPlayerRecentForm([]);
      });
    return () => {
      cancelled = true;
    };
  }, [showPlayerModal, selectedPlayer]);

  useEffect(() => {
    if (selectedPlayer) {
      const p = players.find((x) => x.player_id === selectedPlayer);
      if (p) {
        let boostersObj = {};
        let rolesObj = {};
        try {
          boostersObj = p.boosters_json ? JSON.parse(p.boosters_json) : {};
        } catch {}
        try {
          rolesObj = p.roles_json ? JSON.parse(p.roles_json) : {};
        } catch {}

        setPlayerForm({
          player_id: p.player_id,
          name: p.name || "",
          rating: p.rating ?? "",
        });
        setBoosterForm(boostersObj || {});
        setRoleForm(rolesObj || {});
      }
    }
  }, [selectedPlayer, players]);

  useEffect(() => {
    if (!openPlayerId) return;
    const pid = Number(openPlayerId);
    if (!Number.isFinite(pid) || pid <= 0) {
      if (onOpenPlayerHandled) onOpenPlayerHandled();
      return;
    }
    setDbTab("players");
    setSelectedPlayer(pid);
    setShowPlayerModal(true);
    setPlayerTab("info");
    if (onOpenPlayerHandled) onOpenPlayerHandled();
  }, [openPlayerId, onOpenPlayerHandled]);

  useEffect(() => {
    if (!openTeamId) return;
    const tid = Number(openTeamId);
    if (Number.isFinite(tid) && tid > 0) {
      setDbTab("teams");
      setSelectedTeam(tid);
      setShowTeamModal(true);
    }
    if (onOpenTeamHandled) onOpenTeamHandled();
  }, [openTeamId, onOpenTeamHandled]);

  useEffect(() => {
    const pid = Number(selectedPlayer);
    if (playerTab !== "topxGraph" || !showPlayerModal || !Number.isFinite(pid) || pid <= 0) {
      if (!showPlayerModal || playerTab !== "topxGraph") {
        setPlayerCurveError("");
      }
      return;
    }
    let cancelled = false;
    const run = async () => {
      setPlayerCurveLoading(true);
      setPlayerCurveError("");
      try {
        const res = await api.get(`/players/${pid}/rating-curve`);
        if (!cancelled) setPlayerCurve(res || null);
      } catch (e) {
        if (!cancelled) {
          setPlayerCurve(null);
          setPlayerCurveError("Failed to load Top-X graph.");
        }
      } finally {
        if (!cancelled) setPlayerCurveLoading(false);
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [playerTab, selectedPlayer, showPlayerModal]);

  useEffect(() => {
    if (selectedTeam) {
      const t = teams.find((x) => x.team_id === selectedTeam);
      if (t) {
        setTeamForm({
          team_id: t.team_id,
          hltv_team_id: t.hltv_team_id ?? "",
          name: t.name || "",
          hltv_rank: t.hltv_rank || "",
          hltv_points: t.hltv_points || "",
          vrs_rank: t.vrs_rank || "",
          vrs_points: t.vrs_points || "",
          win_rate: t.win_rate || "",
          map_stats_json: t.map_stats_json || "",
          map_stats_imported_at: t.map_stats_imported_at || "",
          map_stats_source_url: t.map_stats_source_url || "",
          p1: t.player1_id ? String(t.player1_id) : "",
          p2: t.player2_id ? String(t.player2_id) : "",
          p3: t.player3_id ? String(t.player3_id) : "",
          p4: t.player4_id ? String(t.player4_id) : "",
          p5: t.player5_id ? String(t.player5_id) : "",
          formerly: Array.isArray(t.formerly) ? t.formerly : [],
          renamed_to: t.renamed_to || null,
        });
      }
    }
  }, [selectedTeam, teams]);

  // Team renames: the earlier name's results, rankings and map history count
  // for this team (backend data/team_aliases.py).
  const [aliasPick, setAliasPick] = useState("");
  const addTeamAlias = async () => {
    const oldId = Number(aliasPick);
    const newId = Number(teamForm.team_id);
    if (!oldId || !newId) return;
    try {
      await api.post("/admin/team-aliases", { old_team_id: oldId, new_team_id: newId });
      setAliasPick("");
      if (refresh) await refresh();
    } catch (e) {
      notify(`Could not link: ${e?.message || "unknown error"}`);
    }
  };
  const removeTeamAlias = async (oldKey) => {
    try {
      await api.delete(`/admin/team-aliases/${encodeURIComponent(oldKey)}`);
      if (refresh) await refresh();
    } catch (e) {
      notify(`Could not unlink: ${e?.message || "unknown error"}`);
    }
  };

  const openPlayerDetailsFromTeam = (playerId) => {
    const pid = Number(playerId);
    if (!Number.isFinite(pid) || pid <= 0) return;
    setDbTab("players");
    setShowTeamModal(false);
    setSelectedPlayer(pid);
    setShowPlayerModal(true);
  };

  const openTeamDetailsFromPlayer = (teamId) => {
    if (!teamId) return;
    setDbTab("teams");
    setShowPlayerModal(false);
    setSelectedTeam(teamId);
    setShowTeamModal(true);
  };

  // Keep an open team modal's map stats fresh when the shared import job finishes.
  useEffect(() => {
    if (!mapStatsModalRefreshRef) return undefined;
    mapStatsModalRefreshRef.current = async () => {
      if (!selectedTeam) return;
      try {
        const refreshedTeam = await api.get(`/teams/${selectedTeam}`);
        setTeamForm((prev) => ({
          ...prev,
          hltv_team_id: refreshedTeam?.hltv_team_id ?? prev.hltv_team_id,
          map_stats_json: refreshedTeam?.map_stats_json || "",
          map_stats_imported_at: refreshedTeam?.map_stats_imported_at || "",
          map_stats_source_url: refreshedTeam?.map_stats_source_url || "",
        }));
      } catch {
        // The main list refresh still covers this; the direct modal refresh is just for immediacy.
      }
    };
    return () => {
      mapStatsModalRefreshRef.current = null;
    };
  }, [selectedTeam, mapStatsModalRefreshRef]);

  const currentMapPool = ACTIVE_MAP_POOL;
  const canonicalMapName = (value) => {
    const raw = String(value || "").trim();
    const key = raw.toLowerCase().replace(/_/g, " ");
    if (key === "dust 2" || key === "dust2" || key === "de dust2") return "Dust2";
    const found = currentMapPool.find((map) => map.toLowerCase() === key);
    return found || raw;
  };
  const formatTeamPct = (value, digits = 1) => {
    const n = Number(value);
    return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : "-";
  };
  // Self-computed veto profile from stored match vetoes — covers maps HLTV's
  // team-maps page omits entirely (a permaban has no played-stats there).
  const [teamVetoProfile, setTeamVetoProfile] = useState(null);
  useEffect(() => {
    const tid = Number(selectedTeam);
    if (!showTeamModal || !Number.isFinite(tid) || tid <= 0) {
      setTeamVetoProfile(null);
      return;
    }
    let cancelled = false;
    api
      .get(`/teams/${tid}/veto-profile?months=3`, 30000)
      .then((res) => {
        if (!cancelled) setTeamVetoProfile(res || null);
      })
      .catch(() => {
        if (!cancelled) setTeamVetoProfile(null);
      });
    return () => {
      cancelled = true;
    };
  }, [showTeamModal, selectedTeam]);
  const teamMapStatsRows = useMemo(() => {
    try {
      const parsed = JSON.parse(String(teamForm.map_stats_json || "[]"));
      const byMap = {};
      (Array.isArray(parsed) ? parsed : []).forEach((row) => {
        const map = canonicalMapName(row?.map);
        if (map) byMap[map] = row;
      });
      const vetoByMap = {};
      const vetoMatches = Number(teamVetoProfile?.matches || 0);
      Object.entries(teamVetoProfile?.maps || {}).forEach(([m, v]) => {
        const map = canonicalMapName(m);
        if (map) vetoByMap[map] = v;
      });
      if (Object.keys(byMap).length === 0 && vetoMatches === 0) return [];
      // Every active-pool map gets a card — a map the team never touches is
      // itself information (usually their permaban). Our own veto data wins
      // over HLTV's page rates: it also covers never-played maps.
      return currentMapPool.map((map) => {
        const row = byMap[map] || {};
        const veto = vetoByMap[map];
        const useVeto = vetoMatches > 0;
        return {
          map,
          hasData: Boolean(byMap[map]) || Boolean(veto),
          played: Number(row.played || 0),
          winRate: Number(row.win_rate || 0),
          pickRate: useVeto ? Number(veto?.pick_rate || 0) : Number(row.pick_rate || 0),
          banRate: useVeto ? Number(veto?.ban_rate || 0) : Number(row.ban_rate || 0),
        };
      });
    } catch {
      return [];
    }
  }, [teamForm.map_stats_json, teamVetoProfile]);
  const teamMapStatsTotalPlayed = useMemo(
    () => teamMapStatsRows.reduce((sum, row) => sum + Math.max(0, Number(row.played || 0)), 0),
    [teamMapStatsRows]
  );
  const toNum = (v, fallback = 0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const filteredSortedPlayers = useMemo(() => {
    const q = playerSearch.trim().toLowerCase();
    const list = players.filter((p) => {
      if (!q) return true;
      const teamsText = (playerTeamLookup[p.player_id] || []).join(", ").toLowerCase();
      return (
        String(p.player_id).includes(q) ||
        String(p.name || "").toLowerCase().includes(q) ||
        teamsText.includes(q)
      );
    });

    list.sort((a, b) => {
      switch (playerSort) {
        case "name_desc":
          return String(b.name || "").localeCompare(String(a.name || ""));
        case "rating_desc":
          return toNum(b.rating) - toNum(a.rating);
        case "rating_asc":
          return toNum(a.rating) - toNum(b.rating);
        case "team_asc":
          return ((playerTeamLookup[a.player_id] || [])[0] || "").localeCompare(((playerTeamLookup[b.player_id] || [])[0] || ""));
        case "team_desc":
          return ((playerTeamLookup[b.player_id] || [])[0] || "").localeCompare(((playerTeamLookup[a.player_id] || [])[0] || ""));
        case "name_asc":
        default:
          return String(a.name || "").localeCompare(String(b.name || ""));
      }
    });

    return list;
  }, [players, playerSearch, playerSort, playerTeamLookup]);
  const filteredSortedTeams = useMemo(() => {
    const q = teamSearch.trim().toLowerCase();
    const list = teams.filter((t) => {
      if (!q) return true;
      const rosterText = [t.player1_id, t.player2_id, t.player3_id, t.player4_id, t.player5_id]
        .filter(Boolean)
        .map((pid) => playerLookup[pid] || pid)
        .join(", ")
        .toLowerCase();
      return (
        String(t.team_id).includes(q) ||
        String(t.name || "").toLowerCase().includes(q) ||
        rosterText.includes(q)
      );
    });

    list.sort((a, b) => {
      switch (teamSort) {
        case "name_desc":
          return String(b.name || "").localeCompare(String(a.name || ""));
        case "hltv_asc":
          return toNum(a.hltv_rank, 9999) - toNum(b.hltv_rank, 9999);
        case "hltv_desc":
          return toNum(b.hltv_rank, 9999) - toNum(a.hltv_rank, 9999);
        case "hltv_points_asc":
          return toNum(a.hltv_points) - toNum(b.hltv_points);
        case "hltv_points_desc":
          return toNum(b.hltv_points) - toNum(a.hltv_points);
        case "vrs_asc":
          return toNum(a.vrs_rank, 9999) - toNum(b.vrs_rank, 9999);
        case "vrs_desc":
          return toNum(b.vrs_rank, 9999) - toNum(a.vrs_rank, 9999);
        case "vrs_points_asc":
          return toNum(a.vrs_points) - toNum(b.vrs_points);
        case "vrs_points_desc":
          return toNum(b.vrs_points) - toNum(a.vrs_points);
        case "name_asc":
        default:
          return String(a.name || "").localeCompare(String(b.name || ""));
      }
    });

    return list;
  }, [teams, teamSearch, teamSort, playerLookup]);

  return (
    <div className="stack">
      <div className="tab-bar small">
        <button className={dbTab === "players" ? "tab active" : "tab"} onClick={() => setDbTab("players")}>
          Players
        </button>
        <button className={dbTab === "teams" ? "tab active" : "tab"} onClick={() => setDbTab("teams")}>
          Teams
        </button>
        <button className={dbTab === "matches" ? "tab active" : "tab"} onClick={() => setDbTab("matches")}>
          Matches
        </button>
        <button className={dbTab === "maps" ? "tab active" : "tab"} onClick={() => setDbTab("maps")}>
          Maps
        </button>
      </div>

      {dbTab === "maps" && <MapsTab teams={teams} />}

      {dbTab === "players" && <Section title="Players">
        {loading ? (
          <p>Loading...</p>
        ) : error ? (
          <p className="error">{error}</p>
        ) : (
          <div className="players-panel">
            <div className="players-controls">
              <Input label="Search Players" value={playerSearch} onChange={setPlayerSearch} placeholder="Name or team" className="search-field" />
            </div>
            <div className="players-table-wrap">
            <table className="players-table">
              <colgroup>
                <col style={{ width: "34%" }} />
                <col style={{ width: "44%" }} />
                <col style={{ width: "22%" }} />
              </colgroup>
              <thead>
                <tr>
                  <SortHeader sortValue={playerSort} asc="name_asc" desc="name_desc" onChange={setPlayerSort}>Name</SortHeader>
                  <SortHeader sortValue={playerSort} asc="team_asc" desc="team_desc" onChange={setPlayerSort}>Team</SortHeader>
                  <SortHeader sortValue={playerSort} asc="rating_asc" desc="rating_desc" defaultDirection="desc" onChange={setPlayerSort}>Rating</SortHeader>
                </tr>
              </thead>
              <tbody>
                {filteredSortedPlayers.map((p) => (
                  <tr
                    key={p.player_id}
                    className={selectedPlayer === p.player_id ? "row-active" : ""}
                    onClick={() => {
                      setSelectedPlayer(p.player_id);
                      setShowPlayerModal(true);
                    }}
                    >
                      <td><PlayerPhoto playerId={p.player_id} name={p.name} size={26} />{p.name}</td>
                      <td>{(playerTeamLookup[p.player_id] || []).join(", ") || "-"}</td>
                      <td>{Number(p.rating || 0).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </div>
        )}
      </Section>}

      {showPlayerModal && (
        <div className="modal-backdrop" onClick={() => setShowPlayerModal(false)}>
          <div className="modal player-modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-header player-hero-header">
              <div className="player-hero">
                <PlayerPhoto playerId={playerForm.player_id} name={playerForm.name} size={132} className="hero" />
                <div className="player-hero-main">
                  <h3 className="player-modal-title">{playerForm.name || "Player Details"}</h3>
                  <div className="hero-rating">
                    <span className="hero-rating-label">Rating</span>
                    <span className="hero-rating-value">{Number(playerForm.rating || 0).toFixed(2)}</span>
                  </div>
                </div>
                {heroTeam && (
                  <button
                    type="button"
                    className="player-hero-team"
                    onClick={() => openTeamDetailsFromPlayer(heroTeam.team_id)}
                    title={`Open ${heroTeam.team_name}`}
                  >
                    <TeamLogo hltvTeamId={heroTeam.hltv_team_id} name={heroTeam.team_name} size={84} />
                    <span>{heroTeam.team_name}</span>
                  </button>
                )}
              </div>
              <button className="close" onClick={() => setShowPlayerModal(false)}>
                &times;
              </button>
            </header>
            <div className="modal-body">
              <div className="tab-bar small">
                <button className={playerTab === "info" ? "tab active" : "tab"} onClick={() => setPlayerTab("info")}>
                  Ratings
                </button>
                <button className={playerTab === "roles" ? "tab active" : "tab"} onClick={() => setPlayerTab("roles")}>
                  Roles
                </button>
                <button className={playerTab === "boosters" ? "tab active" : "tab"} onClick={() => setPlayerTab("boosters")}>
                  Boosters
                </button>
                <button className={playerTab === "topxGraph" ? "tab active" : "tab"} onClick={() => setPlayerTab("topxGraph")}>
                  Top X
                </button>
              </div>
              {playerTab === "info" && (
                <div className="stack">
                  <div className="player-highlight-row">
                    {heroExtras?.bestRole && (
                      <div className="player-highlight-card">
                        <RoleBadge roleId={heroExtras.bestRole.id} size={46} />
                        <div>
                          <div className="player-highlight-label">Best Role</div>
                          <div className="player-highlight-value">{heroExtras.bestRole.label}</div>
                          <div className="player-highlight-sub">{heroExtras.bestRole.expected.toFixed(2)} pts/game</div>
                        </div>
                      </div>
                    )}
                    {heroExtras?.bestBooster && (
                      <div className="player-highlight-card">
                        <BoosterBadge boosterId={heroExtras.bestBooster.id} size={46} />
                        <div>
                          <div className="player-highlight-label">Best Booster</div>
                          <div className="player-highlight-value">{heroExtras.bestBooster.label}</div>
                          <div className="player-highlight-sub">
                            {heroExtras.bestBooster.delta >= 0 ? "+" : ""}
                            {Math.round(heroExtras.bestBooster.delta * 100)}% vs average ({Math.round(heroExtras.bestBooster.value * 100)}%)
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                  <div className="form-section">
                    <h4 className="form-heading">Recent Matches</h4>
                    {playerRecentForm.length === 0 && <p className="muted">No stored matches for this player's team yet.</p>}
                    {playerRecentForm.length > 0 && (
                      <table>
                        <thead>
                          <tr>
                            <th>Date</th>
                            <th>Opponent</th>
                            <th>Result</th>
                            <th>Rating</th>
                          </tr>
                        </thead>
                        <tbody>
                          {playerRecentForm.map((m) => {
                            const opp = teams.find(
                              (x) => String(x.name || "").trim().toLowerCase() === String(m.opponent || "").trim().toLowerCase()
                            );
                            return (
                              <tr
                                key={m.match_url || `${m.date}-${m.opponent}`}
                                className={m.match_url ? "row-link" : ""}
                                onClick={() => openStoredMatch(m.match_url)}
                                title={m.match_url ? "Open stored match details" : undefined}
                              >
                                <td>{formatDMY(m.date)}</td>
                                <td>
                                  <TeamLogo hltvTeamId={opp?.hltv_team_id} name={m.opponent} size={20} />
                                  {m.opponent}
                                </td>
                                <td>
                                  <span className={`hero-form-chip ${m.won ? "win" : "loss"}`}>{m.won ? "W" : "L"}</span>{" "}
                                  {m.score}
                                </td>
                                <td className="form-rating">
                                  {m.rating !== null && Number.isFinite(Number(m.rating)) ? Number(m.rating).toFixed(2) : "—"}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    )}
                  </div>
                </div>
              )}
              {playerTab === "topxGraph" && (
                <div className="stack">
                  {playerCurveError && <p className="error">{playerCurveError}</p>}
                  {(playerCurve?.model_rows || []).length > 0 && (
                    <div className="value-chart-wrap topx-chart">
                      <ModelCurveChart curve={playerCurve} height={300} />
                    </div>
                  )}
                </div>
              )}
              {playerTab === "boosters" && (
                <div className="booster-tier-grid">
                  {Object.entries(boosterNames).map(([id, label]) => {
                    const value = Number(boosterForm[id]);
                    const average = Number(boosterAverages[id]);
                    const hasValue = Number.isFinite(value);
                    const hasAverage = Number.isFinite(average);
                    const percentText = hasValue ? `${Math.round(value * 100)}%` : "N/A";
                    let deltaText = "vs avg: N/A";
                    let deltaClass = "";
                    if (hasValue && hasAverage) {
                      const deltaPct = Math.round((value - average) * 100);
                      if (deltaPct > 0) {
                        deltaText = `+${deltaPct}% vs avg`;
                        deltaClass = "better";
                      } else if (deltaPct < 0) {
                        deltaText = `${deltaPct}% vs avg`;
                        deltaClass = "worse";
                      } else {
                        deltaText = "same as avg";
                        deltaClass = "equal";
                      }
                    }
                    return (
                      <div key={id} className="booster-tier-card">
                        <BoosterBadge boosterId={id} size={54} />
                        <div className="booster-tier-label">{label}</div>
                        <div className="booster-tier-value">{percentText}</div>
                        <div className={`booster-tier-delta ${deltaClass}`}>{deltaText}</div>
                      </div>
                    );
                  })}
                </div>
              )}
              {playerTab === "roles" && (
                <div className="role-tier-grid">
                  {(() => {
                    const normalizeRate = (v) => {
                      const n = Number(v);
                      if (!Number.isFinite(n)) return null;
                      if (n < 0) return 0;
                      if (n > 1) return n / 100;
                      return n;
                    };

                    const roleMetrics = Object.entries(roleNames).map(([id, label]) => {
                      const majorRate = normalizeRate(roleForm[id]?.major ?? roleForm[id]?.major_win_pct);
                      const minorRate = normalizeRate(roleForm[id]?.minor ?? roleForm[id]?.minor_win_pct);
                      const hasRates = majorRate !== null && minorRate !== null;
                      const expected = hasRates
                        ? 5 * majorRate + 2 * minorRate - 2 * (1 - majorRate - minorRate)
                        : null;
                      return {
                        id,
                        label,
                        hasRates,
                        majorRate,
                        minorRate,
                        expected,
                      };
                    });

                    const top3 = roleMetrics
                      .filter((r) => Number.isFinite(r.expected))
                      .sort((a, b) => b.expected - a.expected)
                      .slice(0, 3);
                    const rankById = {};
                    top3.forEach((r, idx) => {
                      rankById[r.id] = idx + 1;
                    });

                    return roleMetrics.map((r) => {
                      const majorText = r.hasRates ? `${Math.round(r.majorRate * 100)}%` : "N/A";
                      const minorText = r.hasRates ? `${Math.round(r.minorRate * 100)}%` : "N/A";
                      const expectedText = Number.isFinite(r.expected) ? r.expected.toFixed(2) : "N/A";
                      const rank = rankById[r.id] || null;
                      const medalClass = rank === 1 ? "gold" : rank === 2 ? "silver" : rank === 3 ? "bronze" : "";

                      return (
                        <div key={r.id} className={`role-tier-card ${medalClass}`}>
                          <RoleBadge roleId={r.id} />
                          <div className="role-tier-label">{r.label}</div>
                          <div className="role-tier-split">
                            <span>Major: {majorText}</span>
                            <span>Minor: {minorText}</span>
                          </div>
                          <div className="role-tier-expected">{expectedText} pts/game</div>
                        </div>
                      );
                    });
                  })()}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {showTeamModal && (
        <div className="modal-backdrop" onClick={() => setShowTeamModal(false)}>
          <div className="modal team-modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-header">
              <div className="modal-title-wrap">
                <TeamLogo hltvTeamId={teamForm.hltv_team_id} name={teamForm.name} size={44} />
                <h3 className="player-modal-title">{teamForm.name || "Team"}</h3>
              </div>
              <button className="close" onClick={() => setShowTeamModal(false)}>
                &times;
              </button>
            </header>
            <div className="modal-body">
              <section className="team-detail-block">
                <h4 className="team-detail-heading">Roster</h4>
                <div className="team-roster-grid">
                  {[teamForm.p1, teamForm.p2, teamForm.p3, teamForm.p4, teamForm.p5].map((pid, idx) => (
                    <button
                      type="button"
                      className="team-player-tile"
                      key={`team-player-${idx + 1}`}
                      onClick={() => openPlayerDetailsFromTeam(pid)}
                      disabled={!Number(pid)}
                      title={Number(pid) ? `Open ${playerLookup[Number(pid)] || "player"}` : "No player"}
                    >
                      <PlayerPhoto playerId={pid} name={playerLookup[Number(pid)]} size={72} className="roster-photo" />
                      <div className="team-player-name">{playerLookup[Number(pid)] || "-"}</div>
                    </button>
                  ))}
                </div>
              </section>

              <section className="team-detail-block">
                <h4 className="team-detail-heading">Rankings</h4>
                <div className="team-stats-grid">
                  <div className="team-stat-card">
                    <div className="team-stat-label">HLTV Rank</div>
                    <div className="team-stat-value">{teamForm.hltv_rank || "-"}</div>
                  </div>
                  <div className="team-stat-card">
                    <div className="team-stat-label">HLTV Points</div>
                    <div className="team-stat-value">{teamForm.hltv_points || "-"}</div>
                  </div>
                  <div className="team-stat-card">
                    <div className="team-stat-label">VRS Rank</div>
                    <div className="team-stat-value">{teamForm.vrs_rank || "-"}</div>
                  </div>
                  <div className="team-stat-card">
                    <div className="team-stat-label">VRS Points</div>
                    <div className="team-stat-value">{teamForm.vrs_points || "-"}</div>
                  </div>
                </div>
              </section>

              <section className="team-detail-block">
                <h4 className="team-detail-heading">Formerly</h4>
                <div className="team-alias-row">
                  {(teamForm.formerly || []).map((a) => (
                    <span className="pill team-alias-pill" key={a.old_key}>
                      {a.old_name || a.old_key}
                      <button type="button" className="team-alias-remove" onClick={() => removeTeamAlias(a.old_key)} aria-label="Unlink">
                        &times;
                      </button>
                    </span>
                  ))}
                  {teamForm.renamed_to && (
                    <span className="pill team-alias-pill">Now {teamForm.renamed_to.new_name}</span>
                  )}
                  {!teamForm.renamed_to && (
                    <>
                      <select value={aliasPick} onChange={(e) => setAliasPick(e.target.value)}>
                        <option value="">Team</option>
                        {teams
                          .filter((t) => Number(t.team_id) !== Number(teamForm.team_id) && !t.renamed_to)
                          .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
                          .map((t) => (
                            <option key={t.team_id} value={t.team_id}>
                              {t.name}
                            </option>
                          ))}
                      </select>
                      <button type="button" className="secondary" disabled={!aliasPick} onClick={addTeamAlias}>
                        Link
                      </button>
                    </>
                  )}
                </div>
              </section>

              <section className="team-detail-block">
                <div className="team-detail-heading-row">
                  <div>
                    <h4 className="team-detail-heading">Map Stats</h4>
                    {teamMapStatsRows.length > 0 && (
                      <p className="muted">
                        Last 6 months: {teamMapStatsTotalPlayed.toLocaleString()} maps played
                        {Number(teamVetoProfile?.matches || 0) > 0
                          ? ` · pick/ban from ${teamVetoProfile.matches} stored vetoes`
                          : ""}
                      </p>
                    )}
                  </div>
                </div>
                {mapStats.show && (
                  <div className="team-map-stats-progress">
                    <p className="muted">
                      {mapStats.processed.toLocaleString()} / {mapStats.total.toLocaleString()} | ok {mapStats.ok} | failed{" "}
                      {mapStats.failed}
                      {mapStats.active && mapStats.total > mapStats.processed ? ` | ETA: ${formatBatchEta(mapStats.etaSeconds)}` : ""}
                    </p>
                    <div className="progress">
                      <div className="progress-bar determinate" style={{ width: `${mapStats.progressPct}%` }} />
                    </div>
                    <p className="muted">Status: {mapStats.statusLabel}</p>
                  </div>
                )}
                <div className="team-map-stats-grid">
                  {teamMapStatsRows.length > 0 ? (
                    teamMapStatsRows.map((row) => (
                      <div
                        className={`team-map-stat-card ${row.hasData ? "" : "empty"}`}
                        key={`team-map-${row.map}`}
                        style={{ "--map-color": MAP_BAR_COLORS[row.map] || MAP_BAR_FALLBACK_COLOR }}
                      >
                        <div className="team-map-stat-head">
                          <span className="team-map-stat-title">{row.map}</span>
                          <span className="team-map-stat-count">{row.played > 0 ? `${row.played} maps` : "not played"}</span>
                        </div>
                        <div className="team-map-stat-main">{row.played > 0 ? formatTeamPct(row.winRate, 1) : "—"}</div>
                        <div className="team-map-stat-bar">
                          <div
                            className="team-map-stat-bar-fill"
                            style={{ width: `${row.played > 0 ? Math.max(0, Math.min(100, Number(row.winRate || 0) * 100)) : 0}%` }}
                          />
                        </div>
                        <div className="team-map-stat-veto">
                          <div className="veto-row">
                            <span className="veto-label">Pick</span>
                            <div className="veto-bar">
                              <div
                                className="veto-fill pick"
                                style={{ width: `${Math.max(0, Math.min(100, Number(row.pickRate || 0) * 100))}%` }}
                              />
                            </div>
                            <span className="veto-val">{formatTeamPct(row.pickRate, 1)}</span>
                          </div>
                          <div className="veto-row">
                            <span className="veto-label">Ban</span>
                            <div className="veto-bar">
                              <div
                                className="veto-fill ban"
                                style={{ width: `${Math.max(0, Math.min(100, Number(row.banRate || 0) * 100))}%` }}
                              />
                            </div>
                            <span className="veto-val">{formatTeamPct(row.banRate, 1)}</span>
                          </div>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="team-map-stat-empty">No current-pool map stats imported</div>
                  )}
                </div>
              </section>
            </div>
          </div>
        </div>
      )}

      {playerMatchDetail && (
        <MatchDetailModal
          row={playerMatchDetail}
          teams={teams}
          players={players}
          onClose={() => setPlayerMatchDetail(null)}
          onOpenPlayer={(pid) => {
            setPlayerMatchDetail(null);
            openPlayerDetailsFromTeam(pid);
          }}
          onOpenTeam={(tid) => {
            setPlayerMatchDetail(null);
            openTeamDetailsFromPlayer(tid);
          }}
        />
      )}

      {dbTab === "teams" && <Section title="Teams">
        {loading ? (
          <p>Loading...</p>
        ) : error ? (
          <p className="error">{error}</p>
        ) : (
          <>
            <div className="teams-controls">
              <div className="grid two teams-filters">
                <Input label="Search Teams" value={teamSearch} onChange={setTeamSearch} placeholder="Name or player" className="search-field" />
              </div>
            </div>
            <table>
              <thead>
                <tr>
                  <SortHeader sortValue={teamSort} asc="name_asc" desc="name_desc" onChange={setTeamSort}>Name</SortHeader>
                  <SortHeader sortValue={teamSort} asc="hltv_asc" desc="hltv_desc" onChange={setTeamSort}>HLTV Rank</SortHeader>
                  <SortHeader sortValue={teamSort} asc="hltv_points_asc" desc="hltv_points_desc" defaultDirection="desc" onChange={setTeamSort}>HLTV Points</SortHeader>
                  <SortHeader sortValue={teamSort} asc="vrs_asc" desc="vrs_desc" onChange={setTeamSort}>VRS Rank</SortHeader>
                  <SortHeader sortValue={teamSort} asc="vrs_points_asc" desc="vrs_points_desc" defaultDirection="desc" onChange={setTeamSort}>VRS Points</SortHeader>
                  <th>Players</th>
                </tr>
              </thead>
              <tbody>
                  {filteredSortedTeams.map((t) => (
                    <tr
                      key={t.team_id}
                      className={selectedTeam === t.team_id ? "row-active" : ""}
                      onClick={() => {
                        setSelectedTeam(t.team_id);
                        setShowTeamModal(true);
                      }}
                    >
                    <td><TeamLogo hltvTeamId={t.hltv_team_id} name={t.name} size={28} />{t.name}</td>
                    <td>{t.hltv_rank}</td>
                    <td>{t.hltv_points ?? "-"}</td>
                    <td>{t.vrs_rank}</td>
                    <td>{t.vrs_points ?? "-"}</td>
                    <td>
                      {[t.player1_id, t.player2_id, t.player3_id, t.player4_id, t.player5_id]
                        .filter(Boolean)
                        .map((pid) => playerLookup[pid] || pid)
                        .join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Section>}
      {dbTab === "matches" && (
        <Section title="Matches">
          <MatchesDataPanel
            notify={notify}
            teams={teams}
            players={players}
            onOpenPlayer={openPlayerDetailsFromTeam}
            onOpenTeam={openTeamDetailsFromPlayer}
          />
        </Section>
      )}

    </div>
  );
}

const teamInitials = (name) =>
  String(name || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();

function SchedulingTab({ notify, players, refresh }) {
  const [status, setStatus] = useState(null);
  const [runs, setRuns] = useState([]);
  const [saveBusy, setSaveBusy] = useState(false);
  const [runBusy, setRunBusy] = useState(false);
  const [message, setMessage] = useState("");
  // Local edit state so typing a value doesn't fire a save per keystroke.
  const [runTime, setRunTime] = useState("00:00");
  const [morningTime, setMorningTime] = useState("09:00"); // the light morning run: results import + season read
  const [lookbackDays, setLookbackDays] = useState(3);
  const [mapModelMinutes, setMapModelMinutes] = useState(120);
  const [teamDataTopN, setTeamDataTopN] = useState(200);
  const [runDetail, setRunDetail] = useState(null); // the run whose full details are open
  const pollRef = useRef(null);

  const TASK_LABELS = {
    events: "Fantasy events",
    rankings: "Team rankings",
    team_data: "Rosters & map stats",
    matches: "New matches",
    map_model: "Map model data",
    valuations: "Event valuations",
    hltv_session: "HLTV login",
    backup: "Backup",
    ratings: "Top-X ratings",
  };
  const TASK_SUB = {
    events: "hltv.org/fantasy",
    rankings: "HLTV + VRS",
    team_data: "top-ranked teams",
    matches: "results import",
    map_model: "vetoes · scoreboards · map stats",
  };
  const [sessionBusy, setSessionBusy] = useState(false);
  const checkSession = async () => {
    setSessionBusy(true);
    setMessage("");
    try {
      await api.get("/admin/hltv-session-status", 120000);
      await loadStatus();
    } catch (e) {
      setMessage(e?.message || "HLTV login check failed");
    } finally {
      setSessionBusy(false);
    }
  };

  const loadStatus = async () => {
    try {
      const data = await api.get("/schedule/status", 15000);
      setStatus(data);
      if (data?.config) {
        setRunTime(String(data.config.run_time || "00:00"));
        setMorningTime(String(data.config.morning_time || "09:00"));
        setLookbackDays(Number(data.config.matches_lookback_days || 3));
        setMapModelMinutes(Number(data.config.map_model_minutes || 120));
        setTeamDataTopN(Number(data.config.team_data_top_n || 200));
      }
    } catch (e) {
      setMessage(e?.message || "Backend not reachable.");
    }
  };
  const loadRuns = async () => {
    try {
      const data = await api.get("/schedule/runs?limit=200", 15000);
      setRuns(data.runs || []);
    } catch {
      /* runs are non-critical; the status error already surfaces */
    }
  };

  useEffect(() => {
    loadStatus();
    loadRuns();
    loadCoverage();
    pollRef.current = setInterval(() => {
      loadStatus();
      loadRuns();
    }, 5000);
    return () => clearInterval(pollRef.current);
  }, []);

  const config = status?.config || {};
  const state = status?.state || {};
  const lastByTask = status?.last_success_by_task || {};

  // What the nightly tasks maintain, counted on their cards; refreshed on
  // load and whenever a run finishes.
  const [coverage, setCoverage] = useState({});
  const loadCoverage = async () => {
    const get = (path) => api.get(path, 60000).catch(() => null);
    const [rosters, hist, veto, sb] = await Promise.all([
      get("/teams/rosters/coverage?top_n=0"),
      get("/events/hltv-results/historical-map-stats/coverage"),
      get("/events/hltv-results/veto-backfill/coverage"),
      get("/events/hltv-results/map-scoreboards/coverage"),
    ]);
    setCoverage({ rosters, hist, veto, sb });
  };
  const wasRunningRef = useRef(false);
  useEffect(() => {
    if (wasRunningRef.current && !state.running) loadCoverage();
    wasRunningRef.current = Boolean(state.running);
  }, [state.running]);

  const patchConfig = async (patch) => {
    setSaveBusy(true);
    setMessage("");
    try {
      const data = await api.post("/schedule/config", patch);
      setStatus(data);
      notify("Schedule updated");
    } catch (e) {
      setMessage(e?.message || "Failed to update schedule.");
    } finally {
      setSaveBusy(false);
    }
  };

  const runNow = async (task) => {
    setRunBusy(true);
    setMessage("");
    try {
      const res = await api.post("/schedule/run-now", { task });
      if (res?.status === "busy") {
        setMessage(res.detail || "A run is already in progress.");
      } else if (res?.status === "error") {
        setMessage(res.detail || "Failed to start run.");
      } else {
        notify(`Started: ${(res.tasks || [task]).map((t) => TASK_LABELS[t] || t).join(", ")}`);
      }
      loadStatus();
    } catch (e) {
      setMessage(e?.message || "Failed to start run.");
    } finally {
      setRunBusy(false);
    }
  };

  // ---- formatting -----------------------------------------------------
  const pad2 = (n) => String(n).padStart(2, "0");
  const clock = (ts) => (Number(ts) > 0 ? new Date(Number(ts) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "-");
  const dayOf = (ts) => {
    const d = new Date(Number(ts) * 1000);
    return `${pad2(d.getDate())}/${pad2(d.getMonth() + 1)}/${d.getFullYear()}`;
  };
  const fmtDur = (secs) => {
    const s = Math.max(0, Math.round(Number(secs) || 0));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${pad2(s % 60)}s`;
    return `${Math.floor(m / 60)}h ${pad2(m % 60)}m`;
  };
  const relTime = (ts) => {
    if (!(Number(ts) > 0)) return "never";
    const diff = Date.now() / 1000 - Number(ts);
    const abs = Math.abs(diff);
    const txt = abs < 60 ? "now" : abs < 3600 ? `${Math.round(abs / 60)}m` : abs < 86400 ? `${Math.floor(abs / 3600)}h ${pad2(Math.round((abs % 3600) / 60))}m` : `${Math.floor(abs / 86400)}d`;
    if (txt === "now") return txt;
    return diff >= 0 ? `${txt} ago` : `in ${txt}`;
  };
  const nextRun = Number(status?.next_run_at || 0);
  const nextLabel = nextRun > 0 ? (dayOf(nextRun) === dayOf(Date.now() / 1000) ? "Today" : "Tomorrow") : "";
  const session = status?.hltv_session || null;
  const sessionLevel = session ? session.level || "ok" : "none";

  // ---- run history grouped by night ---------------------------------
  const nights = useMemo(() => {
    const groups = new Map();
    (runs || []).forEach((run) => {
      const key = dayOf(run.started_at);
      if (!groups.has(key)) groups.set(key, { key, rows: [], failed: 0, secs: 0, first: run.started_at, last: run.finished_at || run.started_at });
      const g = groups.get(key);
      g.rows.push(run);
      if (run.status !== "success") g.failed += 1;
      if (run.finished_at && run.started_at) g.secs += Math.max(0, run.finished_at - run.started_at);
      g.first = Math.min(g.first, run.started_at);
      g.last = Math.max(g.last, run.finished_at || run.started_at);
    });
    return Array.from(groups.values());
  }, [runs]);
  const lastNight = nights[0] || null;
  const recentRuns = (runs || []).slice(0, 10);
  const runStatusPill = (run) => (
    <span className={run.status === "success" ? "pill ok" : run.status === "warning" ? "pill caution" : "pill warn"}>{run.status}</span>
  );

  const lastRunOf = (task) => (runs || []).find((r) => r.task === task && r.finished_at) || null;
  const taskRow = (task) => {
    const label = TASK_LABELS[task] || task;
    const on = Boolean(config[`do_${task}`]);
    const last = lastByTask[task];
    const run = lastRunOf(task);
    const setting =
      task === "matches"
        ? ["Look back", <input key="lb" type="number" min={1} max={30} aria-label="Look back, days" value={lookbackDays} onChange={(e) => setLookbackDays(Number(e.target.value))} onBlur={() => patchConfig({ matches_lookback_days: lookbackDays })} disabled={saveBusy} />, "days"]
        : task === "team_data"
          ? ["Top", <input key="tn" type="number" min={10} max={500} aria-label="Top teams" value={teamDataTopN} onChange={(e) => setTeamDataTopN(Number(e.target.value))} onBlur={() => patchConfig({ team_data_top_n: teamDataTopN })} disabled={saveBusy} />, "teams"]
          : task === "map_model"
            ? ["Up to", <input key="mm" type="number" min={1} max={720} aria-label="Minutes per night" value={mapModelMinutes} onChange={(e) => setMapModelMinutes(Number(e.target.value))} onBlur={() => patchConfig({ map_model_minutes: mapModelMinutes })} disabled={saveBusy} />, "min / night"]
            : null;
    const live = state.running && state.current_task === task;
    const cov = coverage || {};
    const covLines =
      {
        team_data: [
          ["Rosters", cov.rosters?.checked, cov.rosters?.teams_in_scope, cov.rosters?.unchecked],
          ["Fewer than five", cov.rosters?.short_lineups, null, 0],
        ],
        map_model: [
          ["Map windows", cov.hist?.cached_windows, cov.hist?.required_windows, cov.hist?.missing_windows],
          ["Vetoes", cov.veto?.with_veto, cov.veto?.total_matches, cov.veto?.missing_veto],
          ["Scoreboards", cov.sb?.with_map_scoreboards, cov.sb?.total_matches, cov.sb?.missing_map_scoreboards],
        ],
        matches: [["Stored", cov.veto?.total_matches, null, 0]],
      }[task] || [];
    const pill = live ? (
      <span className="pill ok">Running{state.total > 0 ? ` ${Number(state.processed).toLocaleString()} / ${Number(state.total).toLocaleString()}` : ""}</span>
    ) : !on ? (
      <span className="pill">Off</span>
    ) : !run ? (
      <span className="pill">-</span>
    ) : run.status === "success" ? (
      <span className="pill ok">OK</span>
    ) : run.status === "warning" ? (
      <span className="pill caution" title={run.message || ""}>Warning</span>
    ) : (
      <span className="pill warn" title={run.message || ""}>Failed</span>
    );
    return (
      <tr key={task} className={`${on ? "" : "off"}${live ? " live" : ""}`}>
        <td>
          <div className="sch-row-name">{label}</div>
          <div className="sch-row-sub">{TASK_SUB[task]}</div>
        </td>
        <td className="sch-col-switch">
          <label className="switch" title={on ? "Enabled" : "Disabled"}>
            <input type="checkbox" aria-label={`Run ${label} nightly`} checked={on} onChange={(e) => patchConfig({ [`do_${task}`]: e.target.checked })} disabled={saveBusy} />
            <span className="switch-track" />
          </label>
        </td>
        <td>
          <div className="sch-row-top">
            {pill}
            <span className="sch-row-when" title={Number(last) > 0 ? new Date(Number(last) * 1000).toLocaleString() : ""}>
              {relTime(last)}
            </span>
          </div>
          <div className="sch-row-sub">{run ? `${fmtDur(run.finished_at - run.started_at)} · ${run.trigger}` : "\u00a0"}</div>
        </td>
        <td>
          {setting ? (
            <label className="sch-row-setting">
              <span className="sch-row-sub">{setting[0]}</span>
              {setting[1]}
              <span className="sch-row-sub">{setting[2]}</span>
            </label>
          ) : null}
        </td>
        <td>
          {covLines.length > 0 ? (
            <div className="sch-row-data">
              {covLines.map(([name, done, total, missing]) => (
                <span className="sch-row-stat" key={name}>
                  <span className="sch-row-sub">{name} </span>
                  <strong>{done == null ? "-" : Number(done).toLocaleString()}</strong>
                  {total != null ? <span className="sch-row-sub"> / {Number(total).toLocaleString()}</span> : null}
                  {Number(missing) > 0 ? <span className="warn"> · {Number(missing).toLocaleString()} missing</span> : null}
                </span>
              ))}
            </div>
          ) : null}
        </td>
        <td className="sch-col-run">
          <button className="secondary" aria-label={`Run ${label} now`} onClick={() => runNow(task)} disabled={runBusy || state.running}>
            Run
          </button>
        </td>
      </tr>
    );
  };

  return (
    <div className="stack">
      <Section title="Nightly Data Refresh">
        <div className="sch-strip">
          <div className={`topx-tile sch-tile${config.enabled ? "" : " empty"}`}>
            <div className="topx-tile-label">Scheduler</div>
            <div className="sch-tile-row">
              <label className="switch" title={config.enabled ? "Enabled" : "Disabled"}>
                <input type="checkbox" checked={Boolean(config.enabled)} onChange={(e) => patchConfig({ enabled: e.target.checked })} disabled={saveBusy} />
                <span className="switch-track" />
              </label>
              <input type="time" value={runTime} onChange={(e) => setRunTime(e.target.value)} onBlur={() => patchConfig({ run_time: runTime })} disabled={saveBusy} />
            </div>
            <div className="sch-tile-row">
              <label className="switch" title={config.do_morning ? "Morning results import on" : "Morning results import off"}>
                <input type="checkbox" checked={Boolean(config.do_morning)} onChange={(e) => patchConfig({ do_morning: e.target.checked })} disabled={saveBusy || !config.enabled} />
                <span className="switch-track" />
              </label>
              <input type="time" value={morningTime} onChange={(e) => setMorningTime(e.target.value)} onBlur={() => patchConfig({ morning_time: morningTime })} disabled={saveBusy || !config.enabled} />
            </div>
            <div className="topx-tile-maps">{config.enabled ? `Daily at ${runTime}${config.do_morning ? ` · results at ${morningTime}` : ""}` : "Paused"}</div>
          </div>
          <div className="topx-tile sch-tile">
            <div className="topx-tile-label">Next run</div>
            <div className="topx-tile-value">{nextRun > 0 ? clock(nextRun) : "-"}</div>
            <div className="topx-tile-maps">
              {nextRun > 0 ? `${nextLabel} · ${relTime(nextRun)}` : ""}
              {status?.catch_up_pending ? " · catching up" : ""}
            </div>
          </div>
          <div className={`topx-tile sch-tile${lastNight && lastNight.failed > 0 ? " warn" : ""}`}>
            <div className="topx-tile-label">Last night</div>
            <div className="topx-tile-value">{lastNight ? (lastNight.failed > 0 ? `${lastNight.failed} failed` : "All ok") : "-"}</div>
            <div className="topx-tile-maps">
              {lastNight ? `${lastNight.rows.length} tasks · ${fmtDur(lastNight.secs)} · ${lastNight.key}` : ""}
            </div>
          </div>
          <div className={`topx-tile sch-tile${sessionLevel !== "ok" && sessionLevel !== "none" ? " warn" : ""}`}>
            <div className="topx-tile-label">HLTV login</div>
            <div className="topx-tile-value">{sessionLevel === "none" ? "-" : sessionLevel === "ok" ? "Signed in" : sessionLevel}</div>
            <div className="sch-tile-row">
              <span className="topx-tile-maps" title={session?.message || ""}>
                {session?.checked_at ? `checked ${relTime(session.checked_at)}` : "not checked"}
              </span>
              <button className="secondary" onClick={checkSession} disabled={sessionBusy || state.running}>
                {sessionBusy ? "Checking…" : "Check"}
              </button>
            </div>
          </div>
        </div>
        {state.running && (
          <div className="sch-live">
            <div className="pills">
              <span className="pill ok">Running</span>
              <strong>{TASK_LABELS[state.current_task] || state.current_task}</strong>
              <span className="muted">
                {state.total > 0 ? `${Number(state.processed).toLocaleString()} / ${Number(state.total).toLocaleString()}` : ""}
                {state.trigger ? ` · ${state.trigger}` : ""}
                {state.started_at ? ` · ${fmtDur(Date.now() / 1000 - state.started_at)}` : ""}
              </span>
              {state.message && <span className="muted">{state.message}</span>}
            </div>
            {state.total > 0 && (
              <div className="mm-bar">
                <div className="mm-bar-fill" style={{ width: `${Math.min(100, (100 * Number(state.processed)) / Number(state.total))}%` }} />
              </div>
            )}
          </div>
        )}
        {message && <p className="muted">{message}</p>}
      </Section>

      <Section title="Tasks">
        <table className="sch-task-table">
          <thead>
            <tr>
              <th>Task</th>
              <th className="sch-col-switch">Nightly</th>
              <th>Last run</th>
              <th>Setting</th>
              <th>Data</th>
              <th className="sch-col-run" />
            </tr>
          </thead>
          <tbody>{["events", "rankings", "team_data", "matches", "map_model"].map(taskRow)}</tbody>
        </table>
        <div className="actions">
          <button className="primary" onClick={() => runNow("all")} disabled={runBusy || state.running}>
            {state.running ? "Refresh in progress…" : "Run all now"}
          </button>
        </div>
      </Section>

      <Section title="Run History">
        {recentRuns.length === 0 ? (
          <p className="muted">None</p>
        ) : (
          <table className="sch-runs">
            <thead>
              <tr>
                <th>Task</th>
                <th>Trigger</th>
                <th>Status</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {recentRuns.map((run) => (
                <tr key={run.id} onClick={() => setRunDetail(run)}>
                  <td>{TASK_LABELS[run.task] || run.task}</td>
                  <td className="muted">{run.trigger}</td>
                  <td>{runStatusPill(run)}</td>
                  <td>
                    {dayOf(run.started_at)} {clock(run.started_at)}
                  </td>
                  <td>{run.finished_at && run.started_at ? fmtDur(run.finished_at - run.started_at) : "-"}</td>
                  <td className="details">{run.message || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {runDetail && (
        <div className="modal-backdrop" onClick={() => setRunDetail(null)}>
          <div className="modal sch-run-modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-header">
              <h3>{TASK_LABELS[runDetail.task] || runDetail.task}</h3>
              <button className="close" onClick={() => setRunDetail(null)}>
                &times;
              </button>
            </header>
            <div className="modal-body">
              <div className="pills sch-run-meta">
                {runStatusPill(runDetail)}
                <span className="pill">{runDetail.trigger}</span>
                <span className="pill">
                  {dayOf(runDetail.started_at)} {clock(runDetail.started_at)}
                </span>
                <span className="pill">{runDetail.finished_at && runDetail.started_at ? fmtDur(runDetail.finished_at - runDetail.started_at) : "-"}</span>
              </div>
              <pre className="sch-run-message">{runDetail.message || "-"}</pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The player card's Top X tab: the learned model's expected rating against
// a typical team of each HLTV rank (orange, log rank axis) and the player's
// last ten maps as grey dots at the opponent's rank at the time.
const MODEL_CURVE_TICKS = [1, 2, 3, 5, 10, 20, 30, 50, 100];

const ModelCurveChart = ({ curve, height = 300 }) => {
  const line = (Array.isArray(curve?.model_rows) ? curve.model_rows : [])
    .map((m) => ({ rank: Number(m?.rank), rating: Number(m?.rating) }))
    .filter((m) => Number.isFinite(m.rank) && m.rank >= 1 && m.rank <= 100 && Number.isFinite(m.rating));
  const dots = (Array.isArray(curve?.recent_maps) ? curve.recent_maps : [])
    .map((m, i) => ({
      key: `${m?.date}-${m?.map}-${i}`,
      rank: Number.isFinite(Number(m?.opponent_rank)) ? Math.min(100, Math.max(1, Number(m.opponent_rank))) : 100,
      unranked: !Number.isFinite(Number(m?.opponent_rank)),
      rating: Number(m?.rating),
      date: m?.date,
      map: m?.map,
      opponent: m?.opponent,
      score: m?.score,
      won: m?.won,
    }))
    .filter((d) => Number.isFinite(d.rating));
  const axis = buildNiceStepAxis([...line.map((p) => p.rating), ...dots.map((d) => d.rating)], 0.2);
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={line} margin={{ top: 12, right: 18, left: 6, bottom: 12 }}>
        <CartesianGrid stroke="#232a34" strokeDasharray="3 3" />
        <XAxis
          type="number"
          dataKey="rank"
          scale="log"
          domain={[1, 100]}
          ticks={MODEL_CURVE_TICKS}
          interval={0}
          minTickGap={0}
          tick={{ fill: "#9fb2c9", fontSize: 12 }}
          axisLine={{ stroke: "#3a4452" }}
          tickLine={{ stroke: "#3a4452" }}
          tickFormatter={(v) => String(v)}
          label={{ value: "Opponent HLTV rank", position: "insideBottom", offset: -8, fill: "#7f97bd", fontSize: 11 }}
        />
        <YAxis
          tick={{ fill: "#9fb2c9", fontSize: 12 }}
          axisLine={{ stroke: "#3a4452" }}
          tickLine={{ stroke: "#3a4452" }}
          domain={axis.domain}
          ticks={axis.ticks}
          interval={0}
          minTickGap={0}
          tickFormatter={(v) => Number(v).toFixed(1)}
          width={44}
        />
        <Tooltip
          cursor={false}
          content={({ active, payload }) => {
            if (!active || !payload || payload.length === 0) return null;
            const d = payload[0]?.payload || {};
            return (
              <div className="pool-tooltip">
                {d.map ? (
                  <>
                    <div className="pool-tooltip-title">
                      {d.map} · {formatDMY(d.date)}
                    </div>
                    <div>
                      {d.opponent} {d.unranked ? "(unranked)" : `#${d.rank}`} · {d.won === null || d.won === undefined ? "" : d.won ? "W" : "L"} {d.score}
                    </div>
                    <div>Rating {Number(d.rating).toFixed(2)}</div>
                  </>
                ) : (
                  <>
                    <div className="pool-tooltip-title">Opponent #{d.rank}</div>
                    <div>Expected {Number(d.rating).toFixed(3)}</div>
                  </>
                )}
              </div>
            );
          }}
        />
        <Line type="monotone" dataKey="rating" name="Model" stroke="#ff6b1a" strokeWidth={2.6} dot={false} isAnimationActive={false} />
        <Scatter data={dots} dataKey="rating" name="Last 10 maps" fill="#8fa3bf" shape={(p) => <circle cx={p.cx} cy={p.cy} r={5} fill="#8fa3bf" stroke="#0a0c10" strokeWidth={1.5} />} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  );
};

function GroupTeamRow({ teamId, name, placeholder, muted = false, hltvIdByTeamId }) {
  const id = Number(teamId);
  const known = Number.isFinite(id) && id > 0;
  return (
    <div className={`playoff-team-row${muted ? " muted" : ""}`}>
      {known ? (
        <TeamLogo hltvTeamId={hltvIdByTeamId[id]} name={name} size={24} />
      ) : (
        <span className="playoff-team-badge empty">?</span>
      )}
      {known ? <span>{name}</span> : <span className="playoff-team-tbd">{placeholder || "TBD"}</span>}
    </div>
  );
}

function GroupMatchCard({ title, meta, rows, places }) {
  return (
    <div className="playoff-match-card">
      <div className="playoff-match-head">
        <strong>{title}</strong>
        <span>{meta}</span>
      </div>
      <div className="playoff-match-teams">{rows}</div>
      {places && places.length > 0 && (
        <div className="playoff-match-foot">
          {places.map((p) => (
            <span className={`playoff-place ${p.kind}`} key={p.text}>
              {p.text}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// One resolved outcome drawn as a bracket (the Playoff tab's card + connector
// system): every match card shows both teams, the winner marked, the loser
// muted, and the highlighted team's matches carry an orange bar. Double
// elimination draws the upper and lower brackets; knock-outs one ladder plus
// the third-place decider below it.
function OutcomeBracketVisual({ bracket, teamLookup, teams, highlightTeamId }) {
  if (!bracket) return null;
  const hltvIdByTeamId = {};
  (teams || []).forEach((t) => {
    hltvIdByTeamId[Number(t.team_id)] = t.hltv_team_id;
  });
  const name = (id) => teamLookup[id] || String(id);
  const row = (tid, won, key) => (
    <div
      className={`playoff-team-row outcome-row${won ? " winner" : " loser"}${Number(tid) === Number(highlightTeamId) ? " mine" : ""}`}
      key={key}
    >
      <TeamLogo hltvTeamId={hltvIdByTeamId[Number(tid)]} name={name(tid)} size={24} />
      <span>{name(tid)}</span>
      {won && <span className="outcome-row-mark">W</span>}
    </div>
  );
  const card = (m, title, meta) => {
    const [a, b] = Array.isArray(m.teams) && m.teams.length === 2 ? m.teams : [m.winner, m.loser];
    return (
      <GroupMatchCard
        title={title}
        meta={meta}
        rows={[row(a, Number(a) === Number(m.winner), "a"), row(b, Number(b) === Number(m.winner), "b")]}
      />
    );
  };
  if (bracket.grand_final) {
    const columns = (rounds) => (
      <div className="pb-bracket">
        {rounds.map(([stage, keys, straight]) => {
          const matches = bracket[stage] || [];
          return (
            <div className={`pb-round${straight ? " straight" : ""}`} key={stage}>
              <div className="pb-round-title">{STAGE_FULL[stage] || stage}</div>
              <div className="pb-matches">
                {keys.map((key, i) => {
                  const m = matches.find((x) => x.key === key) || matches[i];
                  if (!m) return null;
                  return (
                    <div className="pb-match-wrap" key={key}>
                      {card(m, DOUBLE8_MATCH_TITLE[key] || key, key === "grand_final" ? "BO5" : "BO3")}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    );
    return (
      <div className="group-brackets">
        <div>
          <div className="group-bracket-title">Upper bracket</div>
          {columns(DOUBLE8_UPPER_ROUNDS)}
        </div>
        <div>
          <div className="group-bracket-title">Lower bracket</div>
          {columns(DOUBLE8_LOWER_ROUNDS)}
        </div>
      </div>
    );
  }
  const rounds = Object.keys(bracket).filter((k) => k !== "third_place" && (bracket[k] || []).length > 0);
  const third = (bracket.third_place || [])[0];
  return (
    <div className="group-brackets">
      <div>
        <div className="pb-bracket">
          {rounds.map((stage, ri) => (
            <div className="pb-round" key={stage}>
              <div className="pb-round-title">{STAGE_FULL[stage] || stage}</div>
              <div className="pb-matches">
                {(bracket[stage] || []).map((m, i) => (
                  <div className="pb-match-wrap" key={`${stage}-${i}`}>
                    {card(m, (bracket[stage] || []).length === 1 ? STAGE_FULL[stage] || stage : `${STAGE_SHORT[stage] || stage} ${i + 1}`, stage === "final" ? "BO5" : "BO3")}
                  </div>
                ))}
                {third && (
                  <div className={`pb-match-wrap pb-aux${ri === rounds.length - 1 ? "" : " pb-spacer"}`} aria-hidden={ri !== rounds.length - 1}>
                    {card(third, "3rd place", "BO3")}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function GroupBracketVisual({ format, slotIds, nameOf, hltvIdByTeamId, combined, bo = 3 }) {
  const meta = `BO${bo}`;
  const seed = (i, key) => {
    const v = slotIds[i];
    const id = v && v !== "unknown" ? Number(v) : 0;
    return (
      <GroupTeamRow
        key={key}
        teamId={id}
        name={id ? nameOf(v) : ""}
        placeholder={id ? undefined : v === "unknown" ? "Unknown team" : `Seed ${i + 1}`}
        hltvIdByTeamId={hltvIdByTeamId}
      />
    );
  };
  const ph = (text, key) => <GroupTeamRow key={key} teamId={0} placeholder={text} muted hltvIdByTeamId={hltvIdByTeamId} />;
  const win = (text) => ({ kind: "win", text });
  const drop = (text) => ({ kind: "drop", text });
  const lose = (text) => ({ kind: "lose", text });

  let upper;
  let lower;
  if (format === "de8_top3" || format === "de8") {
    const top3 = format === "de8_top3";
    upper = [
      {
        title: "Round 1",
        matches: [0, 1, 2, 3].map((m) => ({
          key: `ub${m}`,
          title: `UB ${m + 1}`,
          rows: [seed(2 * m, "a"), seed(2 * m + 1, "b")],
          places: [drop("loser → lower round 1")],
        })),
      },
      {
        title: "Upper semis",
        matches: [0, 1].map((m) => ({
          key: `us${m}`,
          title: `UB SF ${m + 1}`,
          rows: [ph(`Winner UB ${2 * m + 1}`, "a"), ph(`Winner UB ${2 * m + 2}`, "b")],
          places: top3
            ? [drop("loser → lower semis")]
            : [win(combined ? "winner → playoffs (1st / 2nd)" : "winner qualifies (1st / 2nd)"), drop("loser → lower round 2")],
        })),
      },
    ];
    if (top3) {
      upper.push({
        title: "Upper final",
        matches: [
          {
            key: "uf",
            title: "UB Final",
            rows: [ph("Winner UB SF 1", "a"), ph("Winner UB SF 2", "b")],
            places: [
              win(combined ? "1st · bye to the playoff semis" : "1st · qualifies"),
              win(combined ? "2nd · playoff quarter-finals" : "2nd · qualifies"),
            ],
          },
        ],
      });
    }
    lower = [
      {
        title: "Lower round 1",
        matches: [0, 1].map((m) => ({
          key: `lb${m}`,
          title: `LB ${m + 1}`,
          rows: [ph(`Loser UB ${2 * m + 1}`, "a"), ph(`Loser UB ${2 * m + 2}`, "b")],
          places: [lose("loser out")],
        })),
      },
      {
        // Cross-over: each upper-semi loser meets the OTHER side's lower-round
        // winner. Rows are ordered so the straight connector from LB m lands
        // on the card it actually feeds.
        title: top3 ? "Lower semis" : "Lower round 2",
        straight: true,
        matches: [0, 1].map((m) => ({
          key: `ls${m}`,
          title: top3 ? `LB SF ${m + 1}` : `LB ${m + 3}`,
          rows: [ph(`Loser UB SF ${2 - m}`, "a"), ph(`Winner LB ${m + 1}`, "b")],
          places: top3
            ? [lose("loser out")]
            : [win(combined ? "winner → playoffs (3rd / 4th)" : "winner qualifies (3rd / 4th)"), lose("loser out")],
        })),
      },
    ];
    if (top3) {
      lower.push({
        title: "Lower final",
        matches: [
          {
            key: "lf",
            title: "LB Final",
            rows: [ph("Winner LB SF 1", "a"), ph("Winner LB SF 2", "b")],
            places: [win(combined ? "3rd · playoff quarter-finals" : "3rd · qualifies"), lose("loser out (4th)")],
          },
        ],
      });
    }
  } else {
    upper = [
      {
        title: "Opening",
        matches: [0, 1].map((m) => ({
          key: `ub${m}`,
          title: `Opening ${m + 1}`,
          rows: [seed(2 * m, "a"), seed(2 * m + 1, "b")],
          places: [drop("loser → elimination match")],
        })),
      },
      {
        title: "Winners' match",
        matches: [
          {
            key: "wm",
            title: "Winners",
            rows: [ph("Winner Opening 1", "a"), ph("Winner Opening 2", "b")],
            places: [win(combined ? "1st · to the playoffs" : "1st · qualifies"), drop("loser → decider")],
          },
        ],
      },
    ];
    lower = [
      {
        title: "Elimination match",
        matches: [
          {
            key: "em",
            title: "Elimination",
            rows: [ph("Loser Opening 1", "a"), ph("Loser Opening 2", "b")],
            places: [lose("loser out (4th)")],
          },
        ],
      },
      {
        title: "Decider",
        straight: true,
        matches: [
          {
            key: "dc",
            title: "Decider",
            rows: [ph("Loser Winners' match", "a"), ph("Winner Elimination", "b")],
            places: [win(combined ? "2nd · to the playoffs" : "2nd · qualifies"), lose("loser out (3rd)")],
          },
        ],
      },
    ];
  }

  const bracket = (rounds) => (
    <div className="pb-bracket">
      {rounds.map((round, r) => (
        <div className={`pb-round${round.straight ? " straight" : ""}`} key={`${round.title}-${r}`}>
          <div className="pb-round-title">{round.title}</div>
          <div className="pb-matches">
            {round.matches.map((m) => (
              <div className="pb-match-wrap" key={m.key}>
                <GroupMatchCard title={m.title} meta={meta} rows={m.rows} places={m.places} />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
  return (
    <div className="group-brackets">
      <div>
        <div className="group-bracket-title">Upper bracket</div>
        {bracket(upper)}
      </div>
      <div>
        <div className="group-bracket-title">Lower bracket</div>
        {bracket(lower)}
      </div>
    </div>
  );
}

// Playoff bracket skeleton for a combined event: the bracket's shape with
// placeholder rows naming the group place that feeds each slot, in the same
// card + connector system as the Playoff tab. Mirrors the simulator's
// templates: the top-3 shape is the 6-team byes bracket; other shapes flatten
// qualifiers seed-major and pair i vs N-1-i, with stopTeams ending early.
function PlayoffSkeleton({ format, groupCount, qualsPerGroup, stopTeams = 1, bo = null, thirdPlace = false, layout = "cross" }) {
  const boDefault = Number(bo?.default || 3);
  const boFinal = Number(bo?.grandFinal || boDefault);
  const boThird = Number(bo?.thirdPlace || boDefault);
  const empty = {};
  const ph = (text, key, muted = true) => (
    <GroupTeamRow key={key} teamId={0} placeholder={text} muted={muted} hltvIdByTeamId={empty} />
  );
  const ordinal = (r) => ({ 1: "1st", 2: "2nd", 3: "3rd" }[r] || `${r}th`);
  const seedLabel = (g, rank) => `Group ${g + 1} ${ordinal(rank + 1)}`;
  let rounds;
  if (format === "de8_top3") {
    // Presented as HLTV lays it out: QF1's winner meets Group 2's winner,
    // QF2's winner meets Group 1's winner, so the connectors read straight.
    rounds = [
      {
        title: "Quarter-finals",
        matches: [
          { key: "qf1", title: "QF 1", meta: `BO${boDefault}`, rows: [ph(seedLabel(0, 1), "a", false), ph(seedLabel(1, 2), "b", false)] },
          { key: "qf2", title: "QF 2", meta: `BO${boDefault}`, rows: [ph(seedLabel(1, 1), "a", false), ph(seedLabel(0, 2), "b", false)] },
        ],
      },
      {
        title: "Semi-finals",
        straight: true,
        matches: [
          { key: "sf1", title: "SF 1", meta: `BO${boDefault}`, rows: [ph(`${seedLabel(1, 0)} (bye)`, "a", false), ph("Winner QF 1", "b")] },
          { key: "sf2", title: "SF 2", meta: `BO${boDefault}`, rows: [ph(`${seedLabel(0, 0)} (bye)`, "a", false), ph("Winner QF 2", "b")] },
        ],
      },
      {
        title: "Grand final",
        matches: [{ key: "f", title: "Final", meta: `BO${boFinal}`, rows: [ph("Winner SF 1", "a"), ph("Winner SF 2", "b")] }],
      },
    ];
  } else if (format === "gsl4" && groupCount === 2 && qualsPerGroup === 2 && stopTeams === 1) {
    // The organiser's choice (events.playoff_layout): cross or same-group semis.
    const sf = layout === "same_group" ? [[[0, 0], [0, 1]], [[1, 0], [1, 1]]] : [[[0, 0], [1, 1]], [[1, 0], [0, 1]]];
    rounds = [
      {
        title: "Semi-finals",
        matches: sf.map(([a, b], m) => ({
          key: `sf${m + 1}`, title: `SF ${m + 1}`, meta: `BO${boDefault}`,
          rows: [ph(seedLabel(...a), "a", false), ph(seedLabel(...b), "b", false)],
        })),
      },
      {
        title: "Grand final",
        matches: [{ key: "f", title: "Final", meta: `BO${boFinal}`, rows: [ph("Winner SF 1", "a"), ph("Winner SF 2", "b")] }],
      },
    ];
  } else if (format === "gsl4" && groupCount === 4 && qualsPerGroup === 2 && stopTeams === 1) {
    // As HLTV ran Stake Pulse Beat II: A1 v D2, C1 v B2, B1 v C2, D1 v A2,
    // the semis pairing QF1 with QF2 and QF3 with QF4 (groups.py's layout).
    const qf = [[[0, 0], [3, 1]], [[2, 0], [1, 1]], [[1, 0], [2, 1]], [[3, 0], [0, 1]]];
    rounds = [
      {
        title: "Quarter-finals",
        matches: qf.map(([a, b], m) => ({
          key: `qf${m + 1}`, title: `QF ${m + 1}`, meta: `BO${boDefault}`,
          rows: [ph(seedLabel(...a), "a", false), ph(seedLabel(...b), "b", false)],
        })),
      },
      {
        title: "Semi-finals",
        matches: [
          { key: "sf1", title: "SF 1", meta: `BO${boDefault}`, rows: [ph("Winner QF 1", "a"), ph("Winner QF 2", "b")] },
          { key: "sf2", title: "SF 2", meta: `BO${boDefault}`, rows: [ph("Winner QF 3", "a"), ph("Winner QF 4", "b")] },
        ],
      },
      {
        title: "Grand final",
        matches: [{ key: "f", title: "Final", meta: `BO${boFinal}`, rows: [ph("Winner SF 1", "a"), ph("Winner SF 2", "b")] }],
      },
    ];
  } else {
    const seeded = [];
    for (let r = 0; r < qualsPerGroup; r += 1) for (let g = 0; g < groupCount; g += 1) seeded.push([g, r]);
    const n = seeded.length;
    const total = Math.max(1, Math.round(Math.log2(n)) - Math.round(Math.log2(Math.max(1, stopTeams))));
    const titleFor = (teamsIn) => ({ 2: "Grand final", 4: "Semi-finals", 8: "Quarter-finals", 16: "Round of 16" }[teamsIn] || `Round of ${teamsIn}`);
    const shortFor = (teamsIn) => ({ 2: "F", 4: "SF", 8: "QF", 16: "R16" }[teamsIn] || `R${teamsIn}`);
    rounds = [];
    for (let r = 0; r < total; r += 1) {
      const teamsIn = n >> r;
      const matches = n >> (r + 1);
      const feeder = r > 0 ? shortFor(n >> (r - 1)) : "";
      rounds.push({
        title: titleFor(teamsIn),
        matches: Array.from({ length: matches }, (_, m) => ({
          key: `r${r}m${m}`,
          title: teamsIn === 2 ? "Final" : `${shortFor(teamsIn)} ${m + 1}`,
          meta: `BO${teamsIn === 2 ? boFinal : boDefault}`,
          rows:
            r === 0
              ? [ph(seedLabel(...seeded[m]), "a", false), ph(seedLabel(...seeded[n - 1 - m]), "b", false)]
              : [ph(`Winner ${feeder} ${2 * m + 1}`, "a"), ph(`Winner ${feeder} ${2 * m + 2}`, "b")],
        })),
      });
    }
  }
  return (
    <div className="group-brackets">
      <div>
        <div className="pb-bracket">
          {rounds.map((round, r) => (
            <div className={`pb-round${round.straight ? " straight" : ""}`} key={`${round.title}-${r}`}>
              <div className="pb-round-title">{round.title}</div>
              <div className="pb-matches">
                {round.matches.map((m) => (
                  <div className="pb-match-wrap" key={m.key}>
                    <GroupMatchCard title={m.title} meta={m.meta} rows={m.rows} />
                  </div>
                ))}
                {thirdPlace && (
                  <div className={`pb-match-wrap pb-aux${r === rounds.length - 1 ? "" : " pb-spacer"}`} aria-hidden={r !== rounds.length - 1}>
                    <GroupMatchCard title="3rd place" meta={`BO${boThird}`} rows={[ph("Loser SF 1", "a"), ph("Loser SF 2", "b")]} />
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// Stage formats the path engine values (services/paths_engine): the stage tab
// shows a format-specific view, everything else on the page is shared.
const PATH_FORMATS = ["swiss", "bracket"];

// Bracket stage: the most likely bracket of the stored run.
function BracketStageView({ results, teams, teamLookup, draw = null }) {
  const br = results?.bracket || null;
  if (!br) {
    // The draw as HLTV shows it before every team is placed (the stored
    // provisional bracket): the picker board, read-only, TBD where a slot
    // is still open.
    if (draw && (draw.matches || []).length > 0) {
      const slots = draw.team_slots || [];
      const known = slots.filter((t) => Number(t) > 0).length;
      return (
        <div className="card sub group-card">
          <div className="group-card-head">
            <h3>Bracket</h3>
            <span className="muted">
              {known} of {slots.length} teams
            </span>
          </div>
          <BracketPicker bracket={draw} teamLookup={teamLookup} picks={{}} onPick={() => {}} readOnly />
        </div>
      );
    }
    return (
      <div className="card sub">
        <p className="muted">{PUBLIC_BUILD ? NOT_PUBLISHED : "Not baked yet."}</p>
      </div>
    );
  }
  return (
    <div className="card sub group-card">
      <div className="group-card-head">
        <h3>Most Likely Bracket</h3>
        <span className="muted">{(Number(br.best_probability || 0) * 100).toFixed(2)}%</span>
      </div>
      <OutcomeBracketVisual bracket={br.best} teamLookup={teamLookup} teams={teams} />
    </div>
  );
}

// Completed-bracket picker: every match of the stored bracket in play order,
// each a pick between the two teams its earlier picks feed into it.
function BracketPicker({ bracket, teamLookup, picks, onPick, readOnly = false }) {
  const matches = bracket?.matches || [];
  const stages = bracket?.stages || [];
  const slots = (bracket?.team_slots || []).map(Number);
  const winners = {};
  const losers = {};
  const resolved = matches.map(([key, stage, fa, fb]) => {
    const side = (f) => (f[0] === "seed" ? slots[Number(f[1])] : f[0] === "win" ? winners[String(f[1])] : losers[String(f[1])]) || null;
    const a = side(fa);
    const b = side(fb);
    const picked = Number(picks[key] || 0);
    const w = picked && (picked === a || picked === b) ? picked : null;
    if (w) {
      winners[key] = w;
      losers[key] = w === a ? b : a;
    }
    const label = (f) => (f[0] === "seed" ? "TBD" : `${f[0] === "win" ? "Winner" : "Loser"} of ${DOUBLE8_MATCH_TITLE[f[1]] || f[1]}`);
    return { key, stage, a, b, w, pa: label(fa), pb: label(fb) };
  });
  const byStage = {};
  resolved.forEach((m) => {
    (byStage[m.stage] = byStage[m.stage] || []).push(m);
  });
  const titleOf = (m, i, s) => DOUBLE8_MATCH_TITLE[m.key] || (byStage[s.key].length === 1 ? s.full : `${s.label} ${i + 1}`);
  const columns = stages
    .filter((s) => !s.pad && byStage[s.key])
    .map((s) => ({ key: s.key, title: s.full, matches: byStage[s.key].map((m, i) => ({ ...m, title: titleOf(m, i, s) })) }));
  return <PickerBoard columns={columns} teamLookup={teamLookup} onPick={onPick} readOnly={readOnly} />;
}

// The completed-stage picker board every format draws: one column per
// round, one card per match (its title, the two sides as buttons, the
// picked winner lit), unknown sides as disabled placeholders.
function PickerBoard({ columns, teamLookup, onPick, readOnly = false }) {
  return (
    <div className="pb-bracket">
      {columns.map((col) => (
        <div className="pb-round" key={col.key}>
          <div className="pb-round-title">{col.title}</div>
          <div className="pb-matches">
            {col.matches.map((m) => (
              <div className="pb-match-wrap" key={m.key}>
                <div className="gsl-match">
                  <span className="gsl-label">{m.title}</span>
                  <span className="gsl-pick">
                    {[
                      [m.a, m.pa],
                      [m.b, m.pb],
                    ].map(([tid, placeholder], rowIdx) => (
                      <button
                        key={rowIdx}
                        disabled={!tid || readOnly}
                        className={tid && m.w === tid ? "active" : ""}
                        onClick={() => tid && !readOnly && onPick(m.key, tid)}
                      >
                        {tid ? teamLookup[tid] || tid : placeholder}
                      </button>
                    ))}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// Completed Swiss board, laid out the way HLTV draws a Swiss stage: one
// column per round with the record pools stacked inside it (1:0 above 0:1),
// each match a logo-vs-logo card (click a side to make it the winner), the
// rounds still to come as unknown cards, and the finishing records on the
// right: qualified in green, eliminated in red, filled as teams get there.
function SwissBoard({ walk, teams, teamLookup, picks, onPick, readOnly = false, onOpen = null }) {
  const hltvIdByTeamId = {};
  (teams || []).forEach((t) => {
    hltvIdByTeamId[Number(t.team_id)] = t.hltv_team_id;
  });
  const name = (tid) => teamLookup[tid] || String(tid);
  const rec = (pool) => String(pool || "").replace("-", ":");
  const poolOrder = (a, b) => {
    const [aw, al] = String(a).split("-").map(Number);
    const [bw, bl] = String(b).split("-").map(Number);
    return bw - aw || al - bl;
  };
  // Read-only with onOpen: a known match opens (the whole card is the
  // target, so its sides stay enabled and let the click through).
  const openable = readOnly && typeof onOpen === "function";
  const side = (tid, m, known) => {
    const picked = Number(picks[m.key] || 0);
    const cls = known && picked ? (picked === Number(tid) ? " won" : " lost") : "";
    return (
      <button
        key={`${m.key}-${tid ?? "tbd"}`}
        type="button"
        className={`sw-side${cls}`}
        disabled={!known || (readOnly && !openable)}
        tabIndex={openable ? -1 : undefined}
        title={known ? name(tid) : "TBD"}
        onClick={() => known && !readOnly && onPick(m.key, tid)}
      >
        {known ? <TeamLogo hltvTeamId={hltvIdByTeamId[Number(tid)]} name={name(tid)} size={34} /> : <span className="sw-unknown">?</span>}
        <span className="sw-side-name">{known ? name(tid) : ""}</span>
      </button>
    );
  };
  const columns = [];
  (walk?.rounds || []).forEach((rd) => {
    const byPool = {};
    rd.matches.forEach((m) => {
      (byPool[m.pool] = byPool[m.pool] || []).push(m);
    });
    columns.push({
      key: `r${rd.round}`,
      title: `Round ${rd.round}`,
      pools: Object.keys(byPool)
        .sort(poolOrder)
        .map((pool) => ({ pool, matches: byPool[pool].map((m) => ({ key: m.key, a: m.a, b: m.b, known: true, status: m.status || null, start_at: m.start_at || null, maps_a: m.maps_a, maps_b: m.maps_b, map_a: m.map_a, map_b: m.map_b, score: m.score || null })) })),
    });
  });
  (walk?.placeholders || []).forEach((ph) => {
    const pending = (ph.pools || []).map((p) => ({
      pool: p.pool,
      matches: Array.from({ length: p.matches }, (_, i) => ({ key: `r${ph.round}_${p.pool}_${i}`, a: null, b: null, known: false })),
    }));
    // a round already partly played keeps one column: its unplayed slots
    // join the played pools (or stand as their own pool), in record order
    const col = columns.find((c) => c.key === `r${ph.round}`);
    if (col) {
      pending.forEach((pp) => {
        const existing = col.pools.find((x) => x.pool === pp.pool);
        if (existing) existing.matches.push(...pp.matches);
        else col.pools.push(pp);
      });
      col.pools.sort((x, y) => poolOrder(x.pool, y.pool));
    } else {
      columns.push({ key: `r${ph.round}`, title: `Round ${ph.round}`, pools: pending });
    }
  });
  const records = walk?.records || {};
  const finalSlots = walk?.final_slots || {};
  const finished = (pool) => {
    const [w, l] = String(pool).split("-").map(Number);
    return Object.keys(records).filter((tid) => Number(records[tid][0]) === w && Number(records[tid][1]) === l);
  };
  const finalRows = (pools) =>
    pools
      .filter((pool) => finalSlots[pool])
      .map((pool) => {
        const done = finished(pool);
        const empty = Math.max(0, Number(finalSlots[pool] || 0) - done.length);
        return (
          <div className="sw-final-row" key={pool}>
            <span className="sw-final-rec">{rec(pool)}</span>
            <span className="sw-final-teams">
              {done.map((tid) => (
                <span className="sw-final-team" key={tid} title={name(tid)}>
                  <TeamLogo hltvTeamId={hltvIdByTeamId[Number(tid)]} name={name(tid)} size={30} />
                </span>
              ))}
              {Array.from({ length: empty }, (_, k) => (
                <span className="sw-unknown" key={`e-${k}`}>
                  ?
                </span>
              ))}
            </span>
          </div>
        );
      });
  return (
    <div className={`sw-board${readOnly ? " readonly" : ""}`}>
      {columns.map((col) => (
        <div className="sw-col" key={col.key}>
          <div className="sw-col-title">{col.title}</div>
          <div className="sw-col-body">
            {col.pools.map((p) => (
              <div className="sw-pool" key={`${col.key}-${p.pool}`}>
                <div className="sw-pool-title">{rec(p.pool)}</div>
                {p.matches.map((m) => (
                  <div
                    className={`sw-match${m.status === "live" ? " live" : ""}${openable && m.known ? " openable" : ""}`}
                    key={m.key}
                    onClick={() => openable && m.known && onOpen(m)}
                  >
                    {side(m.a, m, m.known)}
                    {m.status === "live" ? (
                      <span className="sw-vs sw-live">
                        <span>{m.maps_a != null ? `${m.maps_a}–${m.maps_b}` : "live"}</span>
                        {m.map_a != null && m.map_b != null && (
                          <span className="sw-map">
                            {m.map_a}–{m.map_b}
                          </span>
                        )}
                      </span>
                    ) : m.score ? (
                      <span className="sw-vs sw-score">{String(m.score).replace("-", "–")}</span>
                    ) : m.status === "upcoming" && m.start_at ? (
                      <span className="sw-vs sw-time">{new Date(Number(m.start_at) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                    ) : (
                      <span className="sw-vs">vs</span>
                    )}
                    {side(m.b, m, m.known)}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      ))}
      <div className="sw-col sw-final">
        <div className="sw-col-title">Final</div>
        <div className="sw-col-body">
          <div className="sw-final-block qualified">{finalRows(["3-0", "3-1", "3-2"])}</div>
          <div className="sw-final-block eliminated">{finalRows(["2-3", "1-3", "0-3"])}</div>
        </div>
      </div>
    </div>
  );
}

// Swiss stage (a group format of the groups engine): the seed order with
// each team's record odds and qualify chance from the stored run.
function SwissStageView({ results, teams, teamLookup, excludeSet = null, teamPlayerIds = null, onToggleTeam = null }) {
  const sw = results?.swiss || null;
  // The simulator: the board starts from the results played so far; pick
  // winners and the engine draws the rounds that follow (the same board and
  // route as the Completed Swiss picker on the Top 5 tab).
  const simId = Number(sw?.event_id || results?.event_id || 0) || null;
  const [simPicks, setSimPicks] = useState({});
  const [simWalk, setSimWalk] = useState(null);
  const simQuery = async (body) => {
    try {
      const d = await api.post("/groups/swiss/pairings", body, 30000);
      if (d && Array.isArray(d.rounds)) {
        setSimWalk(d);
        if (d.picks) setSimPicks(d.picks);
      }
    } catch {
      /* the board keeps what it has */
    }
  };
  useEffect(() => {
    if (!simId) return undefined;
    setSimPicks({});
    setSimWalk(null);
    simQuery({ actual: true });
    return undefined;
  }, [simId, sw?.seed]);
  const simPick = (key, tid) => {
    const round = Number(String(key).match(/^r(\d+)_/)?.[1] || 0);
    const next = {};
    Object.entries(simPicks).forEach(([k, v]) => {
      const r = Number(String(k).match(/^r(\d+)_/)?.[1] || 0);
      if (r <= round && k !== key) next[k] = v;
    });
    next[key] = tid;
    setSimPicks(next);
    simQuery({ swiss_picks: next });
  };
  if (!sw) {
    return (
      <div className="card sub">
        <p className="muted">{PUBLIC_BUILD ? NOT_PUBLISHED : "Not baked yet."}</p>
      </div>
    );
  }
  const order = (results.groups?.[0] || []).map(Number);
  const teamInfo = (tid) => (teams || []).find((t) => Number(t.team_id) === Number(tid));
  const pct = (v) => `${(Number(v || 0) * 100).toFixed(1)}%`;
  const wins = ["3-0", "3-1", "3-2"];
  const losses = ["2-3", "1-3", "0-3"];
  // qualify odds: the engine's advance share for the team, else the three
  // winning records summed (older runs stored them under swiss.qualify)
  const qualifyOf = (tid) => {
    const adv = results.paths_summary?.advance?.[String(tid)];
    if (adv !== undefined && adv !== null) return Number(adv);
    if (sw.qualify?.[String(tid)] !== undefined) return Number(sw.qualify[String(tid)]);
    const rec = sw.records?.[String(tid)] || {};
    return wins.reduce((acc, r) => acc + Number(rec[r] || 0), 0);
  };
  return (
    <>
      {simWalk && (
        <div className="card sub group-card">
          <div className="group-card-head">
            <h3>Swiss Simulator</h3>
          </div>
          <SwissBoard walk={simWalk} teams={teams} teamLookup={teamLookup} picks={simPicks} onPick={simPick} />
          <div className="actions">
            <button className="secondary" onClick={() => simQuery({ actual: true })}>
              Actual results
            </button>
            <button className="secondary" onClick={() => simQuery({ swiss_picks: simPicks, favourites: true })}>
              Favourites win
            </button>
            <button className="secondary" onClick={() => simQuery({ swiss_picks: {} })}>
              Clear
            </button>
          </div>
        </div>
      )}
      <div className="card sub group-card">
      <div className="group-card-head">
        <h3>{sw.stage_name && sw.stage_name !== "SixteenEight" ? sw.stage_name : "Swiss"}</h3>
      </div>
      <table className="swiss-seed-table">
        <thead>
          <tr>
            <th className="num">#</th>
            <th>Team</th>
            {wins.map((r) => (
              <th className="num" key={r}>
                {r}
              </th>
            ))}
            <th className="num strong">Qualify</th>
            {losses.map((r) => (
              <th className="num" key={r}>
                {r}
              </th>
            ))}
            {onToggleTeam && <th />}
          </tr>
        </thead>
        <tbody>
          {order.map((tid, i) => {
            const rec = sw.records?.[String(tid)] || {};
            const info = teamInfo(tid);
            return (
              <tr key={tid}>
                <td className="num muted">{sw.seeds?.[String(tid)] ?? i + 1}</td>
                <td className="team">
                  <TeamLogo hltvTeamId={info?.hltv_team_id} name={teamLookup[tid]} size={18} />
                  <span>{teamLookup[tid] || tid}</span>
                </td>
                {wins.map((r) => (
                  <td className="num" key={r}>
                    {pct(rec[r])}
                  </td>
                ))}
                <td className="num strong">{pct(qualifyOf(tid))}</td>
                {losses.map((r) => (
                  <td className="num" key={r}>
                    {pct(rec[r])}
                  </td>
                ))}
                {onToggleTeam &&
                  (() => {
                    const pids = teamPlayerIds ? teamPlayerIds(tid) : [];
                    const out = pids.length > 0 && pids.every((p) => excludeSet?.has(p));
                    return (
                      <td className="num">
                        <button className={`filter-toggle out${out ? " on" : ""}`} onClick={() => onToggleTeam(tid)}>
                          {out ? "Excluded" : "Exclude"}
                        </button>
                      </td>
                    );
                  })()}
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
    </>
  );
}

function GroupsTab({
  teams,
  teamLookup,
  players,
  refresh,
  // "groups" (the GSL / double-elimination groups) or "swiss": the stage the
  // stored run models; the Top 5, Player Value and Point Sources are shared.
  stageKind = "groups",
  groupVariant = null,
  // From the kind detector: does this ONE fantasy game also score the playoff
  // bracket on the same event page (no separate "Playoffs - ..." fantasy
  // event)? null = unknown. playoffDetected = that bracket's size/byes.
  combinedDetected = null,
  playoffDetected = null,
  // Whether the backend has a verified playoff seeding for this shape; when
  // not, the shape is refused and flagged for development.
  combinedSupported = false,
  combinedShape = "",
  // Best-of for group matches from the event's formats table (default Bo3).
  groupBo = 3,
  // Playoff best-of rules { default, grandFinal, thirdPlace } for the bracket skeleton.
  playoffBo = null,
  // The event page lists a third-place decider for the playoff.
  playoffThirdPlace = false,
  // Semi-final layout of a 2-group GSL playoff ("cross" | "same_group"), stored per event.
  playoffLayoutDetected = "cross",
}) {
  const [groupsTab, setGroupsTab] = useState("stage");
  const [events, setEvents] = useState([]);
  const [selectedEventId, setSelectedEventId] = useState("");
  const [eventTeamNames, setEventTeamNames] = useState(new Set());
  const [groupCount, setGroupCount] = useState(2);
  const [groupFormat, setGroupFormat] = useState("gsl4");
  const seedsPerGroup = groupFormat.startsWith("de8") ? 8 : 4;
  const qualsPerGroup = { gsl4: 2, de8: 4, de8_top3: 3 }[groupFormat] || 2;
  const [combinedPlayoffs, setCombinedPlayoffs] = useState(false);
  const [playoffStopTeams, setPlayoffStopTeams] = useState("1");
  const [playoffLayout, setPlayoffLayout] = useState(playoffLayoutDetected || "cross");
  useEffect(() => {
    setPlayoffLayout(playoffLayoutDetected || "cross");
  }, [playoffLayoutDetected]);
  const [layoutBusy, setLayoutBusy] = useState(false);
  const changePlayoffLayout = async (value) => {
    const prev = playoffLayout;
    setPlayoffLayout(value);
    setLayoutBusy(true);
    try {
      await api.post(`/events/${selectedEventId}/playoff-layout`, { layout: value }, 600000);
      await loadLatest();
    } catch (e) {
      setPlayoffLayout(prev);
      notify?.(`Could not set the layout: ${e?.message || "unknown error"}`);
    } finally {
      setLayoutBusy(false);
    }
  };
  const [slots, setSlots] = useState(Array(8).fill(""));
  const [results, setResults] = useState(null);
  const [updatedAt, setUpdatedAt] = useState("");
  // Combined playoffs run only for shapes whose playoff seeding has been
  // verified against a real HLTV bracket (backend allowlist): today that is
  // 2 groups of 8 with top 3 -> 6-team bracket. Anything else is refused and
  // flagged for development rather than simulated under a guessed seeding.
  const combinedAllowed = (groupFormat === "de8_top3" && groupCount === 2) || (groupFormat === "gsl4" && (groupCount === 2 || groupCount === 4));
  // Detection says this one fantasy game scores the playoffs too: default the
  // toggle on, unless a stored simulation already made a choice (loadLatest
  // overwrites it from the saved payload right after).
  useEffect(() => {
    if (combinedDetected && combinedSupported && !results) {
      setCombinedPlayoffs(true);
      if (groupFormat === "de8_top3") setPlayoffStopTeams("1");
    }
  }, [combinedDetected, combinedSupported, results, groupFormat]);
  const [busy, setBusy] = useState(false);
  const [runMessage, setRunMessage] = useState("");
  const [runProgress, setRunProgress] = useState({ done: 0, total: 0 });
  const [autofillBusy, setAutofillBusy] = useState(false);
  const [autofillMessage, setAutofillMessage] = useState("");
  const [comboMode, setComboMode] = useState("average");
  // Top 5 sub-tabs mirror the Playoff tab: the three combo modes plus the
  // completed-bracket picker (which used to be its own top-level tab).
  const [groupsTopSubtab, setGroupsTopSubtab] = useState("average");
  const groupsTopSubtabs = [
    { key: "average", label: "Average Player Value" },
    { key: "single_outcome", label: "Best Single Outcome" },
    { key: "most_outcomes", label: "Most Likely Winner" },
    { key: "completed", label: stageKind === "bracket" ? "Completed Bracket" : stageKind === "swiss" ? "Completed Swiss" : "Completed Groups" },
  ];
  const setGroupsTopMode = (key) => {
    setGroupsTopSubtab(key);
    if (key !== "completed") setComboMode(key);
  };
  // Team logos for the bracket cards (same lookup the Playoff tab uses).
  const hltvIdByTeamId = useMemo(() => {
    const m = {};
    (teams || []).forEach((t) => {
      if (t.hltv_team_id) m[Number(t.team_id)] = Number(t.hltv_team_id);
    });
    return m;
  }, [teams]);
  const [comboSearch, setComboSearch] = useState("");
  // Include / exclude filters for the Top 5 (player ids); sent with every query.
  const [includeSet, setIncludeSet] = useState(new Set());
  const [excludeSet, setExcludeSet] = useState(new Set());
  const toggleFilter = (pid, kind) => {
    const id = Number(pid);
    const flip = (setter, on) =>
      setter((prev) => {
        const next = new Set(prev);
        if (on) next.add(id);
        else next.delete(id);
        return next;
      });
    if (kind === "include") {
      const on = !includeSet.has(id);
      flip(setIncludeSet, on);
      if (on) flip(setExcludeSet, false);
    } else {
      const on = !excludeSet.has(id);
      flip(setExcludeSet, on);
      if (on) flip(setIncludeSet, false);
    }
  };
  const clearFilters = () => {
    setIncludeSet(new Set());
    setExcludeSet(new Set());
  };
  const [sortKey, setSortKey] = useState("ev_desc");
  const [topTeams, setTopTeams] = useState(null);
  const [allTeams, setAllTeams] = useState(null);
  const [filteredCount, setFilteredCount] = useState(0);
  const [page, setPage] = useState(0);
  const [combosReady, setCombosReady] = useState(false);
  const [liveMode, setLiveMode] = useState(false);
  const [combosUpdatedAt, setCombosUpdatedAt] = useState("");
  const [poolReduction, setPoolReduction] = useState(null);
  const [comboProgress, setComboProgress] = useState({ done: 0, total: 0 });
  const [topMessage, setTopMessage] = useState("");
  const [completedPicks, setCompletedPicks] = useState({});
  const [completedResult, setCompletedResult] = useState(null);
  const [completedMessage, setCompletedMessage] = useState("");
  const pollingRef = useRef(false);
  const querySeqRef = useRef(0);

  const normalizeTeamName = (name) => String(name || "").trim().toLowerCase();
  const filteredTeams = useMemo(() => {
    if (!selectedEventId || !eventTeamNames || eventTeamNames.size === 0) return [];
    return teams.filter((t) => eventTeamNames.has(normalizeTeamName(t.name)));
  }, [teams, selectedEventId, eventTeamNames]);
  // Fantasy pools often cover only part of a big field (e.g. 35 of 64 open
  // qualifier teams); missing opponents are filled with generic rank-250
  // unknowns, selectable as many times as needed.
  const teamOptions = useMemo(() => {
    const base = [
      { value: "", label: "Select team" },
      { value: "unknown", label: "Unknown team (rank 250)" },
      ...filteredTeams.map((t) => ({ value: String(t.team_id), label: t.name || `Team ${t.team_id}` })),
    ];
    // Slots restored from a stored run may reference materialized Unknown-N
    // teams (or other non-event teams); keep them displayable.
    const known = new Set(base.map((o) => o.value));
    const extras = [];
    slots.forEach((s) => {
      if (!s || s === "unknown" || known.has(String(s))) return;
      known.add(String(s));
      extras.push({ value: String(s), label: `${teamLookup[Number(s)] || `Team ${s}`} (${s})` });
    });
    return [...base, ...extras];
  }, [filteredTeams, slots, teamLookup]);

  const setSlot = (idx, val) => {
    setSlots((prev) => {
      const next = [...prev];
      next[idx] = val;
      return next;
    });
  };
  const groupSlots = useMemo(() => {
    const out = [];
    for (let g = 0; g < groupCount; g++) out.push(slots.slice(g * seedsPerGroup, g * seedsPerGroup + seedsPerGroup));
    return out;
  }, [slots, groupCount, seedsPerGroup]);

  const autofillFromHltv = async () => {
    if (PUBLIC_BUILD) return;
    setAutofillBusy(true);
    setAutofillMessage("");
    try {
      const data = await api.post(
        "/groups/autofill-from-hltv-event",
        { group_format: groupFormat === "de8_top3" ? "de8" : groupFormat },
        90000
      );
      if (data?.detail) {
        setAutofillMessage(String(data.detail));
        return;
      }
      const groups = data.groups || [];
      if (groups.length === 0) {
        setAutofillMessage("No groups found on the event page.");
        return;
      }
      const nextSlots = groups.flatMap((grp) => (grp.team_ids || []).map((id) => (id > 0 ? String(id) : "unknown")));
      setGroupCount(groups.length);
      setSlots(nextSlots);
      const tbd = nextSlots.filter((s) => s === "unknown").length;
      setAutofillMessage(
        `Filled ${groups.length} groups from HLTV${tbd ? ` (${tbd} undecided slots set to Unknown)` : ""}.`
      );
      if (refresh) await refresh();
    } catch (e) {
      setAutofillMessage(e?.message || "Autofill failed.");
    } finally {
      setAutofillBusy(false);
    }
  };
  const allSlotsFilled =
    slots.length === groupCount * seedsPerGroup && slots.every((s) => s === "unknown" || Number(s) > 0);
  const teamName = (id) => {
    if (id === "unknown") return "Unknown team";
    return teamLookup[Number(id)] || (id ? `Team ${id}` : "TBD");
  };

  const loadEvents = async (retriesLeft = 3) => {
    let data = null;
    try {
      data = await api.get("/events/");
    } catch {
      if (retriesLeft > 0) setTimeout(() => loadEvents(retriesLeft - 1), 5000);
      return;
    }
    if (data?.detail) return;
    const allEvents = Array.isArray(data.events) ? data.events : [];
    setEvents(allEvents);
    const active = data.active_event_id;
    setSelectedEventId(active == null ? "" : String(active));
  };
  const loadEventTeams = async (eventId) => {
    if (!eventId) {
      setEventTeamNames(new Set());
      return;
    }
    const data = await api.get(`/events/${eventId}`);
    if (data?.detail) return;
    setEventTeamNames(new Set((data.teams || []).map((t) => normalizeTeamName(t.team_name))));
  };
  const loadLatest = async () => {
    const data = await api.get("/groups/latest", 120000);
    if (!data?.exists) return false;
    const payload = data.payload || {};
    if (payload.group_format) {
      setGroupFormat(["de8", "de8_top3", "swiss", "bracket"].includes(payload.group_format) ? payload.group_format : "gsl4");
    }
    const savedGroups = payload.groups || [];
    if (savedGroups.length > 0) {
      setGroupCount(savedGroups.length);
      setSlots(savedGroups.flat().map((x) => String(x)));
    }
    setCombinedPlayoffs(Boolean(payload.combined_playoffs));
    setBakedInfo(payload.baked || null);
    setBracketDraw(payload.bracket || null);
    if (payload.playoff_stop_teams) setPlayoffStopTeams(String(payload.playoff_stop_teams));
    if (payload.playoff_layout) setPlayoffLayout(String(payload.playoff_layout));
    setResults(data.results || null);
    setUpdatedAt(data.updated_at ? new Date(Number(data.updated_at) * 1000).toISOString() : "");
    return savedGroups.length > 0;
  };
  // Prefill the group seeds from what was detected + parsed at import time, so
  // the tab is ready with no manual "Autofill from HLTV event" click. Only used
  // when no stored simulation already populated the slots.
  const loadEventAutofill = async () => {
    try {
      const data = await api.get("/groups/event-autofill", 60000);
      const groups = data?.status === "ok" ? data.groups || [] : [];
      // Nothing stored, or a draw captured before HLTV published it (TBD
      // slots): fetch the live event page instead. This replaces the old
      // manual "Autofill from HLTV" button.
      if (groups.length === 0 || groups.some((grp) => (grp.team_ids || []).some((id) => !(Number(id) > 0)))) {
        await autofillFromHltv();
        return;
      }
      const fmt =
        data.group_format === "de8" ? (groupVariant === "de8_top3" ? "de8_top3" : "de8") : "gsl4";
      const nextSlots = groups.flatMap((grp) =>
        (grp.team_ids || []).map((id) => (id > 0 ? String(id) : "unknown"))
      );
      setGroupFormat(fmt);
      setGroupCount(groups.length);
      setSlots(nextSlots);
      setAutofillMessage(`Prefilled ${groups.length} groups from the imported event.`);
    } catch {
      /* best-effort; button remains available */
    }
  };
  const loadLatestCombos = async () => {
    const data = await api.get("/groups/best-team/latest", 60000);
    if (!data?.exists) {
      setLiveMode(false);
      return;
    }
    setLiveMode(Boolean(data.live));
    setCombosReady(true);
    setCombosUpdatedAt(data.updated_at ? new Date(Number(data.updated_at) * 1000).toISOString() : "");
    if (data.pool_reduced_to && data.pool_reduced_from && data.pool_reduced_to < data.pool_reduced_from) {
      setPoolReduction({ from: data.pool_reduced_from, to: data.pool_reduced_to });
    } else {
      setPoolReduction(null);
    }
  };

  // Nothing renders until the stored simulation (or the event's draw) is in,
  // so the tab never flashes an empty default format first.
  const [ready, setReady] = useState(false);
  useEffect(() => {
    loadEvents();
    loadLatestCombos();
    (async () => {
      try {
        const populated = await loadLatest();
        if (!populated && stageKind === "groups") await loadEventAutofill();
      } finally {
        setReady(true);
      }
    })();
  }, []);
  useEffect(() => {
    loadEventTeams(selectedEventId);
  }, [selectedEventId]);

  const run = async () => {
    if (PUBLIC_BUILD || !allSlotsFilled) return;
    setBusy(true);
    setRunMessage("");
    setRunProgress({ done: 0, total: 0 });
    try {
      const start = await api.post("/groups/start", {
        groups: groupSlots.map((g) => g.map((v) => (v === "unknown" ? "unknown" : Number(v)))),
        group_format: groupFormat,
        combined_playoffs: combinedPlayoffs,
        playoff_stop_teams: groupFormat === "de8_top3" ? 1 : Number(playoffStopTeams) || 1,
      });
      if (start?.detail) {
        setRunMessage(String(start.detail));
        return;
      }
      let done = false;
      while (!done) {
        const status = await api.get(`/groups/job/${start.job_id}`);
        if (status?.detail) {
          setRunMessage(String(status.detail));
          return;
        }
        setRunProgress({ done: Number(status.processed_units || 0), total: Number(status.total_units || 0) });
        if (status.status === "failed") {
          setRunMessage(status.error || "Group simulation failed.");
          return;
        }
        if (status.status === "completed") {
          await loadLatest();
          setCombosReady(false);
          setTopTeams(null);
          setAllTeams(null);
          await loadLatestCombos();
          if (refresh) await refresh(); // pick up any newly created Unknown teams
          done = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
    } catch (e) {
      setRunMessage(e?.message || "Failed to run group simulation.");
    } finally {
      setBusy(false);
    }
  };

  const resetStored = async () => {
    if (PUBLIC_BUILD) return;
    await api.delete("/groups/latest");
    setResults(null);
    setUpdatedAt("");
    setTopTeams(null);
    setAllTeams(null);
    setCombosReady(false);
    setCombosUpdatedAt("");
    setCompletedResult(null);
    setTopMessage("");
    // Start over from HLTV's current draw (live re-sync if it has TBD slots).
    if (stageKind === "groups") await loadEventAutofill();
  };

  const runCombinations = async () => {
    setBusy(true);
    setTopMessage("");
    setComboProgress({ done: 0, total: 0 });
    try {
      if (PUBLIC_BUILD) return;
      const start = await api.post("/groups/best-team/start", {});
      if (start?.detail) {
        setTopMessage(String(start.detail));
        return;
      }
      if (pollingRef.current) return;
      pollingRef.current = true;
      try {
        let done = false;
        while (!done) {
          const status = await api.get(`/groups/best-team/job/${start.job_id}`);
          if (status?.detail) {
            setTopMessage(String(status.detail));
            return;
          }
          setComboProgress({
            done: Number(status.processed_combinations || 0),
            total: Number(status.total_combinations || 0),
          });
          if (status.status === "failed") {
            setTopMessage(status.error || "Combination job failed.");
            return;
          }
          if (status.status === "completed") {
            setCombosReady(true);
            setCombosUpdatedAt(new Date().toISOString());
            done = true;
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
      } finally {
        pollingRef.current = false;
      }
    } catch (e) {
      setTopMessage(e?.message || "Failed to run combinations.");
    } finally {
      setBusy(false);
    }
  };

  const queryCombos = async (nextPage = 0) => {
    if (!combosReady) return;
    const seq = ++querySeqRef.current;
    try {
      const data = await api.post(
        "/groups/best-team/query",
        {
          mode: comboMode,
          search: comboSearch,
          sort: sortKey,
          page: nextPage,
          page_size: COMBO_PAGE_SIZE,
          include_player_ids: Array.from(includeSet),
          exclude_player_ids: Array.from(excludeSet),
        },
        120000
      );
      if (seq !== querySeqRef.current) return;
      setTopTeams(data.top_teams || []);
      setAllTeams(data.page_teams || []);
      setFilteredCount(Number(data.filtered_count || 0));
      setPage(Number(data.page || nextPage || 0));
      setTopMessage(
        data.exact === false
          ? "Ranking not proven exact for this page (time budget or contender limit) — showing the best rosters found, scored exactly."
          : ""
      );
    } catch (e) {
      if (seq !== querySeqRef.current) return;
      setTopMessage(e?.message || "Failed to load combinations.");
    }
  };
  // Only query rosters while the Top 5 tab is actually showing: the live-pool
  // query costs ~0.5 s and 300 KB, which used to run on every Tournament open.
  useEffect(() => {
    if (!combosReady || groupsTab !== "top5" || groupsTopSubtab === "completed") return;
    queryCombos(0);
  }, [combosReady, comboMode, comboSearch, sortKey, groupsTab, groupsTopSubtab, includeSet, excludeSet]);

  const completedMatchCount = groupFormat === "de8" ? 10 : 5;
  const other = (pair, winner) => (pair.length === 2 ? pair.find((t) => String(t) !== String(winner)) : undefined);
  const completedDerived = useMemo(() => {
    const empty = Array(5).fill("");
    return groupSlots.map((group, g) => {
      const picks = completedPicks[g] || empty;
      const [s1, s2, s3, s4] = group;
      const o1 = [s1, s2];
      const o2 = [s3, s4];
      const o1w = picks[0];
      const o2w = picks[1];
      const winnersPair = o1w && o2w ? [o1w, o2w] : [];
      const losersPair = o1w && o2w ? [other(o1, o1w), other(o2, o2w)] : [];
      const wWinner = picks[2];
      const eWinner = picks[3];
      const deciderPair =
        wWinner && eWinner && winnersPair.length ? [other(winnersPair, wWinner), eWinner] : [];
      const pairs = [o1, o2, winnersPair, losersPair, deciderPair];
      const complete = picks.every((p, i) => Boolean(p) && pairs[i].some((t) => String(t) === String(p)));
      return { pairs, picks, complete };
    });
  }, [groupSlots, completedPicks, groupFormat, completedMatchCount]);

  // de8 completed derivation (10 matches): opening 1-4, upper sf 1-2, lower r1 1-2, lower sf 1-2.
  const completedDerived8 = useMemo(() => {
    const empty = Array(10).fill("");
    return groupSlots.map((group, g) => {
      const picks = completedPicks[g] || empty;
      const o1 = [group[0], group[1]];
      const o2 = [group[2], group[3]];
      const o3 = [group[4], group[5]];
      const o4 = [group[6], group[7]];
      const [w1, w2, w3, w4] = [picks[0], picks[1], picks[2], picks[3]];
      const L1 = w1 ? other(o1, w1) : undefined;
      const L2 = w2 ? other(o2, w2) : undefined;
      const L3 = w3 ? other(o3, w3) : undefined;
      const L4 = w4 ? other(o4, w4) : undefined;
      const usf1 = w1 && w2 ? [w1, w2] : []; // upper sf 1
      const usf2 = w3 && w4 ? [w3, w4] : []; // upper sf 2
      const lr1 = w1 && w2 ? [L1, L2] : []; // lower r1 1
      const lr2 = w3 && w4 ? [L3, L4] : []; // lower r1 2
      const uw1 = picks[4]; // upper sf 1 winner
      const uw2 = picks[5]; // upper sf 2 winner
      const UL1 = uw1 && usf1.length ? other(usf1, uw1) : undefined;
      const UL2 = uw2 && usf2.length ? other(usf2, uw2) : undefined;
      const lw1 = picks[6]; // lower r1 1 winner
      const lw2 = picks[7]; // lower r1 2 winner
      const lsf1 = lw1 && UL2 !== undefined ? [lw1, UL2] : []; // crossed: lr1 winner vs other upper loser
      const lsf2 = lw2 && UL1 !== undefined ? [lw2, UL1] : [];
      const pairs = [o1, o2, o3, o4, usf1, usf2, lr1, lr2, lsf1, lsf2];
      const complete = picks.every((p, i) => Boolean(p) && pairs[i].length === 2 && pairs[i].some((t) => String(t) === String(p)));
      return { pairs, picks, complete };
    });
  }, [groupSlots, completedPicks]);

  const activeCompletedDerived = groupFormat === "de8" ? completedDerived8 : completedDerived;
  const allGroupsComplete =
    activeCompletedDerived.length > 0 && activeCompletedDerived.every((g) => g && g.complete);

  const setCompletedPick = (g, matchIdx, value) => {
    setCompletedPicks((prev) => {
      const picks = [...(prev[g] || Array(completedMatchCount).fill(""))];
      picks[matchIdx] = value;
      if (groupFormat === "de8") {
        // opening picks (0-3) reset upper sf, lower r1, lower sf; upper/lower r1 reset lower sf.
        if (matchIdx <= 3) {
          picks[4] = ""; picks[5] = ""; picks[6] = ""; picks[7] = ""; picks[8] = ""; picks[9] = "";
        } else if (matchIdx <= 7) {
          picks[8] = ""; picks[9] = "";
        }
      } else {
        if (matchIdx <= 1) {
          picks[2] = ""; picks[3] = ""; picks[4] = "";
        } else if (matchIdx <= 3) {
          picks[4] = "";
        }
      }
      return { ...prev, [g]: picks };
    });
    setCompletedResult(null);
  };

  // Bracket formats: one pick per match (match key -> winner), downstream
  // picks cleared when an earlier match changes.
  const [bracketPicks, setBracketPicks] = useState({});
  const pickBracketMatch = (key, tid) => {
    setBracketPicks((prev) => {
      const order = (results?.bracket?.matches || []).map((m) => m[0]);
      const at = order.indexOf(key);
      const next = {};
      order.forEach((k, i) => {
        if (i < at && prev[k]) next[k] = prev[k];
      });
      next[key] = tid;
      return next;
    });
    setCompletedResult(null);
  };
  const bracketComplete =
    groupFormat === "bracket" && (results?.bracket?.matches || []).length > 0 && (results?.bracket?.matches || []).every((m) => Boolean(bracketPicks[m[0]]));
  // Swiss: the picks walk the stage; the backend pairs the next round from
  // the sampler's own rule once a round is complete.
  const [swissPicks, setSwissPicks] = useState({});
  const [swissWalk, setSwissWalk] = useState(null);
  useEffect(() => {
    setSwissPicks({});
    setSwissWalk(null);
  }, [results?.swiss?.event_id, results?.swiss?.seed]);
  useEffect(() => {
    if (groupFormat !== "swiss" || !results?.swiss) return undefined;
    let live = true;
    api
      .post("/groups/swiss/pairings", { swiss_picks: swissPicks }, 30000)
      .then((d) => {
        if (live && d && Array.isArray(d.rounds)) setSwissWalk(d);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [groupFormat, results?.swiss, swissPicks]);
  const pickSwissMatch = (key, tid) => {
    const round = Number(String(key).match(/^r(\d+)_/)?.[1] || 0);
    setSwissPicks((prev) => {
      const next = {};
      Object.entries(prev).forEach(([k, v]) => {
        const r = Number(String(k).match(/^r(\d+)_/)?.[1] || 0);
        if (r <= round && k !== key) next[k] = v;
      });
      next[key] = tid;
      return next;
    });
    setCompletedResult(null);
  };
  const swissComplete = groupFormat === "swiss" && Boolean(swissWalk?.complete);
  // Favourites win: every open match goes to the side the run's win matrix
  // favours (the Swiss walk fills round by round on the backend; a bracket
  // resolves in match order here).
  const fillSwissFavourites = async () => {
    try {
      const d = await api.post("/groups/swiss/pairings", { swiss_picks: swissPicks, favourites: true }, 30000);
      if (d?.picks) {
        setSwissPicks(d.picks);
        setSwissWalk(d);
        setCompletedResult(null);
      }
    } catch {
      // the board keeps the picks it has
    }
  };
  const fillSwissActual = async () => {
    try {
      const d = await api.post("/groups/swiss/pairings", { actual: true }, 60000);
      if (d?.picks) {
        setSwissPicks(d.picks);
        setSwissWalk(d);
        setCompletedResult(null);
      }
    } catch {
      // the board keeps the picks it has
    }
  };
  const fillBracketFavourites = () => {
    const br = results?.bracket;
    if (!br) return;
    const order = (results.groups?.[0] || []).map(Number);
    const pos = {};
    order.forEach((tid, i) => {
      pos[tid] = i;
    });
    const wp = br.win_probs || {};
    const seriesOf = {};
    (br.stages || []).forEach((st) => {
      seriesOf[st.key] = st.series;
    });
    const slots = (br.team_slots || []).map(Number);
    const winners = {};
    const losers = {};
    const next = { ...bracketPicks };
    (br.matches || []).forEach(([key, stage, fa, fb]) => {
      const side = (f) => (f[0] === "seed" ? slots[Number(f[1])] : f[0] === "win" ? winners[String(f[1])] : losers[String(f[1])]) || null;
      const a = side(fa);
      const b = side(fb);
      if (!a || !b) return;
      let w = Number(next[key] || 0);
      if (w !== a && w !== b) {
        const matrix = wp[`bo${seriesOf[stage] || 3}`] || wp.bo3 || wp.bo5 || wp.bo1 || null;
        const pA = matrix && pos[a] != null && pos[b] != null ? Number(matrix[pos[a]][pos[b]]) : 0.5;
        w = pA >= 0.5 ? a : b;
        next[key] = w;
      }
      winners[key] = w;
      losers[key] = w === a ? b : a;
    });
    setBracketPicks(next);
    setCompletedResult(null);
  };
  const [completedPage, setCompletedPage] = useState(0);
  const scoreCompleted = async (nextPage = 0) => {
    setCompletedMessage("");
    try {
      const data = await api.post(
        "/groups/best-team/completed-query",
        {
          ...(groupFormat === "bracket"
            ? { bracket_picks: bracketPicks }
            : groupFormat === "swiss"
              ? { swiss_picks: swissPicks }
              : { group_winners: activeCompletedDerived.map((g) => g.picks.map(Number)) }),
          search: comboSearch,
          include_player_ids: Array.from(includeSet),
          exclude_player_ids: Array.from(excludeSet),
          page: Number(nextPage) || 0,
          page_size: COMBO_PAGE_SIZE,
        },
        120000
      );
      if (data?.detail) {
        setCompletedMessage(String(data.detail));
        return;
      }
      setCompletedPage(Number(data?.page || nextPage || 0));
      setCompletedResult(data);
    } catch (e) {
      setCompletedMessage(e?.message || "Failed to score completed groups.");
    }
  };
  // a scored completed stage follows the filters like the ranking modes do
  // (the effect only fires on a filter change, never on the score itself)
  useEffect(() => {
    if (groupsTopSubtab !== "completed" || !completedResult) return;
    scoreCompleted(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [includeSet, excludeSet, comboSearch]);

  const valueData = useMemo(() => buildPlayerValueRowsFromSimulation(results, players), [results, players]);
  // Each player's price (the run's snapshot) and value against the price
  // line, for the team rows' cards.
  const valueByPid = useMemo(() => {
    const m = {};
    (valueData.rows || []).forEach((r) => {
      m[Number(r.player_id)] = r;
    });
    return m;
  }, [valueData]);
  const matchLabels =
    groupFormat === "de8"
      ? [
          "Opening 1",
          "Opening 2",
          "Opening 3",
          "Opening 4",
          "Upper semi 1",
          "Upper semi 2",
          "Lower round 1",
          "Lower round 2",
          "Lower semi 1",
          "Lower semi 2",
        ]
      : ["Opening 1", "Opening 2", "Winners' match", "Elimination", "Decider"];
  const matchPlaceholders =
    groupFormat === "de8"
      ? [
          ["Seed 1", "Seed 2"],
          ["Seed 3", "Seed 4"],
          ["Seed 5", "Seed 6"],
          ["Seed 7", "Seed 8"],
          ["Opening 1 winner", "Opening 2 winner"],
          ["Opening 3 winner", "Opening 4 winner"],
          ["Opening 1 loser", "Opening 2 loser"],
          ["Opening 3 loser", "Opening 4 loser"],
          ["Lower R1 winner", "Upper semi 2 loser"],
          ["Lower R2 winner", "Upper semi 1 loser"],
        ]
      : [
          ["Seed 1", "Seed 2"],
          ["Seed 3", "Seed 4"],
          ["Opening 1 winner", "Opening 2 winner"],
          ["Opening 1 loser", "Opening 2 loser"],
          ["Winners' loser", "Elimination winner"],
        ];
  const metricLabel = (team, mode = comboMode) =>
    mode === "completed"
      ? `Score ${Number(team?.bracket_score ?? team?.total_ev ?? 0).toFixed(2)}`
      : mode === "single_outcome"
      ? `Ceiling ${Number(team?.ceiling_points || 0).toFixed(2)}`
      : mode === "most_outcomes"
      ? `Win ${(Number(team?.outcome_win_probability || 0) * 100).toFixed(1)}%`
      : `EV ${Number(team?.average_ev ?? team?.total_ev ?? 0).toFixed(2)}`;
  const groupTeamInitials = (teamId) => {
    if (teamId === "unknown") return "??";
    const name = teamLookup[Number(teamId)] || "";
    const parts = String(name).trim().split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return `${parts[0][0] || ""}${parts[1][0] || ""}`.toUpperCase();
    return String(name || "?").slice(0, 2).toUpperCase();
  };
  const GroupTeamRow = ({ teamId, placeholder }) => (
    <div className={`playoff-team-row ${teamId ? "" : "muted"}`}>
      <span className={`playoff-team-badge ${teamId ? "" : "empty"}`}>{teamId ? groupTeamInitials(teamId) : "?"}</span>
      {teamId ? <span>{teamName(teamId)}</span> : <span className="playoff-team-tbd">{placeholder || "TBD"}</span>}
    </div>
  );
  const GroupMatchCard = ({ title, rows, className = "" }) => (
    <div className={`playoff-match-card ${className}`}>
      <div className="playoff-match-head">
        <strong>{title}</strong>
        <span>BO3</span>
      </div>
      <div className="playoff-match-teams">{rows}</div>
    </div>
  );
  const GroupPickRow = ({ teamId, selected, onSelect, placeholder }) => (
    <button
      type="button"
      className={`playoff-team-row completed-pick ${teamId ? "" : "muted"} ${selected ? "active" : ""}`}
      onClick={() => teamId && onSelect(String(teamId))}
      disabled={!teamId}
    >
      <span className={`playoff-team-badge ${teamId ? "" : "empty"}`}>{teamId ? groupTeamInitials(teamId) : "?"}</span>
      <span>{teamId ? teamName(teamId) : placeholder || "TBD"}</span>
    </button>
  );
  const qualificationOdds = useMemo(() => {
    const rates = results?.playoff?.advance_rate;
    if (!rates) return [];
    return Object.entries(rates)
      .map(([tid, rate]) => ({ teamId: Number(tid), rate: Number(rate) }))
      .sort((a, b) => b.rate - a.rate);
  }, [results?.playoff?.advance_rate]);

  // Per-team probability of qualifying out of its group, from the exact
  // enumerated outcomes (probabilities sum to 1 per group). Available for both
  // GSL and 8-team formats regardless of the combined-playoff toggle.
  const groupQualifyOdds = useMemo(() => {
    const byTeam = {};
    const ensure = (tid, g) => {
      const key = Number(tid);
      if (!byTeam[key]) byTeam[key] = { teamId: key, rate: 0, group: Number(g) || 0 };
      return byTeam[key];
    };
    // Slim stored results carry the odds precomputed (the 80 MB outcomes list
    // stays on the backend); every team in the draw gets a row, even at 0%.
    const precomputed = results?.qualify_odds;
    if (precomputed && typeof precomputed === "object") {
      (results?.groups || []).forEach((ids, g) => (ids || []).forEach((tid) => ensure(tid, g)));
      Object.entries(precomputed).forEach(([g, odds]) => {
        Object.entries(odds || {}).forEach(([tid, p]) => (ensure(tid, g).rate += Number(p) || 0));
      });
      return Object.values(byTeam).sort((a, b) => a.group - b.group || b.rate - a.rate);
    }
    const outcomes = results?.outcomes;
    if (!Array.isArray(outcomes) || outcomes.length === 0) return [];
    outcomes.forEach((o) => {
      const p = Number(o.probability) || 0;
      const g = Number(o.group) || 0;
      (o.qualified || []).forEach((tid) => (ensure(tid, g).rate += p));
      (o.eliminated || []).forEach((tid) => ensure(tid, g));
    });
    return Object.values(byTeam).sort((a, b) => a.group - b.group || b.rate - a.rate);
  }, [results]);

  // Per-player expected component split (rating/win/role/booster), keyed by
  // player id, so a clicked name can show where its points come from.
  const playerEvByPid = useMemo(() => {
    const map = {};
    Object.values(results?.teams || {}).forEach((t) => {
      Object.entries(t?.players || {}).forEach(([pid, comps]) => {
        map[Number(pid)] = comps || {};
      });
    });
    return map;
  }, [results]);

  const [breakdownPlayer, setBreakdownPlayer] = useState(null);
  const [bakedInfo, setBakedInfo] = useState(null);
  const [bracketDraw, setBracketDraw] = useState(null); // the stored draw (provisional or baked payload)
  // Collapsible group / playoff cards, remembered per browser so a long
  // page can be skipped past on every visit. { "group-0": true, playoff: true }
  const collapseStorageKey = "groups.collapsedCards";
  const [collapsedCards, setCollapsedCards] = useState(() => {
    try {
      const raw = window.localStorage.getItem(collapseStorageKey);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  });
  const toggleCard = (key) => {
    setCollapsedCards((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try {
        window.localStorage.setItem(collapseStorageKey, JSON.stringify(next));
      } catch {
        /* storage may be unavailable; the toggle still works for this session */
      }
      return next;
    });
  };
  const playerNameById = useMemo(() => {
    const m = {};
    (players || []).forEach((p) => {
      m[Number(p.player_id)] = p.name;
    });
    return m;
  }, [players]);
  // Same Include/Exclude block as the Playoff tab's Top 5: exclude whole
  // teams, search with Include/Exclude suggestions, applied filters as chips.
  const groupsTeamsForFilters = useMemo(() => {
    const raw = results?.teams;
    const ids = (Array.isArray(raw) ? raw.map((t) => t?.team_id) : Object.keys(raw || {}))
      .map(Number)
      .filter((id) => Number.isFinite(id) && id > 0);
    const byId = new Map((teams || []).map((t) => [Number(t.team_id), t]));
    return Array.from(new Set(ids))
      .map((id) => byId.get(id))
      .filter(Boolean)
      .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
  }, [results, teams]);
  // A team's players for the filters: the roster the stored valuation was
  // run with (the event's own lineup, stand-ins included), else the teams
  // table's HLTV lineup.
  const teamPlayerIds = (teamId) => {
    const fromRun = Object.keys(results?.teams?.[String(teamId)]?.players || {})
      .map((pid) => Number(pid))
      .filter((pid) => Number.isFinite(pid) && pid > 0);
    if (fromRun.length > 0) return fromRun;
    const team = (teams || []).find((t) => Number(t.team_id) === Number(teamId));
    if (!team) return [];
    return [team.player1_id, team.player2_id, team.player3_id, team.player4_id, team.player5_id]
      .map((pid) => Number(pid))
      .filter((pid) => Number.isFinite(pid) && pid > 0);
  };
  const comboSearchPidByName = useMemo(() => {
    const m = {};
    groupsTeamsForFilters.forEach((t) => {
      teamPlayerIds(t.team_id).forEach((pid) => {
        const nm = playerNameById[pid];
        if (nm) m[String(nm)] = pid;
      });
    });
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupsTeamsForFilters, playerNameById, teams, results]);
  const comboSearchNames = useMemo(
    () => Object.keys(comboSearchPidByName).sort((a, b) => a.localeCompare(b)),
    [comboSearchPidByName]
  );
  const addPlayerFilterByName = (name, kind) => {
    const pid = comboSearchPidByName[name];
    if (!pid) return;
    const already = kind === "include" ? includeSet.has(pid) : excludeSet.has(pid);
    if (!already) toggleFilter(pid, kind);
  };
  const toggleTeamExclude = (teamId) => {
    const pids = teamPlayerIds(teamId);
    const allOut = pids.length > 0 && pids.every((pid) => excludeSet.has(pid));
    setExcludeSet((prev) => {
      const next = new Set(prev);
      pids.forEach((pid) => (allOut ? next.delete(pid) : next.add(pid)));
      return next;
    });
    if (!allOut) {
      setIncludeSet((prev) => {
        const next = new Set(prev);
        pids.forEach((pid) => next.delete(pid));
        return next;
      });
    }
  };
  // Strip order: title odds first (when there is a playoff), then the best
  // group-place odds, so the favourites read top-down like the Playoff tab.
  const valuationTeamOrder = useMemo(() => {
    if (!results?.teams) return [];
    if (PATH_FORMATS.includes(results.group_format)) {
      const q = results.paths_summary?.advance || {};
      return Object.keys(results.teams).sort((x, y) => Number(q[y] || 0) - Number(q[x] || 0));
    }
    const score = (tid) => {
      const title = Number(results.playoff?.advance_rate?.[String(tid)] || 0);
      const place = results.place_odds?.[String(tid)] || {};
      const first = Number(place["1"] || 0);
      const anyq = Object.values(place).reduce((acc, v) => acc + Number(v || 0), 0);
      return title * 1000 + first * 10 + anyq;
    };
    return Object.keys(results.teams).sort((x, y) => score(y) - score(x));
  }, [results]);
  // Roster mode: the stored valuation only carries the best role's trigger
  // rates, so the roster's assigned role gets its own from the player row.
  const withRosterRole = (ctx, pid) => {
    const prow = (players || []).find((x) => Number(x.player_id) === Number(pid));
    let rolesObj = {};
    try {
      rolesObj = prow?.roles_json ? JSON.parse(prow.roles_json) : {};
    } catch {
      rolesObj = {};
    }
    let rr = rolesObj[String(ctx.role_id)] || {};
    // the Leader role's percentages are the team's result odds in this event
    if (Number(ctx.role_id) === 3 && results?.leader_odds) {
      const tid = Object.keys(results.teams || {}).find((t) => (results.teams[t]?.players || {})[String(pid)] != null);
      const lead = tid ? results.leader_odds[String(tid)] : null;
      if (lead) rr = { major: Number(lead.major || 0), minor: Number(lead.minor || 0) };
    }
    const major = Number(rr.major || 0);
    const minor = Number(rr.minor || 0);
    return { ...ctx, major, minor, per_match: 5 * major + 2 * minor - 2 * (1 - major - minor) };
  };
  // The average view: the stored run's stage stats, re-scored client-side
  // under the roster's plan when opened from a roster.
  const openGroupPlayerAverage = (p, rosterCtx = null) => {
    const comps = playerEvByPid[Number(p.player_id)] || {};
    const stageStats = results?.stage_stats || null;
    const stageRow = stageStats?.players?.[String(p.player_id)] || null;
    const teamRow = stageStats?.teams?.[String(p.team_id)] || null;
    setBreakdownPlayer({
      player_id: p.player_id,
      name: p.name,
      team_id: p.team_id,
      role_id: comps.role_id ?? p.role_id ?? null,
      rating: Number(comps.rating_points_total || 0),
      win: Number(comps.win_points_total || 0),
      role: Number(comps.role_points_total || 0),
      booster: Number(comps.booster_points_total || 0),
      total: Number(comps.total_points || 0),
      stage_list: stageStats?.stages || [],
      stage_ev: stageRow?.stages || {},
      team_rounds: teamRow?.rounds || {},
      team_ranks: Object.fromEntries(
        Object.entries(stageStats?.teams || {}).map(([tid, t]) => [tid, t?.rank ?? null])
      ),
      padding_prob: Number(teamRow?.padding_prob || 0),
      role_info: stageRow?.role || null,
      padding: stageRow?.padding || null,
      penalty: Number(stageRow?.penalty || 0),
      playoff_padding: stageRow?.playoff_padding || null,
      playoff_pad_prob: Number(teamRow?.playoff_padding_prob || 0),
      playoff_penalty: Number(stageRow?.playoff_penalty || 0),
      roster: rosterCtx ? withRosterRole(rosterCtx, p.player_id) : null,
    });
  };
  // From a roster card the breakdown follows the ranking mode: Most Likely
  // Winner shows the player over the outcomes this roster wins, Best Single
  // Outcome in the roster's best outcome — scored server-side under the
  // roster's plan (its role, its booster per match), so the parts add up to
  // the card's figure. Average, or a failure, falls back to the average view.
  const openGroupPlayer = (p, rosterCtx = null, team = null) => {
    const conditional = (path, extra, note) => {
      api
        .post(
          path,
          {
            player_id: Number(p.player_id),
            role_id: rosterCtx?.role_id ?? null,
            booster_assignments: team?.booster_assignments || [],
            ...extra,
          },
          120000
        )
        .then((data) => {
          if (!data || data.detail) {
            openGroupPlayerAverage(p, rosterCtx);
            return;
          }
          const c = data.components || {};
          setBreakdownPlayer({
            player_id: p.player_id,
            name: p.name,
            team_id: p.team_id,
            role_id: data.role_info?.role_id ?? rosterCtx?.role_id ?? p.role_id ?? null,
            rating: Number(c.rating || 0),
            win: Number(c.win || 0),
            role: Number(c.role || 0),
            booster: Number(c.booster || 0),
            total: Number(c.total || 0),
            stage_list: data.stage_list || [],
            stage_ev: data.stage_ev || {},
            team_rounds: data.team_rounds || {},
            team_ranks: data.team_ranks || {},
            padding_prob: Number(data.padding_prob || 0),
            role_info: data.role_info || null,
            padding: data.padding || null,
            penalty: Number(data.penalty || 0),
            playoff_padding: data.playoff_padding || null,
            playoff_pad_prob: Number(data.playoff_pad_prob || 0),
            playoff_penalty: Number(data.playoff_penalty || 0),
            hero_note: note(data),
            runs: data.runs || null,
            runs_rest: data.runs_rest || null,
            roster: null,
          });
        })
        .catch(() => openGroupPlayerAverage(p, rosterCtx));
    };
    if (team && comboMode === "most_outcomes" && Array.isArray(team.winning_outcome_indexes) && team.winning_outcome_indexes.length > 0) {
      conditional("/groups/best-team/winning-player-detail", { outcome_indexes: team.winning_outcome_indexes }, (d) => {
        const pct = Number(team.outcome_win_probability ?? d.mass ?? 0) * 100;
        return `in the ${Number(d.n_outcomes || 0).toLocaleString()} outcomes this roster wins · ${pct.toFixed(1)}%`;
      });
      return;
    }
    if (team && comboMode === "single_outcome" && team.ceiling_outcome_index != null) {
      conditional("/groups/best-team/outcome-player-detail", { outcome_index: Number(team.ceiling_outcome_index) }, (d) => {
        const pct = Number(d.probability || 0) * 100;
        const shown = pct >= 1 ? pct.toFixed(1) : pct >= 0.01 ? pct.toFixed(2) : pct > 0 ? pct.toPrecision(2) : "0";
        return `this roster's best outcome · ${shown}% chance`;
      });
      return;
    }
    openGroupPlayerAverage(p, rosterCtx);
  };
  const renderPlayerLinks = (list) =>
    (list || []).map((p, i) => (
      <span key={`${p.player_id}-${i}`}>
        {i > 0 ? ", " : ""}
        <button type="button" className="inline-link-btn" onClick={() => openGroupPlayer(p)}>
          {p.name}
        </button>{" "}
        ({teamLookup[p.team_id] || p.team_id})
      </span>
    ));
  // Completed Groups: components come from the exact-outcome player_values row,
  // so open with those concrete values and an outcome-specific note.
  const teamOfPid = useMemo(() => {
    const m = {};
    Object.entries(results?.teams || {}).forEach(([tid, td]) => {
      Object.keys(td?.players || {}).forEach((pid) => {
        m[Number(pid)] = Number(tid);
      });
    });
    return m;
  }, [results]);
  const completedEvByPid = useMemo(() => {
    const map = {};
    (completedResult?.player_values || []).forEach((row) => {
      map[Number(row.player_id)] = row;
    });
    return map;
  }, [completedResult]);
  const openCompletedPlayer = (p) => {
    const row = completedEvByPid[Number(p.player_id)] || {};
    setBreakdownPlayer({
      player_id: p.player_id,
      name: p.name,
      team_id: p.team_id,
      rating: Number(row.rating || 0),
      win: Number(row.win || 0),
      role: Number(row.role || 0),
      booster: Number(row.booster || 0),
      total: Number(row.points || 0),
      note: "Points for this exact set of group results.",
    });
  };

  // The roster strips every Top 5 list draws (rank, metric, cost, one card
  // per player with the points split): the ranking modes and the completed
  // results share it, so they cannot drift.
  const renderRosterRows = (list, mode, offset, onCard) => (
        <div className="event-team-rows">
          {(list || []).map((team, idx) => {
            const boostByPid = {};
            (team.booster_assignments || []).forEach((a) => {
              const pid = Number(a.player_id);
              boostByPid[pid] = (boostByPid[pid] || 0) + Number(a.expected_points || 0);
            });
            return (
              <div key={idx} className="stack combo-team">
                <div className="event-team-row">
                  <div className="event-team-head">
                    <div className="combo-rank">#{idx + 1 + offset}</div>
                    <div className="event-team-headtext">
                      <span className="event-team-name">{metricLabel(team, mode)}</span>
                      <span className="combo-cost">Cost ${Number(team.cost || 0).toLocaleString()}</span>
                    </div>
                  </div>
                  <div className="event-team-players">
                    {(team.players || []).map((p) => {
                      const roleNum = Number(p.role_name);
                      const roleId =
                        p.role_name != null && p.role_name !== "" && Number.isFinite(roleNum)
                          ? roleNum
                          : roleIdFromName(p.role_name);
                      const assigned = boostByPid[Number(p.player_id)];
                      const boost = assigned != null ? assigned : Number(p.booster_ev || 0);
                      // the kind of points this ranking mode shows; a serialised roster
                      // already folds the assigned booster into its average
                      const pts = rosterPoints(p, mode);
                      const score =
                        Number(pts.total || 0) +
                        (mode === "average" && assigned != null && !(Number(p.booster_ev) > 0) ? assigned : 0);
                      const rosterCtx =
                        (team.booster_assignments || []).length > 0
                          ? {
                              role_id: roleId,
                              role_ev: Number(p.role_ev || 0),
                              booster_ev: boost,
                              boosters: (team.booster_assignments || []).filter(
                                (a) => Number(a.player_id) === Number(p.player_id)
                              ),
                            }
                          : null;
                      return (
                        <div
                          className="event-player-card clickable"
                          key={p.player_id}
                          onClick={() => onCard(p, rosterCtx, team)}
                        >
                          <PlayerPhoto playerId={Number(p.player_id)} name={p.name} size={52} />
                          <div className="event-player-name">{p.name}</div>
                          <div className="event-player-team">{teamLookup[p.team_id] || p.team_id}</div>
                          <div className="event-player-role">
                            {roleId != null && <RoleBadge roleId={roleId} size={16} />}
                            <span>{roleLabel(p.role_name)}</span>
                          </div>
                          <div className="event-player-rating">{score.toFixed(2)}</div>
                          <div className="event-player-stats ev-mini">
                            {POINTS_PARTS.map(([label, part]) => [
                              label,
                              part === "booster" && mode === "average" ? boost : pts[part],
                            ]).map(([label, value]) => (
                              <div className="event-player-stat" key={label}>
                                <div className="event-player-mini">{Number(value || 0).toFixed(1)}</div>
                                <div className="event-player-stat-label">{label}</div>
                              </div>
                            ))}
                          </div>
                          <div className="event-player-price combo-price">
                            ${Math.round(Number(p.price || 0) / 1000)}k
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
  );

  // The Top 5 filters (teams, included / excluded players, search): the
  // ranking modes and the completed sub-tab show the same card.
  const renderTop5Filters = () => (
    <div className="card sub">
      <h3>Top Teams - {(groupsTopSubtabs.find((t) => t.key === groupsTopSubtab) || {}).label || "Best Average Value"}</h3>
      <div className="top5-filters">
        <div className="field">
          <span>Teams</span>
          <div className="filter-team-grid">
            {groupsTeamsForFilters.length === 0 && <span className="muted">No teams stored yet.</span>}
            {groupsTeamsForFilters.map((t) => {
              const tid = Number(t.team_id);
              const pids = teamPlayerIds(tid);
              const isExcluded = pids.length > 0 && pids.every((pid) => excludeSet.has(pid));
              return (
                <div key={`filter-team-${tid}`} className={`filter-team-card${isExcluded ? " exc" : ""}`}>
                  <TeamLogo hltvTeamId={t.hltv_team_id} name={t.name} size={26} />
                  <span className="filter-team-name">{t.name}</span>
                  <div className="filter-team-toggles">
                    <button className={`filter-toggle out${isExcluded ? " on" : ""}`} onClick={() => toggleTeamExclude(tid)}>
                      {isExcluded ? "Excluded" : "Exclude"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
    {(includeSet.size > 0 || excludeSet.size > 0) && (
      <div className="grid two">
        {includeSet.size > 0 && (
          <div className="filter-group in">
            <div className="filter-group-title">Included Players</div>
            <div className="chips">
              {Array.from(includeSet).map((pid) => (
                <button key={`inc-${pid}`} className="filter-chip in" title="Remove filter" onClick={() => toggleFilter(pid, "include")}>
                  <PlayerPhoto playerId={pid} name={playerNameById[pid]} size={20} />
                  {playerNameById[pid] || `Player ${pid}`} <span className="x">&times;</span>
                </button>
              ))}
            </div>
          </div>
        )}
        {excludeSet.size > 0 && (
          <div className="filter-group out">
            <div className="filter-group-title">Excluded Players</div>
            <div className="chips">
              {Array.from(excludeSet).map((pid) => (
                <button key={`exc-${pid}`} className="filter-chip out" title="Remove filter" onClick={() => toggleFilter(pid, "exclude")}>
                  <PlayerPhoto playerId={pid} name={playerNameById[pid]} size={20} />
                  {playerNameById[pid] || `Player ${pid}`} <span className="x">&times;</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    )}
        <div className="top5-controls-row">
          <div className="top5-search">
            <SuggestInput
              label="Search / Filter Players"
              value={comboSearch}
              onChange={setComboSearch}
              placeholder="Player name"
              suggestions={comboSearchNames}
              actions={[
                { key: "in", label: "Include", onPick: (name) => addPlayerFilterByName(name, "include") },
                { key: "out", label: "Exclude", onPick: (name) => addPlayerFilterByName(name, "exclude") },
              ]}
            />
          </div>
        </div>
      </div>
    </div>
  );

  const renderCompletedResults = () => (
    <>
      {completedMessage && <p className="muted">{completedMessage}</p>}
      {completedResult && (
        <>
          <div className="card sub">
            <h3>Best Team For These Results</h3>
            <p className="muted">
              {groupFormat === "swiss"
                ? `Path probability 1 in ${Number(completedResult.outcome_probability) > 0 ? Math.round(1 / Number(completedResult.outcome_probability)).toLocaleString() : "∞"}`
                : `Outcome probability ${(Number(completedResult.outcome_probability || 0) * 100).toFixed(3)}% of ${Number(completedResult.outcomes_count || 0).toLocaleString()} stored outcomes.`}
            </p>
            <ComboPager page={completedPage} pageSize={COMBO_PAGE_SIZE} total={completedResult.filtered_count} onPage={scoreCompleted} />
            {renderRosterRows(completedResult.page_teams || completedResult.top_teams || [], "completed", completedPage * COMBO_PAGE_SIZE, (p) => openCompletedPlayer(p))}
          </div>
          <PlayerScoresTable rows={completedResult.player_values || []} teamOfPid={teamOfPid} teamLookup={teamLookup} onOpen={openCompletedPlayer} />
        </>
      )}
    </>
  );
  return (
    <Section title={stageKind === "swiss" ? "Swiss Stage" : stageKind === "bracket" ? "Playoff Bracket" : "Double-Elimination Groups (BO3)"}>
      <div className="stack">
        {!ready && <p className="muted">Loading group stage...</p>}
        {ready && (
        <>
        {/* Same order as the Playoff tab: stage, player value, top 5 (with the
            completed-groups picker as a Top 5 sub-tab). */}
        <div className="tab-bar small">
          <button className={groupsTab === "stage" ? "tab active" : "tab"} onClick={() => setGroupsTab("stage")}>
            {stageKind === "swiss" ? "Swiss Stage" : stageKind === "bracket" ? "Bracket" : "Group Stage"}
          </button>
          <button className={groupsTab === "value" ? "tab active" : "tab"} onClick={() => setGroupsTab("value")}>
            Player Value
          </button>
          <button className={groupsTab === "top5" ? "tab active" : "tab"} onClick={() => setGroupsTab("top5")}>
            Top 5 Teams
          </button>
        </div>

        {groupsTab === "stage" && groupFormat === "swiss" && (
          <SwissStageView
            results={results}
            teams={teams}
            teamLookup={teamLookup}
            excludeSet={excludeSet}
            teamPlayerIds={teamPlayerIds}
            onToggleTeam={toggleTeamExclude}
          />
        )}
        {groupsTab === "stage" && groupFormat === "bracket" && (
          <BracketStageView results={results} teams={teams} teamLookup={teamLookup} draw={bracketDraw} />
        )}
        {groupsTab === "stage" && (
          <>
            {/* Display-only, like the Playoff tab: the draw comes from the HLTV
                event page, loaded on open and re-synced automatically while any
                slot is still undecided. */}
            {autofillMessage && <p className="muted">{autofillMessage}</p>}
            {!PATH_FORMATS.includes(groupFormat) && groupSlots.length > 0 && (() => {
              // every group in one card, each bracket under its own heading
              const isCollapsed = Boolean(collapsedCards.groups);
              const summary = groupSlots.map((group, g) => `Group ${g + 1}: ${group.map((v) => (v ? teamName(v) : "TBD")).join(" · ")}`).join("   ");
              return (
                <div className="card sub group-card">
                  <button type="button" className="group-card-head collapsible" onClick={() => toggleCard("groups")}>
                    <h3>{groupSlots.length > 1 ? "Groups" : "Group"}</h3>
                    {isCollapsed && <span className="group-card-summary">{summary}</span>}
                    <span className="group-card-chevron" aria-hidden="true">
                      {isCollapsed ? "▸" : "▾"}
                    </span>
                  </button>
                  {!isCollapsed &&
                    groupSlots.map((group, g) => (
                      <div className="group-block" key={`group-${g}`}>
                        {groupSlots.length > 1 && <div className="group-block-title">Group {g + 1}</div>}
                        <GroupBracketVisual
                          format={groupFormat}
                          slotIds={group}
                          nameOf={(v) => (v ? teamName(v) : "")}
                          hltvIdByTeamId={hltvIdByTeamId}
                          combined={combinedPlayoffs}
                          bo={groupBo}
                        />
                      </div>
                    ))}
                </div>
              );
            })()}
            {combinedPlayoffs && combinedAllowed && (
              <div className="card sub group-card">
                <button type="button" className="group-card-head collapsible" onClick={() => toggleCard("playoff")}>
                  <h3>Playoff bracket</h3>
                  {collapsedCards.playoff && (
                    <span className="group-card-summary">
                      {groupFormat === "de8_top3"
                        ? "6 teams · group winners bye to the semi-finals"
                        : `${qualsPerGroup * groupCount} teams · single elimination`}
                    </span>
                  )}
                  <span className="group-card-chevron" aria-hidden="true">
                    {collapsedCards.playoff ? "▸" : "▾"}
                  </span>
                </button>
                {!collapsedCards.playoff && groupFormat === "gsl4" && groupCount === 2 && !PUBLIC_BUILD && (
                  <div className="playoff-layout-row">
                    <select value={playoffLayout} disabled={layoutBusy} onChange={(e) => changePlayoffLayout(e.target.value)}>
                      <option value="cross">Semis: 1st v 2nd of the other group</option>
                      <option value="same_group">Semis: 1st v 2nd of the same group</option>
                    </select>
                  </div>
                )}
                {!collapsedCards.playoff && (
                  <PlayoffSkeleton
                    format={groupFormat}
                    groupCount={groupCount}
                    qualsPerGroup={qualsPerGroup}
                    stopTeams={groupFormat === "de8_top3" ? 1 : Number(playoffStopTeams) || 1}
                    bo={playoffBo}
                    thirdPlace={playoffThirdPlace}
                    layout={playoffLayout}
                  />
                )}
              </div>
            )}
            {combinedDetected && !combinedSupported && (
              <p className="warning-text" style={{ fontSize: 12, margin: 0 }}>
                HLTV runs this as ONE fantasy game for the groups and the playoffs, but the simulator has no verified
                playoff seeding for {combinedShape || "this shape"}. It has been flagged for development; valuations
                here cover the group stage only.
              </p>
            )}
            {!results && (
              <p className="muted" style={{ fontSize: 12 }}>
                No valuations stored yet — they are baked when the event is imported, once HLTV has published the draw
                (the nightly run retries until then).
              </p>
            )}
            {results && (
              <div className="event-team-rows">
                {valuationTeamOrder.map((tid) => {
                  const data = results.teams[tid] || {};
                  const info = (teams || []).find((t) => Number(t.team_id) === Number(tid));
                  const groupIdx = (results.groups || []).findIndex((ids) => (ids || []).some((x) => Number(x) === Number(tid)));
                  const place = results.place_odds?.[String(tid)] || {};
                  const reach = results.playoff?.round_reach?.[String(tid)] || [];
                  const labels = results.playoff?.round_labels || [];
                  const title = Number(results.playoff?.advance_rate?.[String(tid)] || 0);
                  // The odds that matter for a team: making the playoffs (any
                  // qualifying place), winning the group (the bye to the semis in
                  // the top-3 format), reaching the final, and the title.
                  const qualify = Object.values(place).reduce((acc, v) => acc + Number(v || 0), 0);
                  const first = Number(place["1"] || 0);
                  // The last bracket round is the final; a third-place decider is listed after it.
                  const lastRound = Math.max(1, Number(results.playoff?.rounds || labels.length)) - 1;
                  const finalReach = reach.length > 0 ? Number(reach[lastRound] || 0) : null;
                  const earlyStop = Number(results.playoff?.stop_teams || 1) > 1;
                  const advance = Number(results.paths_summary?.advance?.[String(tid)] || 0);
                  const finalKey = results.bracket?.kind === "double" ? "grand_final" : "final";
                  const odds =
                    results.group_format === "swiss"
                      ? [
                          ["Qualify", advance, true],
                          ["3-0", Number(results.swiss?.records?.[String(tid)]?.["3-0"] || 0), false],
                        ]
                      : results.group_format === "bracket"
                        ? [
                            [results.bracket?.kind === "double" ? "Grand final" : "Final", Number(results.paths_summary?.stage_reach?.[String(tid)]?.[finalKey] || 0), false],
                            ["Title", advance, true],
                          ]
                        : [[results.playoff ? "Playoffs" : "Qualify", qualify, false], ["Win group", first, false]];
                  if (results.playoff) {
                    if (finalReach != null && !earlyStop) odds.push([labels[lastRound] || "Final", finalReach, false]);
                    odds.push([earlyStop ? "Qualify" : "Title", title, true]);
                  }
                  const rows = Object.entries(data.players || {}).sort(
                    (x, y) => Number(y[1].total_points ?? 0) - Number(x[1].total_points ?? 0)
                  );
                  const teamPids = teamPlayerIds(tid);
                  const teamExcluded = teamPids.length > 0 && teamPids.every((p) => excludeSet.has(p));
                  return (
                    <div className={`event-team-row${teamExcluded ? " exc" : ""}`} key={tid}>
                      <div className="event-team-head">
                        <TeamLogo hltvTeamId={info?.hltv_team_id} name={teamName(tid)} size={42} />
                        <div className="event-team-headtext">
                          <span className="event-team-name">{teamName(tid)}</span>
                          {PATH_FORMATS.includes(results.group_format) ? (
                            <span className="event-team-rank">
                              Seed {results.swiss?.seeds?.[String(tid)] ?? (results.groups?.[0] || []).map(Number).indexOf(Number(tid)) + 1}
                            </span>
                          ) : (
                            groupIdx >= 0 && <span className="event-team-rank">Group {groupIdx + 1}</span>
                          )}
                          <div className="event-team-odds">
                            {odds.map(([label, p, accent]) => (
                              <div className={`event-team-odd${accent ? " accent" : ""}`} key={label}>
                                <span className="event-team-odd-label">{label}</span>
                                <span className="event-team-odd-value">{Math.round(p * 100)}%</span>
                              </div>
                            ))}
                          </div>
                          <div className="filter-team-toggles">
                            <button className={`filter-toggle out${teamExcluded ? " on" : ""}`} onClick={() => toggleTeamExclude(tid)}>
                              {teamExcluded ? "Excluded" : "Exclude"}
                            </button>
                          </div>
                        </div>
                      </div>
                      <div className="event-team-players">
                        {rows.map(([pid, comps]) => {
                          const total = Number(comps.total_points ?? 0);
                          const name = playerNameById[Number(pid)] || String(pid);
                          const included = includeSet.has(Number(pid));
                          const excluded = excludeSet.has(Number(pid));
                          return (
                            <div
                              className={`event-player-card clickable${excluded ? " exc" : included ? " inc" : ""}`}
                              key={pid}
                              onClick={() => openGroupPlayer({ player_id: Number(pid), name, team_id: Number(tid) })}
                            >
                              <PlayerPhoto playerId={Number(pid)} name={name} size={52} />
                              <div className="event-player-name">{name}</div>
                              {comps.role_id != null && (
                                <div className="event-player-role">
                                  <RoleBadge roleId={comps.role_id} size={16} />
                                  <span>{ROLE_NAMES[comps.role_id] || `Role ${comps.role_id}`}</span>
                                </div>
                              )}
                              <div className="event-player-rating">{total.toFixed(2)}</div>
                              <div className="event-player-stats ev-mini">
                                {[
                                  ["Rating", comps.rating_points_total],
                                  ["Win", comps.win_points_total],
                                  ["Role", comps.role_points_total],
                                  ["Boost", comps.booster_points_total],
                                ].map(([label, value]) => (
                                  <div className="event-player-stat" key={label}>
                                    <div className="event-player-mini">{Number(value || 0).toFixed(1)}</div>
                                    <div className="event-player-stat-label">{label}</div>
                                  </div>
                                ))}
                              </div>
                              {valueByPid[Number(pid)] && (
                                <div className="event-player-price combo-price">
                                  ${Math.round(Number(valueByPid[Number(pid)].price || 0) / 1000)}k
                                </div>
                              )}
                              {valueByPid[Number(pid)] && (
                                <div
                                  className={`event-player-value${
                                    Number(valueByPid[Number(pid)].distance) < -0.005
                                      ? " neg"
                                      : Number(valueByPid[Number(pid)].distance) > 0.005
                                        ? " pos"
                                        : ""
                                  }`}
                                >
                                  {Number(valueByPid[Number(pid)].distance) >= 0 ? "+" : ""}
                                  {Number(valueByPid[Number(pid)].distance || 0).toFixed(1)}
                                </div>
                              )}
                              <div className="event-player-filters">
                                <button
                                  className={`filter-toggle in${included ? " on" : ""}`}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    toggleFilter(Number(pid), "include");
                                  }}
                                >
                                  {included ? "Included" : "Include"}
                                </button>
                                <button
                                  className={`filter-toggle out${excluded ? " on" : ""}`}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    toggleFilter(Number(pid), "exclude");
                                  }}
                                >
                                  {excluded ? "Excluded" : "Exclude"}
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}

        {groupsTab === "top5" && (
          <>
            {!results && (
              <div className="card sub">
                <p className="muted">{PUBLIC_BUILD ? NOT_PUBLISHED : "Run the group stage first."}</p>
              </div>
            )}
            {results && (
              <>
                <div className="tab-bar small">
                  {groupsTopSubtabs.map((sub) => (
                    <button
                      key={sub.key}
                      className={groupsTopSubtab === sub.key ? "tab active" : "tab"}
                      onClick={() => setGroupsTopMode(sub.key)}
                    >
                      {sub.label}
                    </button>
                  ))}
                </div>
                {groupsTopSubtab !== "completed" && (
                <>
                {renderTop5Filters()}
                {(allTeams || topTeams) && (allTeams || topTeams).length > 0 && (
                  <div className="card sub">
                    <h3>Top Teams</h3>
                    <ComboPager page={page} pageSize={COMBO_PAGE_SIZE} total={filteredCount} onPage={queryCombos} />
                    {/* Same roster strips as the Playoff tab's Top Teams: rank,
                        metric, cost, then one card per player with the EV split.
                        The roster's booster assignment (when the query returns
                        one) is folded into each player's Boost. */}
                    {renderRosterRows(allTeams && allTeams.length > 0 ? allTeams : topTeams, comboMode, page * COMBO_PAGE_SIZE, openGroupPlayer)}
                  </div>
                )}
                {topMessage && <p className="muted">{topMessage}</p>}
                </>
                )}
              </>
            )}
          </>
        )}

        {groupsTab === "top5" && results && groupsTopSubtab === "completed" && groupFormat === "de8_top3" && (
          <p className="muted">
            Completed-bracket scoring for the top-3 double-elim variant isn't supported yet — use the Group
            Stage simulation.
          </p>
        )}
        {groupsTab === "top5" && results && groupsTopSubtab === "completed" && groupFormat === "swiss" && (
          <>
            <div className="card sub">
              <h3>Swiss Results</h3>
              <SwissBoard walk={swissWalk} teams={teams} teamLookup={teamLookup} picks={swissPicks} onPick={pickSwissMatch} />
            </div>
            {renderTop5Filters()}
            <div className="actions">
              <button className="secondary" onClick={fillSwissActual} disabled={!swissWalk}>
                Actual results
              </button>
              <button className="secondary" onClick={fillSwissFavourites} disabled={!swissWalk}>
                Favourites win
              </button>
              <button className="primary" onClick={() => scoreCompleted(0)} disabled={!swissComplete || !combosReady}>
                Score Completed Swiss
              </button>
            </div>
            {renderCompletedResults()}
          </>
        )}
        {groupsTab === "top5" && results && groupsTopSubtab === "completed" && groupFormat === "bracket" && (
          <>
            <div className="card sub">
              <h3>Bracket Results</h3>
              <BracketPicker bracket={results.bracket} teamLookup={teamLookup} picks={bracketPicks} onPick={pickBracketMatch} />
            </div>
            {renderTop5Filters()}
            <div className="actions">
              <button className="secondary" onClick={fillBracketFavourites}>
                Favourites win
              </button>
              <button className="primary" onClick={() => scoreCompleted(0)} disabled={!bracketComplete || !combosReady}>
                Score Completed Bracket
              </button>
            </div>
            {renderCompletedResults()}
          </>
        )}
        {groupsTab === "top5" && results && groupsTopSubtab === "completed" && groupFormat !== "de8_top3" && !PATH_FORMATS.includes(groupFormat) && (
          <>
            {results &&
              activeCompletedDerived.map((group, g) => {
                const pickRow = (matchIdx) => {
                  const placeholders = matchPlaceholders[matchIdx] || ["", ""];
                  const ready = group.pairs[matchIdx].length === 2 && group.pairs[matchIdx].every(Boolean);
                  return (
                    <div className="gsl-match" key={`c-${g}-${matchIdx}`}>
                      <span className="gsl-label">{matchLabels[matchIdx]}</span>
                      <span className="gsl-pick">
                        {[0, 1].map((rowIdx) => {
                          const tid = ready ? group.pairs[matchIdx][rowIdx] : "";
                          const selected = ready && String(group.picks[matchIdx]) === String(tid);
                          return (
                            <button
                              key={rowIdx}
                              disabled={!tid}
                              className={selected ? "active" : ""}
                              onClick={() => tid && setCompletedPick(g, matchIdx, String(tid))}
                            >
                              {tid ? teamName(tid) : placeholders[rowIdx]}
                            </button>
                          );
                        })}
                      </span>
                    </div>
                  );
                };
                return (
                  <div className="card sub" key={`completed-${g}`}>
                    <h3>Group {g + 1} Results</h3>
                    <p className="muted">Click each match winner; later matchups fill in from earlier picks.</p>
                    <div className="gsl-flow">
                      {matchLabels.map((_, matchIdx) => pickRow(matchIdx))}
                    </div>
                  </div>
                );
              })}
            {results && renderTop5Filters()}
            {results && (
              <div className="actions">
                <button className="primary" onClick={() => scoreCompleted(0)} disabled={!allGroupsComplete || !combosReady}>
                  Score Completed Groups
                </button>
                {combosReady && !allGroupsComplete && <span className="muted">Pick every match winner first.</span>}
              </div>
            )}
            {renderCompletedResults()}
          </>
        )}

        {groupsTab === "value" && results && (
          <PriceVsPointsPanel
            title="Player Price vs Points (Groups)"
            rows={valueData.rows}
            slope={valueData.slope}
            intercept={valueData.intercept}
          />
        )}
        {groupsTab === "value" && !results && (
          <div className="card sub">
            <p className="muted">{PUBLIC_BUILD ? NOT_PUBLISHED : "Run the group stage first."}</p>
          </div>
        )}
        </>
        )}
      </div>
      <GroupPlayerBreakdownModal
        player={breakdownPlayer}
        teamLookup={teamLookup}
        teams={teams}
        onClose={() => setBreakdownPlayer(null)}
      />
    </Section>
  );
}

function BountyTab(props) {
  const [bountyTab, setBountyTab] = useState("playoffs");
  return (
    <div className="stack">
      <div className="tab-bar small">
        <button className={bountyTab === "playoffs" ? "tab active" : "tab"} onClick={() => setBountyTab("playoffs")}>
          Playoffs
        </button>
        <button className="tab" disabled title="Online stage (32 teams) coming later">
          Online Stage
        </button>
      </div>
      {bountyTab === "playoffs" && <PlayoffTab {...props} variant="bounty" />}
    </div>
  );
}

function TournamentTab({ teams, teamLookup, players, sortTeams, applyFilters, onOpenPlayer, refresh, notify, eventId = null, onSelectEvent = null }) {
  // One adaptive page: the active fantasy event's kind (per-stage, e.g. a
  // "Cologne Groups" event shows only the group stage) picks which tournament
  // UI renders. Auto-detected on the backend; a manual override is stored on
  // the event for the rare ambiguous case (and to reach Bounty mode).
  const [kindInfo, setKindInfo] = useState(null);
  const [message, setMessage] = useState("");
  // Imported events + the server's current one, for the public event picker.
  const [eventList, setEventList] = useState([]);
  const [serverActive, setServerActive] = useState(null);

  const loadKind = async () => {
    setMessage("");
    try {
      const evs = await api.get("/events/");
      setEventList(evs?.events || []);
      setServerActive(evs?.active_event_id ?? null);
      const active = eventId ?? evs?.active_event_id ?? null;
      if (active == null) {
        setKindInfo(null);
        setMessage("No active fantasy event. Set one active in the Events tab.");
        return;
      }
      const info = await api.get(`/events/${active}/kind`);
      if (info?.detail) {
        setKindInfo(null);
        setMessage(`Format detection failed for event ${active}: ${info.detail}`);
        return;
      }
      if (!info?.kind || info.kind === "unknown") {
        setKindInfo(null);
        setMessage(`Event ${active} has no detectable tournament format (no structured data on its event page).`);
        return;
      }
      setKindInfo(info);
    } catch (e) {
      setMessage(e?.message || "Could not detect the event's tournament kind.");
    }
  };

  useEffect(() => {
    loadKind();
  }, []);

  const kind = kindInfo?.kind || null;
  const sharedProps = { teams, teamLookup, players, sortTeams, applyFilters, onOpenPlayer };
  return (
    <div className="stack">
      {message && <p className="muted">{message}</p>}
      {!kind && !message && <p className="muted">Loading tournament...</p>}
      {kind && !["swiss", "groups", "playoff", "bounty", "double_elim"].includes(kind) && (
        <p className="muted">
          Detected format: {kindInfo?.label || kind} — no simulator supports this format yet.
        </p>
      )}
      {kind === "swiss" && (
        <GroupsTab
          teams={teams}
          teamLookup={teamLookup}
          players={players}
          refresh={refresh}
          stageKind="swiss"
          groupBo={Number(kindInfo?.swiss_bo_rules?.default || 3)}
        />
      )}
      {kind === "groups" && (
        <GroupsTab
          teams={teams}
          teamLookup={teamLookup}
          players={players}
          refresh={refresh}
          groupVariant={kindInfo?.group_variant}
          combinedDetected={typeof kindInfo?.combined_playoffs === "boolean" ? kindInfo.combined_playoffs : null}
          combinedSupported={Boolean(kindInfo?.combined_supported)}
          combinedShape={kindInfo?.combined_shape || ""}
          playoffDetected={{ size: Number(kindInfo?.playoff_size || 0), byes: Number(kindInfo?.playoff_byes || 0) }}
          groupBo={Number(kindInfo?.group_bo_rules?.default || 3)}
          playoffBo={kindInfo?.playoff_bo_rules || null}
          playoffThirdPlace={Boolean(kindInfo?.playoff_third_place)}
          playoffLayoutDetected={kindInfo?.playoff_layout || "cross"}
        />
      )}
      {(kind === "playoff" || kind === "double_elim") && (
        <GroupsTab
          teams={teams}
          teamLookup={teamLookup}
          players={players}
          refresh={refresh}
          stageKind="bracket"
          playoffBo={kindInfo?.playoff_bo_rules || null}
        />
      )}
      {kind === "bounty" && <BountyTab {...sharedProps} />}
    </div>
  );
}

export default function App() {
  const [active, setActive] = useState("view");
  const [openPlayerId, setOpenPlayerId] = useState(null);
  const [players, setPlayers] = useState([]);
  const [teams, setTeams] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const server = useServerStatus();
  const auth = useAuth(server.status === "ok", Boolean(server.config?.auth?.required));
  // Public build: which event the user is looking at (null = the server's).
  const [viewEventId, setViewEventIdState] = useState(VIEW_EVENT_ID);
  const selectViewEvent = (id) => {
    rememberViewEvent(id);
    setViewEventIdState(id || null);
  };
  // When the server comes back after an outage the tabs' first loads have
  // already failed; fetch again as soon as the gate lifts.
  const serverWasOk = useRef(server.status === "ok");
  useEffect(() => {
    if (server.status === "ok" && !serverWasOk.current && (!PUBLIC_BUILD_STATIC || auth.status === "signed_in")) load();
    serverWasOk.current = server.status === "ok";
  }, [server.status]);
  // Public build: the data endpoints need a session, so load after sign-in.
  const authWasIn = useRef(false);
  useEffect(() => {
    if (!PUBLIC_BUILD_STATIC) return;
    if (auth.status === "signed_in" && !authWasIn.current) load();
    authWasIn.current = auth.status === "signed_in";
  }, [auth.status]);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [p, t] = await Promise.all([api.get("/players/"), api.get("/teams/")]);
      setPlayers(p);
      setTeams(t);
    } catch (e) {
      setError("Backend unavailable. Make sure FastAPI is running.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!PUBLIC_BUILD_STATIC) load();
  }, []);

  const teamLookup = useMemo(() => {
    const map = {};
    teams.forEach((t) => (map[t.team_id] = t.name));
    return map;
  }, [teams]);

  const playerLookup = useMemo(() => {
    const map = {};
    players.forEach((p) => (map[p.player_id] = p.name));
    return map;
  }, [players]);

  // Toast popups removed by request — components still call notify(), it just
  // no longer surfaces anything.
  const notify = () => {};

  const mapStatsModalRefreshRef = useRef(null);
  const mapStatsJob = useMapStatsJob({ refresh: load, notify, modalRefreshRef: mapStatsModalRefreshRef });

  const handleOpenPlayerFromAnywhere = (playerId) => {
    const pid = Number(playerId);
    if (!Number.isFinite(pid) || pid <= 0) return;
    setActive("view");
    setOpenPlayerId(pid);
  };

  const [openTeamId, setOpenTeamId] = useState(null);
  const handleOpenTeamFromAnywhere = (teamId) => {
    const tid = Number(teamId);
    if (!Number.isFinite(tid) || tid <= 0) return;
    setActive("view");
    setOpenTeamId(tid);
  };

  const sortTeams = (list, key) => {
    const arr = [...(list || [])];
    switch (key) {
      case "ev_asc":
        arr.sort((a, b) => a.total_ev - b.total_ev);
        break;
      case "cost_asc":
        arr.sort((a, b) => a.cost - b.cost);
        break;
      case "cost_desc":
        arr.sort((a, b) => b.cost - a.cost);
        break;
      case "cpp_desc":
        arr.sort((a, b) => b.total_ev / (b.cost || 1) - a.total_ev / (a.cost || 1));
        break;
      case "cpp_asc":
        arr.sort((a, b) => a.total_ev / (a.cost || 1) - b.total_ev / (b.cost || 1));
        break;
      case "ev_desc":
      default:
        arr.sort((a, b) => b.total_ev - a.total_ev);
    }
    return arr;
  };

  const applyFilters = (teams, includeSet, excludeSet) => {
    if (!teams) return [];
    return teams.filter((team) => {
      const ids = team.players.map((p) => p.player_id);
      if (includeSet && includeSet.size > 0) {
        for (const pid of includeSet) {
          if (!ids.includes(pid)) return false;
        }
      }
      if (excludeSet && excludeSet.size > 0) {
        for (const pid of excludeSet) {
          if (ids.includes(pid)) return false;
        }
      }
      return true;
    });
  };

  const contentMap = {
    view: (
      <DatabaseTab
        players={players}
        teams={teams}
        loading={loading}
        error={error}
        refresh={load}
        notify={notify}
        openPlayerId={openPlayerId}
        onOpenPlayerHandled={() => setOpenPlayerId(null)}
        openTeamId={openTeamId}
        onOpenTeamHandled={() => setOpenTeamId(null)}
        mapStats={mapStatsJob}
        mapStatsModalRefreshRef={mapStatsModalRefreshRef}
      />
    ),
    events: (
      <EventsTab
        refreshData={load}
        notify={notify}
        players={players}
        teams={teams}
        onOpenPlayer={handleOpenPlayerFromAnywhere}
        onOpenTeam={handleOpenTeamFromAnywhere}
        viewEventId={viewEventId}
        onViewEvent={selectViewEvent}
      />
    ),
    live: <LiveTab onOpenSettings={() => setActive("settings")} />,
    season: <SeasonTab onOpenSettings={() => setActive("settings")} />,
    settings: <SettingsTab />,
    tournament: (
      <TournamentTab
        key={`tournament-${viewEventId || "current"}`}
        teams={teams}
        teamLookup={teamLookup}
        players={players}
        sortTeams={sortTeams}
        applyFilters={applyFilters}
        onOpenPlayer={handleOpenPlayerFromAnywhere}
        refresh={load}
        notify={notify}
        eventId={viewEventId}
        onSelectEvent={selectViewEvent}
      />
    ),
    scheduling: <SchedulingTab notify={notify} players={players} refresh={load} />,
    users: <UsersTab notify={notify} me={auth.user} />,
  };

  return (
    <>
      {/* Drag region standing in for the hidden native title bar (see main.js). */}
      <div className="titlebar">
        <span className="titlebar-brand">
          CS Fantasy <em>Toolkit</em>
        </span>
        {PUBLIC_BUILD_STATIC && <span className="titlebar-version">v{APP_VERSION}</span>}
        <UpdateBanner />
        {PUBLIC_BUILD_STATIC && auth.status === "signed_in" && auth.user && (
          <span className="titlebar-user">
            {auth.user.picture && <img src={auth.user.picture} alt="" referrerPolicy="no-referrer" />}
            <span className="titlebar-user-name">{auth.user.name || auth.user.email}</span>
            {auth.user.is_admin && <span className="titlebar-admin">Admin</span>}
            <button type="button" className="titlebar-signout" onClick={auth.signOut}>
              Sign out
            </button>
          </span>
        )}
      </div>
      {PUBLIC_BUILD_STATIC && <UpdateModal />}
      {PUBLIC_BUILD_STATIC && server.status !== "ok" ? (
        <ServerGate {...server} />
      ) : PUBLIC_BUILD_STATIC && auth.status !== "signed_in" ? (
        <SignInGate auth={auth} configured={server.config?.auth ? Boolean(server.config.auth.configured) : null} />
      ) : (
        <div className="layout">
          {PUBLIC_BUILD_STATIC && server.config?.message && <div className="server-notice">{server.config.message}</div>}
          <nav className="tab-bar">
            {visibleTabs().map((t) => (
              <TabButton key={t.key} active={t.key === active} onClick={() => setActive(t.key)}>
                {t.label}
              </TabButton>
            ))}
          </nav>

          <main className="content">
            <TabErrorBoundary key={active} name={active}>
              {contentMap[active]}
            </TabErrorBoundary>
          </main>
        </div>
      )}
    </>
  );
}

// Named export so the modal can be rendered on its own (preview / visual checks).
export { GroupPlayerBreakdownModal };
