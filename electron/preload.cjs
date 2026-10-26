// CommonJS on purpose: the renderer is sandboxed (Electron default), and a
// sandboxed preload cannot use ESM imports - with the old .js/ESM file the
// bridge silently failed to load and the renderer fell back to fetching
// 127.0.0.1:8000 directly. package.json is "type": "module", so .cjs.
const { contextBridge, ipcRenderer } = require("electron");

// The main process resolved which backend to talk to: the local one it found
// or spawned (operator checkout) or the hosted one from the website's api.json
// (public build); ask it once, sync, so the base URL is baked in before the
// renderer's first request.
const API_BASE = ipcRenderer.sendSync("api-base") || "http://127.0.0.1:8000";
const APP_INFO = ipcRenderer.sendSync("app-info") || { publicBuild: false, version: "dev", siteUrl: "", packaged: false };
// Session token for the hosted backend (public build): loaded once, sent as
// a bearer header on every request, replaced/cleared by the sign-in flow.
let AUTH_TOKEN = APP_INFO.publicBuild ? ipcRenderer.sendSync("auth-token-get") || null : null;

const parseJsonSafe = async (res) => {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    // An HTML page here is Cloudflare's (rate limited, access denied, origin
    // down), never the backend's: report the condition, not the markup.
    const throttled = /rate limit|error code: ?1015|too many requests/i.test(text);
    return {
      detail: throttled
        ? "The server is busy. Please try again in a moment."
        : `The server returned an unexpected response (HTTP ${res.status}).`,
      _html: true,
    };
  }
};

// Throttled responses (the backend's 429, Cloudflare's rate-limit page) are
// retried a couple of times with a pause instead of failing the whole page.
const RETRY_DELAYS_MS = [2000, 4000];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const requestJson = async (path, init = {}, timeoutMs = 60000, attempt = 0) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  const headers = { ...(init.headers || {}) };
  // The public app is treated as public by the backend even on the operator's
  // own machine (backend/services/public_access.py).
  if (APP_INFO.publicBuild) headers["X-Public-Client"] = "1";
  if (AUTH_TOKEN) headers.Authorization = `Bearer ${AUTH_TOKEN}`;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, headers, signal: controller.signal });
  } catch (e) {
    if (e?.name === "AbortError") {
      throw new Error(
        APP_INFO.publicBuild
          ? "The server did not respond in time. Please try again in a moment."
          : "Request timed out - the backend may still be working. Wait a moment and retry."
      );
    }
    if (APP_INFO.publicBuild) {
      throw new Error("Could not reach the server. Check your connection and try again.");
    }
    throw e;
  } finally {
    clearTimeout(timeout);
  }
  const data = await parseJsonSafe(res);
  if (!res.ok) {
    const throttled = res.status === 429 || res.status === 503 || (data?._html && /busy/.test(String(data?.detail || "")));
    if (throttled && attempt < RETRY_DELAYS_MS.length) {
      await sleep(RETRY_DELAYS_MS[attempt]);
      return requestJson(path, init, timeoutMs, attempt + 1);
    }
    const detail = data?.detail || `HTTP ${res.status}`;
    throw new Error(String(detail));
  }
  return data;
};

contextBridge.exposeInMainWorld("api", {
  baseUrl: API_BASE,
  appInfo: APP_INFO,
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
  // Server-sent events: onEvent({event, data}) for every message until the
  // returned function is called; onClose() once if the connection drops.
  subscribe: (path, onEvent, onClose) => {
    const controller = new AbortController();
    (async () => {
      try {
        const headers = { Accept: "text/event-stream" };
        if (APP_INFO.publicBuild) headers["X-Public-Client"] = "1";
        if (AUTH_TOKEN) headers.Authorization = `Bearer ${AUTH_TOKEN}`;
        const res = await fetch(`${API_BASE}${path}`, { headers, signal: controller.signal });
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
  openExternal: (url) => ipcRenderer.invoke("open-external", url),
  onUpdateStatus: (callback) => {
    ipcRenderer.on("update-status", (_event, status) => callback(status));
  },
  installUpdate: () => ipcRenderer.invoke("install-update"),
  // Renderer-side errors go to the app log (updater.log in the data folder).
  reportError: (info) => ipcRenderer.send("renderer-error", info),
  log: (info) => ipcRenderer.send("renderer-log", info),
  auth: {
    get: () => AUTH_TOKEN,
    set: (token) => {
      AUTH_TOKEN = String(token || "") || null;
      return ipcRenderer.invoke("auth-token-set", AUTH_TOKEN);
    },
    clear: (reason) => {
      AUTH_TOKEN = null;
      return ipcRenderer.invoke("auth-token-clear", reason || "");
    },
  },
  checkForUpdates: () => ipcRenderer.invoke("check-updates"),
  // My Team: sign in to HLTV in a window and get the session cookies back.
  connectHltv: () => ipcRenderer.invoke("hltv-connect"),
  forgetHltv: () => ipcRenderer.invoke("hltv-forget"),
});
