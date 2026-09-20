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
  const _cache = Object.create(null);   // key → { ts, data }
  const _inflight = Object.create(null); // key → Promise

  function _cacheKey(path, options) {
    const m = String((options && options.method) || "GET").toUpperCase();
    return m + " " + path;
  }
  function invalidate(prefix) {
    const p = prefix || "";
    for (const k in _cache) { if (!p || k.indexOf(p) >= 0) delete _cache[k]; }
  }
  function apiCached(path, options, ttlMs) {
    const opts = options || {};
    const m = String(opts.method || "GET").toUpperCase();
    if (m !== "GET" && m !== "HEAD") return api(path, options);
    const key = _cacheKey(path, opts);
    const ttl = ttlMs == null ? CACHE_TTL : ttlMs;
    const hit = _cache[key];
    if (hit && (Date.now() - hit.ts) < ttl) return Promise.resolve(hit.data);
    if (_inflight[key]) return _inflight[key];
    const pr = api(path, options).then(function (data) {
      _cache[key] = { ts: Date.now(), data: data };
      delete _inflight[key];
      return data;
    }, function (err) {
      delete _inflight[key];
      throw err;
    });
    _inflight[key] = pr;
    return pr;
  }
  function retryNow() { invalidate(); _emit("psv:retry", {}); }

  // ── Low-level API call ──────────────────────────────────
  async function _raw(path, options) {
    const headers = Object.assign({}, options.headers || {});
    if (jwt) headers["Authorization"] = "Bearer " + jwt;

    let resp;
    try {
      resp = await fetch(path, Object.assign({}, options, { headers }));
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
        resp = await fetch(path, Object.assign({}, options, { headers }));
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
    for (;;) {
      try {
        return await _raw(path, opts);
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
          const r = await fetch("/auth/refresh", {
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
    const r = await fetch("/auth/login", {
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
    staff = { name: u.name || u.username || username, id: u.username || null, role: u.role || "staff", username: u.username || username };
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
    L.push({ text: "PS VIBE", bold: true, size: 36, align: "center" });
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
        L.push({ text: (it.name || "-"), size: 15 });
        L.push({ text: "   " + qty + " x " + nf(price) + " = " + nf(qty * price), size: 14, align: "right" });
      }
    } else {
      L.push({ text: "(no items)", size: 14, align: "center" });
    }
    hr();
    L.push({ text: "TOTAL: " + nf(data.total) + " Ks", bold: true, size: 20, align: "right" });
    if (data.payment) L.push({ text: "Paid: " + data.payment, size: 15 });
    if (data.balance) L.push({ text: "Balance: " + data.balance, size: 15 });
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
        const W = 576;
        const PAD = 20;
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        const lines = _voucherLines(data);
        let H = PAD * 2;
        for (const ln of lines) H += (ln.size || 16) + 8;
        canvas.width = W;
        canvas.height = Math.max(H, 160);
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#000000";
        ctx.textBaseline = "top";
        let y = PAD;
        for (const ln of lines) {
          const size = ln.size || 16;
          ctx.font = (ln.bold ? "bold " : "") + size + "px 'Courier New', monospace";
          ctx.textAlign = ln.align || "left";
          const x = ln.align === "center" ? W / 2 : (ln.align === "right" ? W - PAD : PAD);
          ctx.fillText(String(ln.text == null ? "" : ln.text), x, y);
          y += size + 8;
        }
        resolve(canvas.toDataURL("image/png"));
      } catch (e) { reject(e); }
    });
  }

  // Print a voucher: native Bluetooth plugin → share → download. Never throws.
  async function printVoucher(data) {
    let dataUrl;
    try { dataUrl = await renderVoucherPng(data); }
    catch (e) { toast("Voucher render failed: " + (e && e.message ? e.message : e), "fail"); return false; }
    const base64 = String(dataUrl).split(",")[1] || "";
    const cap = window.Capacitor;
    const plugin = cap && cap.Plugins && cap.Plugins.PSVPrinter;
    if (plugin && typeof plugin.printImage === "function") {
      try {
        // The webapp has no pairing step of its own, but the native plugin REQUIRES an
        // active connection (requireConnection) — without this the print silently fell
        // back to "saving image instead". Connect to the saved printer first (2026-09-20).
        try {
          const saved = JSON.parse(localStorage.getItem("psv_printer") || "null");
          const st = plugin.status ? await plugin.status().catch(function () { return null; }) : null;
          if (saved && saved.address && !(st && st.connected)) {
            await plugin.connect({ address: saved.address, mode: "auto" });
          }
        } catch (_connErr) { /* fall through — printImage will report if unusable */ }
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
      resp = await fetch(path, Object.assign({}, opts, { headers }));
    } catch (e) {
      throw new Error("Network error — check your internet connection");
    }
    if (resp.status === 401) {
      const wasWeb = isWebLogin;
      const ok = await _recoverAuth();
      if (ok) {
        headers["Authorization"] = "Bearer " + jwt;
        resp = await fetch(path, Object.assign({}, opts, { headers }));
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
    api, apiCached, invalidate, retryNow, unwrap, authenticate, isAuthed, getStaff, getTg,
    blobUrl, revokeBlob,
    login, logout, loadStoredAuth, usesWebLogin,
    renderVoucherPng, printVoucher,
    genClientRef, genVoucherNo, fmtKs, todayMMT, monthMMT, toast, haptic,
  };
})();
