/* PS VIBE Mini App — API helper module (no build step, plain ES module via window).
 *
 * Auth flows:
 *   [Telegram]  initData  →  POST /api/telegram/validate  →  JWT (memory only)  →  Bearer calls.
 *   [Standalone] username+password → POST /auth/login → access+refresh (localStorage, see below)
 *                401 → POST /auth/refresh (once) → retry; on failure → clear + login screen.
 *
 * Exposes: window.PSV = { api, login, logout, authenticate, getStaff, ... , printVoucher }
 */
(function () {
  "use strict";

  // ── Bundled (self-contained) build support (2026-09-20, owner order: Android app only) ──
  // When the UI ships INSIDE the APK the WebView origin is https://localhost, so a relative
  // path like "/api/..." would hit the Capacitor fallback server (index.html), not the API.
  // In that case prefix every request with the public origin. On the served web app
  // (ps-vibe.com/webapp) API_ORIGIN is "" so nothing changes there.
  var API_ORIGIN = "";
  try {
    if (location.hostname === "localhost" || location.protocol === "capacitor:") {
      API_ORIGIN = "https://ps-vibe.com";
    }
  } catch (_e) {}
  function _u(p) { return /^https?:/i.test(String(p)) ? p : (API_ORIGIN + p); }

  // ── Web-build self-heal (2026-09-20) ────────────────────────────────────────
  // The Android WebView can keep serving a cached app.js after a server deploy, so a
  // fix looks like it "didn't work" on the phone. On boot, compare the build marker in
  // the freshly fetched index.html with the marker this bundle was built with; when the
  // server is newer, hard-reload with a cache-busting query. Bump BOTH markers together
  // with the ?v= query on every deploy.
  var PSV_BUILD = "w33depositdrafts";
  // Real app build number — shown in the More menu footer (was hardcoded "build 34" before,
  // which misled diagnosis). Bump together with android versionCode.
  var APP_BUILD = "51";
  function psvBuildSelfHeal() {
    try {
      fetch(_u("/webapp/?_b=" + Date.now()), { cache: "no-store" })
        .then(function (r) { return r.text(); })
        .then(function (t) {
          var m = t.match(/psv-build"\s+content="([^"]+)"/);
          if (!m || !m[1] || m[1] === PSV_BUILD) return;
          var u = new URL(location.href);
          if (u.searchParams.get("_v") === m[1]) return;   // already tried this build
          u.searchParams.set("_v", m[1]);
          location.replace(u.toString());
        })
        .catch(function () {});
    } catch (_e) {}
  }
  try { psvBuildSelfHeal(); } catch (_e) {}

  // ── Telegram WebApp handle ──────────────────────────────
  const tg = window.Telegram && window.Telegram.WebApp;

  let jwt = null;        // effective access token
  let refreshTok = null; // standalone staff-login refresh token
  let isWebLogin = false; // true when authenticated via /auth/login (NOT Telegram)
  let staff = { name: "", id: null, role: "" };

  // ── Standalone (non-Telegram) token store ───────────────
  // TRADEOFF: The Android/WebView shell has NO Telegram context, so there is no
  // initData to exchange. To keep staff signed in between app launches we persist
  // the access+refresh pair in localStorage. This is a deliberate, scoped exception:
  // the Telegram path stays memory-only (XSS-safer) and never touches these keys.
  const AUTH_KEY = "psvibe.staff.auth";
  // ── Native-shell JWT mirror (FCM integration) ───────────
  // The ALREADY-DISTRIBUTED Android shell (psv-native-bridge.js) reads the
  // bearer token for its device-registration call from localStorage
  // 'psv_jwt' (fallback 'psv_token'). We mirror the plain access token there
  // so push registration works WITHOUT an APK rebuild. 'psvibe.staff.auth'
  // remains the single source of truth; these keys are a convenience mirror
  // and are cleared on logout / forced-logout / refresh-failure.
  const SHELL_JWT_KEY = "psv_jwt";
  const SHELL_JWT_FALLBACK = "psv_token";
  function _syncShellJwt() {
    try {
      if (isWebLogin && jwt) { localStorage.setItem(SHELL_JWT_KEY, jwt); localStorage.setItem(SHELL_JWT_FALLBACK, jwt); }
      else { localStorage.removeItem(SHELL_JWT_KEY); localStorage.removeItem(SHELL_JWT_FALLBACK); }
    } catch (_e) {}
  }
  function _persistAuth() {
    if (!isWebLogin) return;
    try { localStorage.setItem(AUTH_KEY, JSON.stringify({ access: jwt, refresh: refreshTok, staff: staff })); } catch (_e) {}
    _syncShellJwt();
  }
  function _clearStored() {
    try { localStorage.removeItem(AUTH_KEY); } catch (_e) {}
    try { localStorage.removeItem(SHELL_JWT_KEY); localStorage.removeItem(SHELL_JWT_FALLBACK); } catch (_e) {}
  }
  function _loadStored() {
    try {
      const raw = localStorage.getItem(AUTH_KEY);
      if (!raw) return false;
      const o = JSON.parse(raw);
      if (o && o.access) { jwt = o.access; refreshTok = o.refresh || null; staff = o.staff || staff; isWebLogin = true; _syncShellJwt(); return true; }
    } catch (_e) {}
    return false;
  }
  function _forceLogout() {
    jwt = null; refreshTok = null; isWebLogin = false; staff = { name: "", id: null, role: "" };
    _clearStored();
    try { window.dispatchEvent(new Event("psv-auth-expired")); } catch (_e) {}
  }

  // ── Rate-limit / Cloudflare-challenge detection (WAVE 4) ─
  // The owner's phone once hit a Cloudflare "You are being rate limited /
  // banned temporarily" HTML page; the old code surfaced that raw HTML as the
  // error message. We now: detect challenges, NEVER render raw HTML, and
  // auto-retry GETs with exponential backoff (2s → 4s → 8s, max 3 retries).
  const RATE_RE = /(rate limit|too many requests|attention required|you are being rate limited|banned temporarily|cloudflare|__cf_chl|cf-error-details)/i;
  const RATE_MSG = "Server is busy — retrying…";
  const BACKOFF_MS = [2000, 4000, 8000];

  function _looksHtml(s) {
    return typeof s === "string" && /<\s*(!doctype|html|head|body|title|div|script|p|span)\b/i.test(s.slice(0, 400));
  }
  function _cleanText(s, status) {
    if (typeof s !== "string" || !s) return "HTTP " + status;
    if (_looksHtml(s)) return "Server error (HTTP " + status + ") — please try again";
    return s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 300) || ("HTTP " + status);
  }
  function _isChallenge(status, ctype, body) {
    if (status === 429) return true;
    const isHtml = ctype.indexOf("html") >= 0;
    if (typeof body === "string" && body && RATE_RE.test(body)) return true;
    if ((status === 403 || status === 503) && (isHtml || !ctype)) return true;
    return false;
  }
  function _rateError() {
    const e = new Error(RATE_MSG);
    e.code = "RATE_LIMITED"; e.rateLimited = true; e.retryable = true;
    return e;
  }
  function _emit(name, detail) {
    try { window.dispatchEvent(new CustomEvent(name, { detail: detail || {} })); } catch (_e) {}
  }
  let _rlActive = false;
  function _rlStart(info) { _rlActive = true; _emit("psv:rate-limit", info || {}); }
  function _rlClear() { if (_rlActive) { _rlActive = false; _emit("psv:rate-limit-clear", {}); } }

  // ── Request de-bursting: TTL cache + in-flight dedupe (WAVE 4) ──
  // Tabs/screens used to refetch instantly (and the "Open" chip fired 4
  // parallel status calls). Identical concurrent GETs now share ONE promise;
  // results are cached for CACHE_TTL so quick tab switches cost zero requests.
  const CACHE_TTL = 15000;
  const CACHE_STORE = "psv_cache_v1";
  const _cache = Object.create(null);      // key → { ts, data }
  const _inflight = Object.create(null);   // key → Promise
  let _persist = Object.create(null);      // survives app restarts (instant first paint)
  try { _persist = JSON.parse(localStorage.getItem(CACHE_STORE) || "{}") || Object.create(null); } catch (_e) { _persist = Object.create(null); }

  function _cacheKey(path, options) {
    const m = String((options && options.method) || "GET").toUpperCase();
    return m + " " + path;
  }
  function _persistSave() {
    try {
      const keys = Object.keys(_persist);
      if (keys.length > 80) {   // keep the store tiny
        keys.sort(function (a, b) { return ((_persist[a] || {}).ts || 0) - ((_persist[b] || {}).ts || 0); });
        for (let i = 0; i < keys.length - 80; i++) delete _persist[keys[i]];
      }
      localStorage.setItem(CACHE_STORE, JSON.stringify(_persist));
    } catch (_e) { /* quota / private mode — memory cache still works */ }
  }
  function invalidate(prefix) {
    const p = prefix || "";
    for (const k in _cache) { if (!p || k.indexOf(p) >= 0) delete _cache[k]; }
    for (const k in _persist) { if (!p || k.indexOf(p) >= 0) delete _persist[k]; }
    if (!p) { try { localStorage.removeItem(CACHE_STORE); } catch (_e) {} }
    else { _persistSave(); }
  }
  /**
   * Cached GET with stale-while-revalidate (owner 2026-09-20 16:06: "loading အရမ်းကြာနေတယ်").
   *  - fresh hit  → resolved from memory (zero requests)
   *  - stale hit  → resolved IMMEDIATELY with the last known data, refreshed in the background
   *                 (a "psv:data" event fires when the fresh payload lands)
   *  - no hit     → normal request
   * Persisted entries survive an app restart, so the first screen paint is instant.
   * Non-GET calls are passed straight through to api().
   */
  function apiCached(path, options, ttlMs) {
    const opts = options || {};
    const m = String(opts.method || "GET").toUpperCase();
    if (m !== "GET" && m !== "HEAD") return api(path, options);
    const key = _cacheKey(path, opts);
    const ttl = ttlMs == null ? CACHE_TTL : ttlMs;
    const hit = _cache[key] || _persist[key] || null;

    const fetchFresh = function () {
      if (_inflight[key]) return _inflight[key];
      const pr = api(path, options).then(function (data) {
        _cache[key] = { ts: Date.now(), data: data };
        _persist[key] = _cache[key];
        _persistSave();
        delete _inflight[key];
        try { _emit("psv:data", { path: path, key: key }); } catch (_e) {}
        return data;
      }, function (err) { delete _inflight[key]; throw err; });
      _inflight[key] = pr;
      return pr;
    };

    if (hit && (Date.now() - hit.ts) < ttl) return Promise.resolve(hit.data);   // fresh
    if (hit) { fetchFresh().catch(function () {}); return Promise.resolve(hit.data); } // stale → instant
    return fetchFresh();
  }
  /** Multipart upload (profile picture etc.) — same auth + origin handling as api(). */
  async function upload(path, formData) {
    const headers = {};
    if (jwt) headers["Authorization"] = "Bearer " + jwt;
    let resp;
    try { resp = await fetch(_u(path), { method: "POST", headers: headers, body: formData }); }
    catch (e) { throw new Error("Network error — check your internet connection"); }
    let body = null;
    try { body = await resp.json(); } catch (_e) { body = null; }
    if (!resp.ok) {
      const m = (body && (body.detail || body.error)) || ("Upload failed (" + resp.status + ")");
      throw new Error(m);
    }
    return body;
  }

  // ── Avatar (owner 2026-09-20 17:43: the uploaded picture never appeared) ──────────────
  // Server photo_url when it exists, otherwise a copy kept ON THIS PHONE so a failed upload
  // still shows a picture immediately.
  function _avatarKey() {
    try { const st = staff || {}; return "psv_avatar_" + (st.username || st.id || "me"); } catch (_e) { return "psv_avatar_me"; }
  }
  function avatarSrc() {
    try {
      const st = staff || {};
      const su = st.photo_url || "";
      let url = su ? (/^(https?:|data:)/i.test(su) ? su : (API_ORIGIN + su)) : (localStorage.getItem(_avatarKey()) || "");
      if (!url) return "";
      if (url.slice(0, 5) === "data:") return url;
      // Owner 2026-09-20 17:54: the upload worked but the phone kept showing the OLD picture —
      // the file name never changes, so bust the WebView image cache with a version query.
      let v = "";
      try { v = localStorage.getItem(_avatarKey() + "_v") || ""; } catch (_e) {}
      return url + (url.indexOf("?") >= 0 ? "&" : "?") + "v=" + (v || "1");
    } catch (_e) { return ""; }
  }
  function setLocalAvatar(dataUrl) {
    try { localStorage.setItem(_avatarKey(), dataUrl || ""); } catch (_e) {}
    bumpAvatar();
  }
  function bumpAvatar() {
    try { localStorage.setItem(_avatarKey() + "_v", String(Date.now())); } catch (_e) {}
  }

  /** CORS-simple POST (no Authorization header, text/plain body) — used by the profile calls
   *  because the phone rejected the preflighted /auth/* POSTs with a network error. */
  async function apiPlain(path, obj) {
    let resp;
    try {
      resp = await fetch(_u(path), { method: "POST", headers: { "Content-Type": "text/plain;charset=UTF-8" }, body: JSON.stringify(obj || {}) });
    } catch (e) { throw new Error("Network error — " + path); }
    let body = null;
    try { body = await resp.json(); } catch (_e) { body = null; }
    if (!resp.ok) throw new Error((body && (body.detail || body.error)) || ("HTTP " + resp.status));
    return body;
  }
  /** CORS-simple multipart upload (no Authorization header). */
  async function uploadPlain(path, formData) {
    let resp;
    try { resp = await fetch(_u(path), { method: "POST", body: formData }); }
    catch (e) { throw new Error("Network error — " + path); }
    let body = null;
    try { body = await resp.json(); } catch (_e) { body = null; }
    if (!resp.ok) throw new Error((body && (body.detail || body.error)) || ("HTTP " + resp.status));
    return body;
  }
  function apiToken() { return jwt || ""; }

  /**
   * Direct POST with NO 401-recovery / retry wrapper, so the screen sees the TRUE outcome.
   * (The profile calls kept showing "Network error" because api() answers a 401 by refreshing
   *  through /auth/*, and that refresh failed on the device — hiding the real status.)
   */
  async function apiDirect(path, obj) {
    const headers = { "Content-Type": "application/json" };
    if (jwt) headers["Authorization"] = "Bearer " + jwt;
    let resp;
    try { resp = await fetch(_u(path), { method: "POST", headers: headers, body: JSON.stringify(obj || {}) }); }
    catch (e) { throw new Error("Network error — " + path); }
    let body = null;
    try { body = await resp.json(); } catch (_e) { body = null; }
    if (!resp.ok) {
      const detail = (body && (body.detail || body.error)) || "";
      const msg = (resp.status === 401)
        ? "Session expired (401) — log out and sign in again"
        : (detail || ("HTTP " + resp.status));
      const err = new Error(msg);
      err.status = resp.status;
      throw err;
    }
    return body;
  }
  /** Same as apiDirect but for multipart uploads. */
  async function uploadDirect(path, formData) {
    const headers = {};
    if (jwt) headers["Authorization"] = "Bearer " + jwt;
    let resp;
    try { resp = await fetch(_u(path), { method: "POST", headers: headers, body: formData }); }
    catch (e) { throw new Error("Network error — " + path); }
    let body = null;
    try { body = await resp.json(); } catch (_e) { body = null; }
    if (!resp.ok) {
      const detail = (body && (body.detail || body.error)) || "";
      const msg = (resp.status === 401) ? "Session expired (401) — log out and sign in again" : (detail || ("HTTP " + resp.status));
      const err = new Error(msg); err.status = resp.status; throw err;
    }
    return body;
  }

  function retryNow() { invalidate(); _emit("psv:retry", {}); }

  // ── Low-level API call ──────────────────────────────────
  async function _raw(path, options) {
    const headers = Object.assign({}, options.headers || {});
    if (jwt) headers["Authorization"] = "Bearer " + jwt;

    let resp;
    try {
      resp = await fetch(_u(path), Object.assign({}, options, { headers }));
    } catch (e) {
      throw new Error("Network error — check your internet connection");
    }

    // Auto-recover once on 401 (refresh for web login, re-validate for Telegram)
    const _noRecover = /^\/auth\//.test(path) || path === "/api/telegram/validate";
    if (resp.status === 401 && !_noRecover) {
      const wasWeb = isWebLogin;
      const ok = await _recoverAuth();
      if (ok) {
        headers["Authorization"] = "Bearer " + jwt;
        resp = await fetch(_u(path), Object.assign({}, options, { headers }));
      } else {
        throw new Error(wasWeb ? "Session expired — please sign in again" : "Session expired — please reopen from the Telegram bot");
      }
    }

    const ctype = (resp.headers.get("content-type") || "").toLowerCase();
    let body = null;
    try {
      body = ctype.includes("json") ? await resp.json() : await resp.text();
    } catch (_e) {
      body = null;
    }

    if (!resp.ok) {
      // 429 / Cloudflare challenge / HTML error page → friendly, never raw HTML.
      if (_isChallenge(resp.status, ctype, body)) throw _rateError();
      let msg = "HTTP " + resp.status;
      if (body && typeof body === "object") {
        msg = body.detail || body.error || (body.detail && body.detail.error) || msg;
        if (msg && typeof msg === "object") msg = msg.error || JSON.stringify(msg);
      } else if (typeof body === "string" && body) {
        msg = _cleanText(body, resp.status);
      }
      throw new Error(msg);
    }
    _rlClear();
    return body;
  }

  // Public call: adds bounded exponential backoff for idempotent (GET/HEAD)
  // requests. Mutations are NOT auto-retried (avoids double writes) — they
  // surface the friendly rate-limit state so the UI can offer a manual Retry.
  async function api(path, options) {
    const opts = options || {};
    const method = String(opts.method || "GET").toUpperCase();
    const canRetry = method === "GET" || method === "HEAD";
    let attempt = 0;
    const _isWrite = method !== "GET" && method !== "HEAD";
    for (;;) {
      try {
        const out = await _raw(path, opts);
        if (_isWrite) invalidate();   // owner 2026-09-20: never serve a stale list after a write
        return out;
      } catch (err) {
        if (err && err.rateLimited) {
          if (canRetry && attempt < BACKOFF_MS.length) {
            const wait = BACKOFF_MS[attempt];
            attempt++;
            _rlStart({ path: path, attempt: attempt, max: BACKOFF_MS.length + 1, delayMs: wait, exhausted: false });
            await new Promise(function (r) { setTimeout(r, wait); });
            continue;
          }
          _rlStart({ path: path, attempt: attempt + 1, max: BACKOFF_MS.length + 1, delayMs: 0, exhausted: true });
          throw err;
        }
        throw err;
      }
    }
  }

  // Unwrap the {success, data} envelope; returns data if present else the whole object.
  function unwrap(j) {
    if (j && typeof j === "object" && "data" in j) return j.data;
    return j;
  }

  // ── Authentication ──────────────────────────────────────
  async function authenticate(silent) {
    !silent;
    if (!tg || !tg.initData) return false;
    const resp = await api("/api/telegram/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: tg.initData }),
    });
    const data = unwrap(resp);
    if (data && data.token) {
      jwt = data.token;
      staff.id = data.user_id != null ? data.user_id : null;
      staff.name = data.username || ("#" + data.user_id);
      return true;
    }
    // Also accept raw token at top level
    if (resp && resp.token) {
      jwt = resp.token;
      staff.id = resp.user_id != null ? resp.user_id : null;
      staff.name = resp.username || ("#" + resp.user_id);
      return true;
    }
    return false;
  }

  function isAuthed() { return !!jwt; }
  function getStaff() { return staff; }
  function getTg() { return tg; }

  // ── 401 recovery ────────────────────────────────────────
  async function _recoverAuth() {
    if (isWebLogin) {
      // Standalone path: try refresh once, else force back to login.
      if (refreshTok) {
        try {
          const r = await fetch(_u("/auth/refresh"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: refreshTok }),
          });
          if (r.ok) {
            const d = await r.json();
            if (d && d.access_token) {
              jwt = d.access_token;
              refreshTok = d.refresh_token || refreshTok;
              if (d.user) staff = { name: d.user.name || d.user.username, id: d.user.username || null, role: d.user.role || "" };
              _persistAuth(); // also re-mirrors 'psv_jwt' for the native shell
              return true;
            }
          }
        } catch (_e) {}
      }
      _forceLogout();
      return false;
    }
    // Telegram path (UNCHANGED): silently re-validate initData once.
    jwt = null;
    return await authenticate(true);
  }

  // ── Standalone login / logout ───────────────────────────
  async function login(username, password, remember) {
    const r = await fetch(_u("/auth/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username, password: password }),
    });
    let body = null;
    try { body = await r.json(); } catch (_e) {}
    if (!r.ok) {
      let m = (body && (body.detail || body.error)) || ("Login failed (HTTP " + r.status + ")");
      if (m && typeof m === "object") m = m.error || JSON.stringify(m);
      throw new Error(m || "Login failed");
    }
    if (!body || !body.access_token) throw new Error("Login failed — no token returned");
    jwt = body.access_token;
    refreshTok = body.refresh_token || null;
    const u = body.user || {};
    staff = { name: u.name || u.username || username, id: u.username || null, role: u.role || "staff", username: u.username || username, photo_url: u.photo_url || "" };
    isWebLogin = true;
    if (remember === false) _clearStored(); else _persistAuth();
    _syncShellJwt(); // mirror access token for the native shell (FCM registration), even when "remember me" is off
    return staff;
  }

  function logout() { _forceLogout(); }
  function loadStoredAuth() { return _loadStored(); }
  function usesWebLogin() { return isWebLogin; }

  // ── Helpers ─────────────────────────────────────────────
  function genClientRef() {
    if (window.crypto && crypto.randomUUID) {
      try { return crypto.randomUUID(); } catch (_e) {}
    }
    return "cl-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
  }

  // Voucher preview V-WEB-... (backend assigns the real sequence on record)
  function genVoucherNo() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const ts = d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
      p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
    const suffix = Math.floor(Math.random() * 900 + 100);
    return "V-WEB-" + ts + "-" + suffix;
  }

  function fmtKs(n) {
    const v = Number(n) || 0;
    return v.toLocaleString("en-US");
  }

  function todayMMT() {
    // MMT = UTC+6:30
    const now = new Date(Date.now() + (6 * 60 + 30) * 60000);
    return now.toISOString().slice(0, 10);
  }

  function monthMMT() {
    return todayMMT().slice(0, 7);
  }

  function toast(msg, type) {
    // Vant Toast is registered globally; guard in case not ready yet.
    if (window.vant && vant.showToast) {
      vant.showToast({ message: msg || "", type: type || "text", duration: 1800 });
    } else {
      // fallback
      console.warn("[toast]", msg);
    }
  }

  function haptic(type) {
    try {
      if (tg && tg.HapticFeedback) {
        const m = { light: "impactLight", medium: "impactMedium", heavy: "impactHeavy", rig: "rigidImpact", soft: "softImpact" };
        const evt = m[type] || "impactOccurred";
        if (evt === "impactOccurred") tg.HapticFeedback.impactOccurred("light");
        else if (tg.HapticFeedback[evt]) tg.HapticFeedback[evt]();
      }
    } catch (_e) {}
  }

  // ── Vant en-US locale (default is zh-CN) ───────────────
  if (window.vant && vant.Locale && typeof vant.Locale.use === "function") {
    vant.Locale.use("en-US", {
      name: "Name", tel: "Phone", save: "Save", clear: "Clear", undo: "Undo",
      cancel: "Cancel", confirm: "Confirm", delete: "Delete", loading: "Loading...",
      noCoupon: "No coupons", nameEmpty: "Please fill in the name", addContact: "Add contact",
      telInvalid: "Malformed phone number",
      vanCalendar: { end: "End", start: "Start", title: "Calendar", weekdays: ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"], monthTitle: (y, m) => y + "/" + m, rangePrompt: (n) => "Choose no more than " + n + " days" },
      vanCascader: { select: "Select" },
      vanPagination: { prev: "Previous", next: "Next" },
      vanPullRefresh: { pulling: "Pull to refresh...", loosing: "Loose to refresh..." },
      vanSubmitBar: { label: "Total:" },
      vanCoupon: { unlimited: "Unlimited", discount: (d) => d * 10 + "% off", condition: (c) => "At least " + c },
      vanCouponCell: { title: "Coupon", count: (n) => "You have " + n + " coupons" },
      vanCouponList: { exchange: "Exchange", close: "Close", enable: "Available", disabled: "Unavailable", placeholder: "Coupon code" },
      vanAddressEdit: { area: "Area", areaEmpty: "Please select a receiving area", addressEmpty: "Address can not be empty", addressDetail: "Address", defaultAddress: "Set as the default address" },
      vanAddressList: { add: "Add new address" },
    });
  }

  // ── 80mm thermal voucher rendering + printing ────────────
  function _dataUrlToBlob(dataUrl) {
    const parts = String(dataUrl).split(",");
    const mime = (/data:(.*?);/.exec(parts[0]) || [])[1] || "image/png";
    const bin = atob(parts[1] || "");
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return new Blob([u8], { type: mime });
  }

  function _voucherLines(data) {
    data = data || {};
    const items = Array.isArray(data.items) ? data.items : [];
    const L = [];
    const hr = () => L.push({ text: "--------------------------------", size: 14, align: "center" });
    const nf = (n) => (Number(n) || 0).toLocaleString("en-US");
    // Real PS VIBE logo (the one WITH the wordmark) printed as an image at the top of the
    // receipt (owner 2026-09-20 15:23). nativeSkip keeps a text header for any build or
    // fallback path that cannot render image lines.
    L.push({ img: "asset:print/psvibe-logo.png", size: 340, align: "center" });
    L.push({ text: "PS VIBE", bold: true, size: 36, align: "center", nativeSkip: true });
    L.push({ text: "Play The Game. Share The VIBE!", size: 13, align: "center" });
    L.push({ text: "PS5 Gaming Lounge", size: 13, align: "center" });
    L.push({ text: "", size: 6 });
    hr();
    L.push({ text: "VOUCHER: " + (data.voucher_no || "-"), bold: true, size: 18 });
    if (data.date) L.push({ text: "Date: " + data.date + (data.time ? " " + data.time : ""), size: 15 });
    if (data.staff) L.push({ text: "Staff: " + data.staff, size: 15 });
    if (data.member) L.push({ text: "Member: " + data.member, size: 15 });
    hr();
    if (items.length) {
      for (const it of items) {
        const qty = Number(it.qty) || 0;
        const price = Number(it.price) || 0;
        L.push({ text: (it.name || "-"), size: it.plain ? 14 : 15, align: it.align || "left" });
        if (!it.plain) L.push({ text: "   " + qty + " x " + nf(price) + " = " + nf(qty * price), size: 14, align: "right" });
      }
    } else {
      L.push({ text: "(no items)", size: 14, align: "center" });
    }
    hr();
    // Owner 2026-09-20 15:57: member-card plays show a full wallet-balance breakdown
    // (before / deducted / after) instead of a one-line wallet remark.
    if (data.wallet && (data.wallet.before || data.wallet.ded || data.wallet.after)) {
      hr();
      L.push({ text: "MEMBER BALANCE (min)", bold: true, size: 15, align: "center" });
      L.push({ text: "Balance before : " + nf(data.wallet.before) + " min", size: 15 });
      L.push({ text: "Deducted       : -" + nf(data.wallet.ded) + " min", bold: true, size: 15 });
      L.push({ text: "Balance after  : " + nf(data.wallet.after) + " min", bold: true, size: 15 });
    }
    L.push({ text: "TOTAL: " + nf(data.total) + " Ks", bold: true, size: 20, align: "right" });
    if (data.payment) L.push({ text: "Paid: " + data.payment, size: 15 });
    if (data.balance && !data.wallet) L.push({ text: "💳 " + data.balance, size: 15 });
    if (data.notes) L.push({ text: "Note: " + data.notes, size: 14 });
    hr();
    L.push({ text: "Thank you! Please come again", size: 14, align: "center" });
    L.push({ text: "PS VIBE", size: 14, align: "center" });
    return L;
  }

  // Render voucher to a 576px-wide PNG (80mm @ 203dpi). Returns dataURL.
  function renderVoucherPng(data) {
    return new Promise((resolve, reject) => {
      try {
        // Paper-aware: 80mm -> 576px, 58mm -> 384px (matches the printer dot width).
        // The design is authored at 576px; scale everything down so 58mm never overflows.
        const W = 576; // vouchers are always 80mm — never shrink to the 58mm canvas
        const S = W / 576;
        // Side margin: some 80mm printers have a printable width a little narrower than 576
        // dots, which clipped the first/last characters (owner receipts 2026-09-20 14:43).
        const PAD = Math.round(26 * S);
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        const lines = _voucherLines(data);
        // The line `size` values were authored for the printer's OWN ESC/POS font (a normal
        // line = 24 dots, wide = 32, big = 48). Rendering them 1:1 as canvas pixels made the
        // raster voucher far smaller than the text version (owner 2026-09-20 14:38:
        // "Voucher က size မမှန်တော့ပြန်ဘူး"). Map size -> printer dots so the raster matches.
        const dp = (n) => {
          const sz = Number(n) || 16;
          if (sz >= 28) return 48;
          if (sz >= 20) return 32;
          if (sz >= 15) return 24;
          return Math.max(16, Math.round(sz * 1.5));
        };
        let H = PAD * 2;
        for (const ln of lines) {
          const _n = Math.max(1, Math.ceil(String(ln.text || "").length / 40));
          H += _n * (Math.round(dp(ln.size) * S) + Math.round(10 * S));
        }
        canvas.width = W;
        canvas.height = Math.max(H, 160);
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#000000";
        ctx.textBaseline = "top";
        let y = PAD;
        for (const ln of lines) {
          const size = Math.max(14, Math.round(dp(ln.size) * S));
          // Per-line font stack: monospace keeps the ASCII layout aligned, the Myanmar
          // fallbacks let the WebView draw Burmese glyphs (which are then rasterised).
          const _mm = /[^\x00-\x7F]/.test(String(ln.text || ""));
          const _fam = _mm
            ? "'Noto Sans Myanmar','Myanmar Text','Padauk','Pyidaungsu','Zawgyi-One','Courier New',monospace"
            : "'Courier New', monospace";
          ctx.font = (ln.bold ? "bold " : "") + size + "px " + _fam;
          ctx.textAlign = ln.align || "left";
          const x = ln.align === "center" ? W / 2 : (ln.align === "right" ? W - PAD : PAD);
          // Wrap to the printable width so a long item name can never run off the paper.
          const maxW = W - PAD * 2;
          const words = String(ln.text == null ? "" : ln.text).split(" ");
          let cur = "";
          const flush = (t) => { if (t) { ctx.fillText(t, x, y); y += size + Math.round(10 * S); } };
          if (maxW > 20) {
            for (const wd of words) {
              const cand = cur ? (cur + " " + wd) : wd;
              if (cur && ctx.measureText(cand).width > maxW) { flush(cur); cur = wd; }
              else cur = cand;
            }
            flush(cur);
          } else {
            ctx.fillText(String(ln.text || ""), x, y);
            y += size + Math.round(10 * S);
          }
        }
        resolve(canvas.toDataURL("image/png"));
      } catch (e) { reject(e); }
    });
  }

  // Print a voucher: native Bluetooth plugin → share → download. Never throws.
  // Preferred path = printVoucherText: sends the voucher as TEXT through the printer's own
  // font (crisp, no 203dpi raster blur, auto-fits the paper). Falls back to printImage.
  async function _ensurePrinterConnected(plugin) {
    try {
      const saved = JSON.parse(localStorage.getItem("psv_printer") || "null");
      const st = plugin.status ? await plugin.status().catch(function () { return null; }) : null;
      if (saved && saved.address && !(st && st.connected)) {
        await plugin.connect({ address: saved.address, mode: "auto" });
      }
    } catch (_connErr) { /* fall through — the print call will report if unusable */ }
  }

  function paperWidth() {
    try { return parseInt(localStorage.getItem("psv_paper_mm"), 10) === 58 ? 58 : 80; }
    catch (_e) { return 80; }
  }

  // Persist + push the paper width to the native plugin (no-op in web preview).
  async function setPaperWidth(mm) {
    mm = (Number(mm) === 58) ? 58 : 80;
    try { localStorage.setItem("psv_paper_mm", String(mm)); } catch (_e) {}
    const cap = window.Capacitor;
    const plugin = cap && cap.Plugins && cap.Plugins.PSVPrinter;
    if (plugin && typeof plugin.setPaperWidth === "function") {
      try { await plugin.setPaperWidth({ mm: mm }); } catch (_e) {}
    }
    return mm;
  }

  // Optional print darkness (0 = OFF, default). Native sends raw ESC/POS DC2 # n when >0.
  async function setPrintDensity(level) {
    level = Math.max(0, Math.min(8, Number(level) || 0));
    try { localStorage.setItem("psv_print_density", String(level)); } catch (_e) {}
    const cap = window.Capacitor;
    const plugin = cap && cap.Plugins && cap.Plugins.PSVPrinter;
    if (plugin && typeof plugin.setPrintDensity === "function") {
      try { await plugin.setPrintDensity({ level: level }); } catch (_e) {}
    }
    return level;
  }

  async function printVoucher(data) {
    const cap = window.Capacitor;
    const plugin = cap && cap.Plugins && cap.Plugins.PSVPrinter;

    // Burmese (or any non-ASCII) cannot go through ESC/POS text — the native sanitizer maps
    // it to "?" (owner 2026-09-20 11:56: "?? တွေပဲ ပေါ်နေတာ · mm font support မလုပ်တာနဲ့တူ").
    // Detect it up-front and use the raster path so Myanmar glyphs actually print.
    const _qLines = _voucherLines(data);
    const _hasNonAscii = _qLines.some((l) => /[^\x00-\x7F]/.test(String((l && l.text) || "")));

    // ── Preferred #0: RAW in-app Canvas render + raw ESC/POS write (2026-09-20) ──
    // The owner: "text mode was correct, the raster became tiny" — the DantSu library scales /
    // centres an <img> against the paper profile, so a stale 58mm profile printed the whole
    // receipt at 384 dots. This path renders at 576 dots and writes GS v 0 raster bands
    // straight to the printer stream: no library scaling, no profile, always 80mm.
    if (plugin && typeof plugin.printVoucherRaw === "function") {
      try {
        await _ensurePrinterConnected(plugin);
        try { await plugin.setPaperWidth({ mm: 80 }); } catch (_e) {}
        const _rr = await plugin.printVoucherRaw({ lines: _qLines, cut: true });
        const _rw = (_rr && _rr.width) || 576;
        toast("Voucher sent ✓ · " + _rw + " dots (80mm)", "success");
        return true;
      } catch (e) {
        try { toast("raw print failed → fallback", "warning"); } catch (_e2) {}
      }
    }

    // ── Preferred #1: native in-app Canvas render (self-contained APK, 2026-09-20) ──
    // The bundled Android app draws the voucher with Android Canvas and prints it as a
    // monochrome raster, so Myanmar + the full layout print correctly with NO web-side
    // canvas and NO dependency on the served web app. Falls through when the plugin is absent.
    if (plugin && typeof plugin.printVoucherNative === "function") {
      try {
        await _ensurePrinterConnected(plugin);
        // VOUCHERS ARE ALWAYS 80mm (owner 2026-09-20). A stale 58mm profile made the whole
        // raster print at 384 dots = 33% smaller ("အရမ်းသေး / 80mm မမှန်"). Never let the
        // stored preference shrink a voucher.
        if (typeof plugin.setPaperWidth === "function") {
          try { await plugin.setPaperWidth({ mm: 80 }); } catch (_e) {}
        }
        const _r = await plugin.printVoucherNative({ lines: _qLines, cut: true });
        const _w = (_r && _r.width) || 576;
        toast("Voucher sent ✓ · " + _w + " dots (80mm)", "success");
        return true;
      } catch (e) {
        // fall through to the text / raster paths below
      }
    }

    // ── Preferred #2: crisp native text path (ASCII only) ──
    if (plugin && typeof plugin.printVoucherText === "function" && !_hasNonAscii) {
      try {
        await _ensurePrinterConnected(plugin);
        if (typeof plugin.setPaperWidth === "function") {
          try { await plugin.setPaperWidth({ mm: paperWidth() }); } catch (_e) {}
        }
        await plugin.printVoucherText({ lines: _qLines, cut: true });
        toast("Voucher sent to printer ✓", "success");
        return true;
      } catch (e) {
        // fall through to the image path below
      }
    }

    // ── Fallback: raster image (older bundles / text path unavailable) ──
    let dataUrl;
    try { dataUrl = await renderVoucherPng(data); }
    catch (e) { toast("Voucher render failed: " + (e && e.message ? e.message : e), "fail"); return false; }
    const base64 = String(dataUrl).split(",")[1] || "";
    if (plugin && typeof plugin.printImage === "function") {
      try {
        await _ensurePrinterConnected(plugin);
        await plugin.printImage({ base64: base64 });
        toast("Voucher sent to printer ✓", "success");
        return true;
      } catch (e) {
        toast("Print failed — saving image instead", "warning");
      }
    }
    try {
      const fname = "voucher-" + ((data && data.voucher_no) || Date.now()) + ".png";
      const file = new File([_dataUrlToBlob(dataUrl)], fname, { type: "image/png" });
      if (navigator.canShare && navigator.canShare({ files: [file] }) && navigator.share) {
        await navigator.share({ files: [file], title: "PS VIBE Voucher" });
        return true;
      }
    } catch (e) {
      if (e && e.name === "AbortError") return false;
    }
    try {
      const a = document.createElement("a");
      a.href = dataUrl;
      a.download = "voucher-" + ((data && data.voucher_no) || Date.now()) + ".png";
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      toast("Voucher image downloaded", "success");
      return true;
    } catch (e) {
      toast("Print/share unavailable on this device", "fail");
      return false;
    }
  }

  // ── Authenticated binary fetch → object URL (proof images) ──
  // Same Bearer + 401-refresh path as api(); returns a blob: URL that an <img>
  // can load. Cached per path so re-opening a sheet does not refetch.
  const _blobCache = Object.create(null); // path → { url, ts }
  async function blobUrl(path, options) {
    const hit = _blobCache[path];
    if (hit) return hit.url;
    const opts = options || {};
    const headers = Object.assign({}, opts.headers || {});
    if (jwt) headers["Authorization"] = "Bearer " + jwt;

    let resp;
    try {
      resp = await fetch(_u(path), Object.assign({}, opts, { headers }));
    } catch (e) {
      throw new Error("Network error — check your internet connection");
    }
    if (resp.status === 401) {
      const wasWeb = isWebLogin;
      const ok = await _recoverAuth();
      if (ok) {
        headers["Authorization"] = "Bearer " + jwt;
        resp = await fetch(_u(path), Object.assign({}, opts, { headers }));
      } else {
        throw new Error(wasWeb ? "Session expired — please sign in again" : "Session expired — please reopen from the Telegram bot");
      }
    }
    if (!resp.ok) {
      let msg = "HTTP " + resp.status;
      try {
        const ct = (resp.headers.get("content-type") || "").toLowerCase();
        if (ct.indexOf("json") >= 0) {
          const b = await resp.json();
          msg = (b && (b.detail || b.error)) || msg;
          if (msg && typeof msg === "object") msg = msg.error || JSON.stringify(msg);
        }
      } catch (_e) {}
      throw new Error(msg);
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    _blobCache[path] = { url: url, ts: Date.now() };
    return url;
  }
  function revokeBlob(key) {
    const hit = _blobCache[key];
    if (hit) {
      try { URL.revokeObjectURL(hit.url); } catch (_e) {}
      delete _blobCache[key];
    }
  }

  window.PSV = {
    api, apiCached, invalidate, retryNow, upload, unwrap, authenticate, isAuthed, getStaff, getTg,
    apiOrigin: function () { return API_ORIGIN; },
    appBuild: APP_BUILD,
    apiPlain: apiPlain,
    apiDirect: apiDirect,
    uploadDirect: uploadDirect,
    uploadPlain: uploadPlain,
    apiToken: apiToken,
    avatarSrc: avatarSrc,
    setLocalAvatar: setLocalAvatar,
    bumpAvatar: bumpAvatar,
    blobUrl, revokeBlob,
    login, logout, loadStoredAuth, usesWebLogin,
    renderVoucherPng, printVoucher, setPaperWidth, getPaperWidth: paperWidth, setPrintDensity,
    genClientRef, genVoucherNo, fmtKs, todayMMT, monthMMT, toast, haptic,
  };
})();
