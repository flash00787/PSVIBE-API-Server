/* PS VIBE Mini App — API helper module (no build step, plain ES module via window).
 *
 * Auth flow (unchanged from Phase 1/2):
 *   Telegram initData  →  POST /api/telegram/validate  →  JWT (memory only)  →  Bearer calls.
 *
 * Exposes: window.PSV = { api, authenticate, getStaff, genClientRef, genVoucherNo, fmt, ... }
 */
(function () {
  "use strict";

  // ── Telegram WebApp handle ──────────────────────────────
  const tg = window.Telegram && window.Telegram.WebApp;

  let jwt = null;        // in-memory only (XSS-safe — never localStorage)
  let staff = { name: "", id: null };

  // ── Low-level API call ──────────────────────────────────
  async function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (jwt) headers["Authorization"] = "Bearer " + jwt;

    let resp;
    try {
      resp = await fetch(path, Object.assign({}, options, { headers }));
    } catch (e) {
      throw new Error("Network error — အင်တာနက် ချိတ်ဆက်မှု စစ်ပါ");
    }

    // Auto re-validate once on 401
    if (resp.status === 401) {
      jwt = null;
      const ok = await authenticate(true /* silent */);
      if (ok) {
        headers["Authorization"] = "Bearer " + jwt;
        resp = await fetch(path, Object.assign({}, options, { headers }));
      } else {
        throw new Error("Session expired — bot ကနေ ပြန်ဖွင့်ပါ");
      }
    }

    let body = null;
    const ctype = resp.headers.get("content-type") || "";
    try {
      body = ctype.includes("json") ? await resp.json() : await resp.text();
    } catch (_e) {
      body = null;
    }

    if (!resp.ok) {
      let msg = "HTTP " + resp.status;
      if (body && typeof body === "object") {
        msg = body.detail || body.error || body.detail?.error || msg;
      } else if (typeof body === "string" && body) {
        msg = body;
      }
      throw new Error(msg);
    }
    return body;
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

  window.PSV = {
    api, unwrap, authenticate, isAuthed, getStaff, getTg,
    genClientRef, genVoucherNo, fmtKs, todayMMT, monthMMT, toast, haptic,
  };
})();
