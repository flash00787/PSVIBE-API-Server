/* PS VIBE Telegram Mini App — Phase 3 full-feature SPA
 * Vue 3 (global build) + Vant 4 (UMD build) — no build step, static files only.
 *
 * Architecture:
 *   - window.PSV (api.js)  → auth + fetch helper
 *   - tiny hash router      → #/route → component mapping
 *   - Vant components registered globally via app.use(vant)
 *
 * Views map to sale-bot handlers (see final coverage matrix in deliverables).
 */
(function () {
  "use strict";

  const { createApp, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } = Vue;
  const vant = window.vant;
  const PSV = window.PSV;
  const tg = PSV.getTg();

  // ── App vs Telegram mode ────────────────────────────────
  // App (standalone Android/WebView) mode = no Telegram initData. Mirrors the
  // inline head script in index.html. All premium styling is scoped under
  // html.psv-native, so the Telegram Mini App look is untouched.
  const NATIVE = !(tg && tg.initData);
  const TABS_TG = ["/", "/sales", "/members", "/bookings", "/more"];
  const TABS_NATIVE = ["/", "/sales", "/bookings", "/print", "/more"];

  // ── Theme sync ──────────────────────────────────────────
  function applyTheme() {
    const scheme = (tg && tg.colorScheme) || "dark";
    const root = document.documentElement;
    root.classList.toggle("dark", scheme === "dark");
    root.setAttribute("data-theme", scheme);
    const accent = (tg && tg.themeParams && tg.themeParams.button_color) || ((tg && tg.initData) ? "#6366f1" : "#f0b429");
    root.style.setProperty("--van-primary-color", accent);
  }
  applyTheme();
  if (tg && tg.onEvent) {
    try { tg.onEvent("themeChanged", applyTheme); } catch (_e) {}
  }

  // ── Shared helpers ──────────────────────────────────────
  const showToast = (msg, type) => PSV.toast(msg, type);
  const haptic = (t) => PSV.haptic(t);
  const fKs = (n) => PSV.fmtKs(n);
  // WAVE 3: several staff endpoints answer HTTP 200 with {success:false, error}
  // (the session move/swap guards do this). PSV.api only throws on non-2xx, so
  // unwrap those refusals into a real Error and surface the message verbatim.
  const asOk = (body) => {
    if (body && typeof body === "object" && body.success === false) {
      const e = new Error(body.error || body.message || "Request failed");
      if (body.code != null) e.code = body.code;
      throw e;
    }
    return body;
  };
  const isOwnerBlock = (e) => /owner|admin|not allowed|pin|403|manage/i.test(String((e && e.message) || e || ""));

  // ── Auth gate (standalone staff login) ──────────────────
  // Telegram path is NEVER gated (initData drives auth). Only the standalone
  // (no-initData) Android/WebView path shows the login screen.
  const authGate = ref(false); // true → show LoginScreen
  // 24h "HH:MM" → 12h "hh:mm AM/PM" (00:00→12:00 AM, 12:00→12:00 PM, 13:00→01:00 PM)
  const fmt12h = (v) => {
    if (!v) return "";
    const m = String(v).trim().match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return String(v);
    const hh = parseInt(m[1], 10);
    const mm = m[2];
    if (isNaN(hh) || hh < 0 || hh > 23) return String(v);
    const mer = hh < 12 ? "AM" : "PM";
    const h12 = hh % 12 || 12;
    return String(h12).padStart(2, "0") + ":" + mm + " " + mer;
  };

  // ── Tiny Hash Router (WAVE 6: single-step back) ──────────
  // WAVE 6 — BACK. The app runs in a Capacitor WebView (remote URL) *and* in
  // Telegram/desktop. Android back must move exactly ONE step:
  //     close the top overlay  →  previous screen  →  previous tab  →  exit app.
  //   * navigation uses pushState/replaceState (same-document — never a reload);
  //   * root-tab → root-tab switches REPLACE (back must not walk every tab tap);
  //   * an in-app stack is the source of truth for router.back().
  const ROOT_TABS = ["/", "/sales", "/members", "/bookings", "/print", "/more"];
  const isRootTab = (p) => ROOT_TABS.indexOf(p) >= 0;

  const router = {
    routes: {},
    current: ref("/"),
    query: ref({}),
    _stack: [],       // non-root breadcrumbs: [{path, hash}]
    _tabStack: [],    // root-tab history (Home → Sales → …)
    _overlays: [],    // LIFO overlay closers registered by open sheets/pickers/dialogs
    register(path, component, title) { this.routes[path] = component; this.routes[path + "__title"] = title; },
    title(path) { return this.routes[path + "__title"] || "PS VIBE"; },
    onlyPath(path) { const q = String(path || "").indexOf("?"); return q >= 0 ? String(path).slice(0, q) : String(path || ""); },
    // Parse "#/route?query" → update current + query refs. Returns the route.
    applyHash(h) {
      let s = String(h || "").replace(/^#/, "");
      let qs = "";
      const qi = s.indexOf("?");
      if (qi >= 0) { qs = s.slice(qi + 1); s = s.slice(0, qi); }
      const q = {};
      if (qs) qs.split("&").forEach((pair) => {
        if (!pair) return;
        const eq = pair.indexOf("=");
        const k = eq >= 0 ? pair.slice(0, eq) : pair;
        const v = eq >= 0 ? pair.slice(eq + 1) : "";
        try { q[decodeURIComponent(k)] = decodeURIComponent(v); } catch (_e) { q[k] = v; }
      });
      this.query.value = q;
      this.current.value = (!s || s === "/" || !this.routes[s]) ? "/" : s;
      return this.current.value;
    },
    resolve() { return this.applyHash(window.location.hash); },
    _write(how, path) {
      try {
        const st = { p: path };
        if (how === "push") history.pushState(st, "", "#" + path);
        else history.replaceState(st, "", "#" + path);
      } catch (_e) { try { window.location.hash = "#" + path; } catch (_e2) {} }
    },
    push(path) {
      const target = this.onlyPath(path);
      const cur = this.current.value;
      if (window.location.hash === "#" + path) { this.applyHash(window.location.hash); return; }
      // Root tab → root tab: REPLACE, so back does not replay every tab tap.
      if (isRootTab(cur) && isRootTab(target)) { this.replace(path, true); return; }
      // A "back to the screen we came from" push (PageHeader `back`) steps back
      // instead of stacking a duplicate entry.
      const top = this._stack[this._stack.length - 1];
      if (top && this.onlyPath(top.path) === target) { this._stack.pop(); this.replace(path); return; }
      if (cur) this._stack.push({ path: cur, hash: window.location.hash || "#" + cur });
      this._write("push", path);
      this.applyHash("#" + path);
    },
    replace(path, fromTab) {
      const target = this.onlyPath(path);
      const cur = this.current.value;
      if (fromTab && isRootTab(cur) && isRootTab(target) && cur !== target) {
        if (this._tabStack[this._tabStack.length - 1] !== cur) this._tabStack.push(cur);
        if (this._tabStack.length > 12) this._tabStack.shift();
      }
      this._write("replace", path);
      this.applyHash("#" + path);
    },
    // Views (via the global overlay mixin) register a closer while a sheet is open.
    pushOverlay(fn) { this._overlays.push(fn); },
    popOverlay(fn) { const i = this._overlays.lastIndexOf(fn); if (i >= 0) this._overlays.splice(i, 1); },
    closeTopOverlay() {
      const fn = this._overlays[this._overlays.length - 1];
      if (!fn) return false;
      let ok = true;
      try { ok = fn() !== false; } catch (_e) { ok = false; }
      const i = this._overlays.lastIndexOf(fn);
      if (i >= 0) this._overlays.splice(i, 1);
      return ok;
    },
    // ONE step back. Returns false when there is nowhere left (caller exits the app).
    back() {
      if (this.closeTopOverlay()) return true;
      if (this._stack.length) { this.replace(this._stack.pop().path); return true; }
      if (this._tabStack.length) { this.replace(this._tabStack.pop()); return true; }
      return false;
    },
    start() {
      // System/browser/WebView back. Keep the in-app stacks in sync with it.
      window.addEventListener("popstate", () => {
        const p = this.onlyPath(window.location.hash.replace(/^#/, "")) || "/";
        const last = this._stack[this._stack.length - 1];
        if (last && this.onlyPath(last.path) === p) this._stack.pop();
        else if (this._tabStack[this._tabStack.length - 1] === p) this._tabStack.pop();
        this.applyHash(window.location.hash);
      });
      window.addEventListener("hashchange", () => this.applyHash(window.location.hash));
      this.applyHash(window.location.hash);

      // Android (Capacitor) hardware/gesture back. @capacitor/app is already bundled
      // in the shipped shell, so wiring it here needs NO APK rebuild: registering a
      // listener stops Capacitor calling webView.goBack() itself, giving us full
      // control (single step, close sheets, exit only from a root tab).
      try {
        const Cap = window.Capacitor;
        const AppPlugin = Cap && Cap.Plugins && Cap.Plugins.App;
        if (AppPlugin && typeof AppPlugin.addListener === "function") {
          AppPlugin.addListener("backButton", () => {
            try { haptic("light"); } catch (_e) {}
            let handled = false;
            try { handled = router.back(); } catch (_e) { handled = false; }
            if (!handled) { try { AppPlugin.exitApp(); } catch (_e) {} }
          });
        }
      } catch (_e) { /* not a Capacitor shell */ }

      // Telegram hardware back key → same single-step logic.
      try {
        const tg = PSV.getTg();
        if (tg && tg.BackButton) {
          tg.BackButton.onClick(() => {
            try { haptic("light"); } catch (_e) {}
            let handled = false;
            try { handled = router.back(); } catch (_e) { handled = false; }
            if (!handled && tg.close) tg.close();
          });
          tg.BackButton.show();
        }
      } catch (_e) { /* non-Telegram env */ }
    },
  };

  // ── Native-shell push deep-link hook ────────────────────
  // The distributed Android shell (psv-native-bridge.js) can call
  // window.PSVOpenNotification("<id>") when the user taps an FCM notification.
  // The FCM data payload carries `notification_id` (set in api/fcm.py). We mark
  // the row read (best effort) and open the in-app notification center.
  window.PSVOpenNotification = function (id) {
    try {
      if (id != null && id !== "") {
        PSV.api("/api/staff/notifications/" + encodeURIComponent(String(id)) + "/read", { method: "POST" }).catch(function () {});
      }
    } catch (_e) {}
    try { haptic("light"); router.push("/notifications"); } catch (_e2) {}
  };

  // ── Booking deep-link (push tap-through + notification items) ──
  // WAVE 1: a native push (`psv:push` DOM event) or a Notification Center row
  // carries `booking_id`. We stash it here and route to /bookings, whose view
  // opens the matching booking detail (fetching it if it's not in the list).
  const pendingBooking = ref(null);
  function openBooking(id) {
    const bid = (id === 0 || id) ? String(id) : "";
    if (!bid) { try { router.push("/bookings"); } catch (_e) {} return; }
    try { haptic("light"); } catch (_e) {}
    pendingBooking.value = bid;
    try { router.push("/bookings"); } catch (_e) {}
  }
  window.PSVOpenBooking = openBooking;

  // ── Native shell push tap-through (FCM) ─────────────────
  // psv-native-bridge.js dispatches window event `psv:push` with detail
  // { source, type, title, body, booking_id }. Route to the referenced booking
  // (or the live consoles board for session reminders).
  function handleNativePush(ev) {
    try {
      const d = (ev && ev.detail) || {};
      haptic("light");
      if (d.booking_id) { openBooking(d.booking_id); return; }
      const t = String(d.type || "");
      if (t.indexOf("session") >= 0) { router.push("/consoles"); return; }
      router.push("/notifications");
    } catch (_e) {}
  }
  window.addEventListener("psv:push", handleNativePush);

  // ── Reusable components ─────────────────────────────────
  const PageHeader = {
    props: { title: String, back: String, fixedBack: String },
    template: `
      <van-nav-bar :title="title" left-arrow @click-left="onBack" :border="false">
        <template #left><van-icon name="arrow-left" size="20" /></template>
      </van-nav-bar>
    `,
    setup(props) {
      function onBack() {
        const dest = props.fixedBack || props.back || "/more";
        router.push(dest);
        haptic("light");
      }
      return { onBack };
    },
  };

  // ── Reusable premium picker sheet (WAVE 5b) ─────────────
  // Native (standalone) mode ONLY — call sites keep their original van-picker in
  // Telegram mode, so the Mini App look never regresses. Markup/visual only:
  // it renders whatever array it is given (same data flow / API calls).
  const PSVPicker = {
    props: {
      show: Boolean,
      title: { type: String, default: "Select" },
      items: { type: Array, default: () => [] },   // [{value,label,sub,meta,avatar,icon,disabled}]
      modelValue: { type: [String, Number], default: "" },
      searchable: { type: Boolean, default: undefined },
      confirmText: { type: String, default: "Done" },
      emptyText: { type: String, default: "No matches" },
    },
    emits: ["update:show", "confirm", "cancel"],
    template: `
      <van-popup :show="show" position="bottom" round class="psv-pick" :style="{ maxHeight: '82vh' }"
                 :close-on-click-overlay="true" @update:show="(v) => $emit('update:show', v)">
        <div class="psv-pick__sheet">
          <div class="psv-pick__head">
            <div class="psv-pick__title">{{ title }}<span v-if="items.length" class="psv-pick__count">{{ items.length }}</span></div>
            <div class="psv-pick__acts">
              <button class="psv-pick__cancel" @click="close">Cancel</button>
              <button class="psv-pick__done" :disabled="!hasSel" @click="confirm">{{ confirmText }}</button>
            </div>
          </div>
          <div class="psv-pick__search" v-if="showSearch">
            <input class="psv-pick__input" type="search" v-model="q" placeholder="Search…" autocomplete="off" />
          </div>
          <div class="psv-pick__list">
            <div v-for="it in filtered" :key="it.value" class="psv-pick__row"
                 :class="{ 'is-sel': it.value === sel, 'is-dim': it.disabled }" @click="choose(it)">
              <span v-if="it.avatar" class="psv-pick__av">{{ it.avatar }}</span>
              <span v-else class="psv-pick__ic">{{ it.icon || '•' }}</span>
              <div class="psv-pick__main">
                <div class="psv-pick__label">{{ it.label }}</div>
                <div v-if="it.sub || it.meta" class="psv-pick__sub">
                  <span v-if="it.sub">{{ it.sub }}</span>
                  <span v-if="it.meta" class="psv-pick__meta">{{ it.meta }}</span>
                </div>
              </div>
              <span v-if="it.value === sel" class="psv-pick__check">✓</span>
              <van-icon v-else name="arrow" class="psv-pick__chev" />
            </div>
            <div v-if="!filtered.length" class="psv-pick__empty">
              <div class="psv-pick__emptyico">🔍</div>{{ emptyText }}
            </div>
          </div>
        </div>
      </van-popup>
    `,
    setup(props, { emit }) {
      const q = ref("");
      const sel = ref(props.modelValue);
      watch(() => props.modelValue, (v) => { sel.value = v; });
      watch(() => props.show, (v) => { if (v) { q.value = ""; sel.value = props.modelValue; } });
      const showSearch = computed(() => (props.searchable === undefined ? props.items.length > 8 : props.searchable));
      const filtered = computed(() => {
        const s = (q.value || "").trim().toLowerCase();
        if (!s) return props.items;
        return props.items.filter((it) => ((it.label || "") + " " + (it.sub || "") + " " + (it.value || "")).toLowerCase().includes(s));
      });
      const hasSel = computed(() => props.items.some((it) => it.value === sel.value && !it.disabled));
      function choose(it) { if (!it || it.disabled) return; haptic("light"); sel.value = it.value; }
      function confirm() { if (!hasSel.value) return; haptic("medium"); emit("confirm", sel.value); emit("update:show", false); }
      function close() { emit("update:show", false); emit("cancel"); }
      return { q, sel, showSearch, filtered, hasSel, choose, confirm, close };
    },
  };

  // ── VIEW: Home / Dashboard ──────────────────────────────
  const Home = {
    template: `
      <div class="pv-page">
        <div class="pv-header">
          <div>
            <div class="pv-brand">PS <span class="vibe">VIBE</span></div>
            <div class="pv-tagline">Play The Game. Share The VIBE!</div>
          </div>
        </div>
        <div class="pv-header psv-home-greet" style="padding-top:0;padding-bottom:8px">
          <div v-if="nativeMode" class="psv-home-id">
            <div class="psv-avatar psv-avatar--lg">{{ initials }}</div>
            <div>
              <div class="pv-hello">Welcome 👋</div>
              <div class="pv-staff-name">{{ staffName }}</div>
              <div v-if="webLogin" class="pv-account-role">{{ staffRole }}</div>
            </div>
          </div>
          <div v-else>
            <div class="pv-hello">Welcome 👋</div>
            <div class="pv-staff-name">{{ staffName }}</div>
            <div v-if="webLogin" class="pv-account-role">{{ staffRole }}</div>
          </div>
          <div style="display:flex;align-items:center;gap:14px">
            <div class="psv-bell" @click="goNotifications" aria-label="Notifications">
              <van-icon name="bell" size="22" />
              <span v-if="unread > 0" class="psv-bell__badge">{{ unread > 99 ? '99+' : unread }}</span>
            </div>
            <van-icon v-if="webLogin" name="revoke" size="20" @click="doLogout" style="color:var(--pv-danger);cursor:pointer" />
            <van-icon name="replay" size="22" @click="load" style="color:var(--pv-muted);cursor:pointer" />
          </div>
        </div>

        <van-skeleton title :row="3" v-if="loading" style="margin:12px 14px" />

        <template v-else>
          <div v-if="nativeMode" class="pv-stats pv-stats--premium">
            <div class="pv-stat pv-stat--accent"><div class="pv-stat__value">{{ fKs(summary.todayRevenue) }}</div><div class="pv-stat__label">Today's Sales (Ks)</div></div>
            <div class="pv-stat pv-stat--ok"><div class="pv-stat__value">{{ summary.activeConsoles }}</div><div class="pv-stat__label">Active Sessions</div></div>
            <div class="pv-stat"><div class="pv-stat__value">{{ summary.bookings }}</div><div class="pv-stat__label">Bookings</div></div>
            <div class="pv-stat pv-stat--warn"><div class="pv-stat__value">{{ summary.pendingDeposits }}</div><div class="pv-stat__label">Pending Deposits</div></div>
          </div>
          <div v-else class="pv-stats">
            <div class="pv-stat pv-stat--accent"><div class="pv-stat__value">{{ fKs(summary.todayRevenue) }}</div><div class="pv-stat__label">Today's Revenue (Ks)</div></div>
            <div class="pv-stat pv-stat--teal"><div class="pv-stat__value">{{ summary.activeConsoles }}</div><div class="pv-stat__label">Active Consoles</div></div>
            <div class="pv-stat"><div class="pv-stat__value">{{ summary.todaySales }}</div><div class="pv-stat__label">Today's Sales</div></div>
            <div class="pv-stat"><div class="pv-stat__value">{{ summary.totalMembers }}</div><div class="pv-stat__label">Total Members</div></div>
          </div>

          <div class="pv-section-title">Quick Actions</div>
          <div class="pv-actions" :class="{ 'pv-actions--premium': nativeMode }">
            <div class="pv-action" @click="go('/sales')"><div class="pv-action__icon">💰</div><div class="pv-action__label">New Sale</div></div>
            <div class="pv-action" @click="openTopupEntry"><div class="pv-action__icon">➕</div><div class="pv-action__label">Top Up</div></div>
            <div class="pv-action" @click="go('/bookings')"><div class="pv-action__icon">📅</div><div class="pv-action__label">New Booking</div></div>
            <div class="pv-action" @click="go('/consoles')"><div class="pv-action__icon">🕹️</div><div class="pv-action__label">Consoles</div></div>
            <div class="pv-action" @click="go('/inventory')"><div class="pv-action__icon">📦</div><div class="pv-action__label">Inventory</div></div>
            <div class="pv-action" @click="go('/reports')"><div class="pv-action__icon">📊</div><div class="pv-action__label">Today</div></div>
            <div class="pv-action" @click="go('/members')"><div class="pv-action__icon">🆕</div><div class="pv-action__label">New Member</div></div>
            <div class="pv-action" @click="go('/food')"><div class="pv-action__icon">🍜</div><div class="pv-action__label">Food</div></div>
          </div>
        </template>

        <van-popup v-model:show="topup.picker" position="bottom" round style="max-height:70vh">
          <van-search v-model="topup.q" placeholder="Search member" />
          <van-cell-group inset style="margin-top:8px">
            <van-cell v-for="m in topupFiltered" :key="m.id" :title="m.name || m.id" :label="'ID ' + (m.id||'-') + ' · ' + (m.phone||'-') + ' · ' + (m.wallet_balance ?? m.liability_mins ?? 0) + ' mins'" is-link @click="pickTopup(m)" />
          </van-cell-group>
        </van-popup>
        <van-dialog v-model:show="topup.dlg" :title="'Top Up — ' + (topup.member_name || topup.member_id)" show-cancel-button @confirm="confirmTopup">
          <div style="padding:16px">
            <van-field v-model.number="topup.amount" type="number" label="Amount (Ks)" placeholder="0" />
            <div style="margin-top:10px;padding:10px 12px;background:var(--pv-card);border-radius:8px;font-size:13px;line-height:1.7">
              <div>Base: <span class="pv-amount">{{ topupPreview.base }}</span> mins</div>
              <div>Bonus: <span class="pv-amount">+{{ topupPreview.bonus }}</span> mins <span style="color:var(--pv-muted)">({{ topupTier }})</span></div>
              <div style="font-weight:600;border-top:1px solid var(--pv-border, #eee);margin-top:4px;padding-top:4px">Total: <span class="pv-amount" style="font-size:15px">{{ topupPreview.total }}</span> mins</div>
            </div>
            <div style="margin-top:12px">
              <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0">
                <span style="font-weight:600">Payment</span>
                <span style="color:var(--pv-muted);font-size:13px">Paid: <span class="pv-amount">{{ fKs(topupPaid) }}</span> / {{ fKs(topup.amount) }} Ks</span>
              </div>
              <div v-if="!topup.splits.length" style="padding:4px 0;color:var(--pv-muted)">No split — full amount on Cash</div>
              <div v-for="(s, i) in topup.splits" :key="i" style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:var(--pv-card);border-radius:8px;margin-bottom:6px">
                <span style="flex:1">{{ s.method }}</span>
                <span class="pv-amount" style="margin:0 10px">{{ fKs(s.amount) }} Ks</span>
                <van-icon name="cross" style="color:var(--pv-danger);padding:4px" @click="topupRemoveSplit(i)" />
              </div>
              <van-button size="small" plain type="primary" style="width:100%;margin-top:4px" @click="topupOpenPay">+ Add payment</van-button>
            </div>
          </div>
        </van-dialog>
        <van-popup v-model:show="topup.payPicker" position="bottom" round>
          <van-picker :columns="topup.payColumns" @confirm="topupPickPay" @cancel="topup.payPicker=false" />
        </van-popup>
        <van-popup v-model:show="topup.payAmountPicker" position="bottom" round>
          <div style="padding:14px 16px">
            <div style="font-weight:600;margin-bottom:10px">Amount for {{ topup.pendingMethod }} <span style="color:var(--pv-muted);font-weight:400">(remaining {{ fKs(topupRemaining) }} Ks)</span></div>
            <van-field v-model.number="topup.payAmountInput" type="number" label="Amount (Ks)" placeholder="0" />
            <div style="display:flex;gap:8px;margin-top:12px">
              <van-button block plain @click="topup.payAmountPicker=false">Cancel</van-button>
              <van-button block type="primary" @click="topupConfirmSplit">Add</van-button>
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup() {
      const staffName = ref(PSV.getStaff().name || "");
      const webLogin = ref(PSV.usesWebLogin());
      const staffRole = ref(PSV.getStaff().role || "");
      function doLogout() { PSV.logout(); showToast("Logged out", "success"); }
      const loading = ref(true);
      const summary = reactive({ todayRevenue: 0, activeConsoles: 0, todaySales: 0, totalMembers: 0, bookings: 0, pendingDeposits: 0 });
      const nativeMode = NATIVE;
      const initials = computed(() => String(staffName.value || "?").trim().slice(0, 2).toUpperCase());
      const PAY_METHODS = ["Cash", "KPay", "WavePay", "AYA Pay"];
      const topup = reactive({ picker: false, dlg: false, q: "", members: [], member_id: "", member_name: "", member_tier: "", amount: 0, splits: [], payPicker: false, payAmountPicker: false, pendingMethod: "", payAmountInput: null, payColumns: PAY_METHODS.map((p) => ({ text: p, value: p })), base_rate: 10000, bonus_table: [], master_threshold: 300000, immortal_threshold: 1000000, member_total_spend: 0, busy: false });
      const topupTier = computed(() => topup.member_tier || "Warrior");
      const topupPaid = computed(() => topup.splits.reduce((s, x) => s + (Number(x.amount) || 0), 0));
      const topupRemaining = computed(() => Math.max(0, (Number(topup.amount) || 0) - topupPaid.value));
      const topupPreview = computed(() => {
        const amt = Number(topup.amount) || 0;
        const base = topup.base_rate > 0 ? Math.round((amt * 60) / topup.base_rate) : amt;
        const bonus = calcTopupBonus(topupTier.value, amt, topup.bonus_table);
        return { base: Math.round(base), bonus: bonus, total: Math.round(base) + bonus };
      });

      function calcTopupBonus(rank, amt, table) {
        if (amt <= 0) return 0;
        if (!Array.isArray(table) || !table.length) return 0;
        let chosen = null;
        for (const row of table) {
          const thr = Number(row.threshold ?? row[0] ?? 0) || 0;
          if (thr <= amt && (chosen === null || thr > Number(chosen.threshold ?? chosen[0] ?? 0))) chosen = row;
        }
        if (chosen === null) return 0;
        if (rank === "Immortal") return Number(chosen.immortal_bonus ?? chosen[3] ?? 0) || 0;
        if (rank === "Master") return Number(chosen.master_bonus ?? chosen[2] ?? 0) || 0;
        return Number(chosen.warrior_bonus ?? chosen[1] ?? 0) || 0;
      }

      function go(path) { haptic("light"); router.push(path); }
      // In app mode the Top Up tile opens the premium Deposit screen; Telegram keeps the dialog.
      function openTopupEntry() { if (NATIVE) { haptic("light"); router.push("/deposit"); } else { openTopup(); } }

      const topupFiltered = computed(() => {
        const q = topup.q.trim().toLowerCase();
        if (!q) return topup.members;
        return topup.members.filter((m) => (m.name && String(m.name).toLowerCase().includes(q)) || (m.phone && String(m.phone).includes(q)) || (m.id && String(m.id).includes(q)));
      });

      async function openTopup() {
        haptic("light");
        topup.q = ""; topup.picker = true;
        if (!topup.members.length) {
          try { const d = PSV.unwrap(await PSV.api("/api/fetch_members?limit=300")); topup.members = (d && d.members) || []; }
          catch (e) { showToast("Load failed: " + e.message, "fail"); topup.picker = false; }
        }
      }

      async function loadTopupConfigOnce() {
        if (topup._configLoaded) return;
        topup._configLoaded = true;
        const [base, bonus, thresh] = await Promise.allSettled([
          PSV.api("/api/fetch_base_rate"),
          PSV.api("/api/fetch_bonus_table"),
          PSV.api("/api/fetch_rank_thresholds"),
        ]);
        if (base.status === "fulfilled") { const v = Number(PSV.unwrap(base.value)); if (!isNaN(v) && v > 0) topup.base_rate = v; }
        if (bonus.status === "fulfilled") { const b = PSV.unwrap(bonus.value); if (Array.isArray(b) && b.length) topup.bonus_table = b; }
        if (thresh.status === "fulfilled") { const t = PSV.unwrap(thresh.value) || {}; if (t.master_threshold != null) topup.master_threshold = Number(t.master_threshold) || 300000; if (t.immortal_threshold != null) topup.immortal_threshold = Number(t.immortal_threshold) || 1000000; }
      }

      function pickTopup(m) {
        topup.picker = false;
        topup.member_id = m.id; topup.member_name = m.name || m.id;
        topup.member_tier = m.tier || "";
        topup.member_total_spend = Number(m.total_spend ?? 0) || 0;
        topup.amount = 0; topup.splits = [];
        topup.dlg = true;
        loadTopupConfigOnce();
        (async () => {
          try {
            const d = PSV.unwrap(await PSV.api("/api/fetch_member_data/" + encodeURIComponent(m.id)));
            if (d) {
              topup.member_total_spend = Number(d.net_spend ?? d.total_spend ?? 0) || 0;
              if (d.rank_raw) topup.member_tier = d.rank_raw;
            }
          } catch (_e) {
            // fallback: use fetch_members tier field
          }
        })();
      }

      async function confirmTopup() {
        const amt = Number(topup.amount) || 0;
        if (amt <= 0) { showToast("Enter amount", "fail"); return; }
        if (topup.busy) return;
        if (topup.splits.length && Number(topupPaid.value) < amt - 0.001) {
          showToast("Payments total " + Number(topupPaid.value) + " < Amount " + amt, "warning"); return;
        }
        const active = topup.splits.filter((s) => Number(s.amount) > 0);
        const pm = active.length ? active.map((s) => `${s.method}:${s.amount}`).join("|") : ("Cash:" + amt);
        topup.busy = true; haptic("medium");
        try {
          const res = PSV.unwrap(await PSV.api("/api/topup/log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member_id: topup.member_id, amount: amt, payment_method: pm, staff_name: PSV.getStaff().name, client_ref: PSV.genClientRef() }) }));
          if (res && res.duplicate) showToast("Already applied — " + (res.balance_mins ?? "?") + " mins", "warning");
          else {
            const base = res && res.base_mins != null ? res.base_mins : topupPreview.value.base;
            const bonus = res && res.bonus_mins != null ? res.bonus_mins : topupPreview.value.bonus;
            const total = (res && res.mins_added) || (res.balance_mins != null ? res.balance_mins : topupPreview.value.total);
            showToast("Top-up done ✓ " + total + " mins (base " + base + " + bonus " + bonus + ") · balance " + (res.balance_mins ?? "?"), "success");
          }
          topup.dlg = false; topup.amount = 0; topup.splits = [];
          load();
        } catch (e) { showToast("Error: " + e.message, "fail"); }
        finally { topup.busy = false; }
      }

      function topupOpenPay() { if (!topup.splits.length) topup.splits = []; topup.payPicker = true; haptic("light"); }
      function topupPickPay({ selectedOptions }) { topup.pendingMethod = selectedOptions[0].value; topup.payPicker = false; topup.payAmountInput = topupRemaining.value; topup.payAmountPicker = true; }
      function topupConfirmSplit() {
        const a = Number(topup.payAmountInput) || 0;
        if (a <= 0) { showToast("Enter amount", "warning"); return; }
        if (a > topupRemaining.value + 0.001) { showToast("Amount exceeds remaining " + topupRemaining.value, "warning"); return; }
        if (!topup.splits) topup.splits = [];
        topup.splits.push({ method: topup.pendingMethod, amount: a });
        topup.payAmountPicker = false;
      }
      function topupRemoveSplit(i) { topup.splits.splice(i, 1); }

      // WAVE 4 — notification poll hygiene: at most once per 60s, paused while
      // the app is backgrounded (visibilitychange), and served through the
      // shared 60s cache so a foreground/background flip never bursts.
      const unread = ref(0);
      const UNREAD_MIN_MS = 60000;
      let _lastUnread = 0;
      let _unreadTimer = null;
      async function loadUnread(force) {
        if (document.hidden && !force) return;
        const now = Date.now();
        if (!force && (now - _lastUnread) < UNREAD_MIN_MS) return;
        _lastUnread = now;
        try {
          const d = PSV.unwrap(await PSV.apiCached("/api/staff/notifications/unread-count", {}, 60000));
          unread.value = Number((d && (d.unread != null ? d.unread : d.count)) || 0) || 0;
        } catch (_e) { /* keep the last known value */ }
      }
      function _onVisibility() { if (!document.hidden) loadUnread(false); }
      function goNotifications() { haptic("light"); router.push("/notifications"); }

      async function load() {
        loading.value = true;
        // Home ≤ 4 requests per load: members + consoles + sales (+ native bookings),
        // all served from the 15s shared cache so quick returns are free.
        const [members, consoles, sales] = await Promise.allSettled([
          PSV.apiCached("/api/fetch_members?limit=1", {}, 15000),
          PSV.apiCached("/api/fetch_console_status", {}, 15000),
          PSV.apiCached("/api/sales/list-today", {}, 15000).catch(() => null),
        ]);
        if (members.status === "fulfilled") {
          const m = PSV.unwrap(members.value);
          summary.totalMembers = (m && (m.total ?? m.count ?? (Array.isArray(m.members) ? m.members.length : 0))) || 0;
        }
        if (consoles.status === "fulfilled") {
          const c = PSV.unwrap(consoles.value);
          const list = Array.isArray(c) ? c : (c && c.consoles) || [];
          summary.activeConsoles = list.filter((x) => String(x.status || "").toLowerCase() === "active").length;
        }
        if (sales.status === "fulfilled" && sales.value) {
          const s = PSV.unwrap(sales.value);
          if (Array.isArray(s)) {
            summary.todaySales = s.length;
            summary.todayRevenue = s.reduce((a, r) => a + (Number(r.net) || 0), 0);
          } else if (s) {
            summary.todaySales = s.total ?? s.count ?? 0;
            summary.todayRevenue = s.total_net ?? s.total_revenue ?? s.total ?? 0;
          }
        }
        if (NATIVE) {
          try {
            const bd = PSV.unwrap(await PSV.apiCached("/api/bookings?status=", {}, 15000));
            const arr = Array.isArray(bd) ? bd : (bd && bd.bookings) || [];
            const today = PSV.todayMMT();
            const _liveSt = ["pending", "confirmed", "checked_in", "active", "pending_check_in"];
            summary.bookings = arr.filter((b) => _liveSt.includes(String(b.status || "").toLowerCase()) && (!(b.date || b.booking_date) || String(b.date || b.booking_date).slice(0, 10) === today)).length;
            const _openSt = ["pending", "confirmed", "checked_in", "active"];
            summary.pendingDeposits = arr.filter((b) => _openSt.includes(String(b.status || "").toLowerCase()) && ["paid", "pending"].includes(String(b.deposit_status || "").toLowerCase())).length;
          } catch (_e) {}
        }
        loading.value = false;
      }
      onMounted(() => {
        load();
        loadUnread(true);
        _unreadTimer = setInterval(() => { loadUnread(false); }, UNREAD_MIN_MS);
        document.addEventListener("visibilitychange", _onVisibility);
        window.addEventListener("psv:retry", load);
      });
      onUnmounted(() => {
        if (_unreadTimer) clearInterval(_unreadTimer);
        document.removeEventListener("visibilitychange", _onVisibility);
        window.removeEventListener("psv:retry", load);
      });
      return { staffName, webLogin, staffRole, doLogout, loading, summary, nativeMode, initials, fKs, go, load, unread, goNotifications, topup, topupFiltered, topupTier, topupPaid, topupRemaining, topupPreview, calcTopupBonus, openTopup, openTopupEntry, pickTopup, confirmTopup, topupOpenPay, topupPickPay, topupConfirmSplit, topupRemoveSplit };
    },
  };

  // ── VIEW: Sales ─────────────────────────────────────────
  const Sales = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Sales" />
        <van-tabs v-model:active="tab" sticky>
          <van-tab title="New"><SaleForm @saved="onSaved" /></van-tab>
          <van-tab title="Today">
            <van-pull-refresh v-model="refreshing" @refresh="loadToday">
              <van-empty v-if="!today.length && !loadingToday" description="No sales today yet" />
              <van-cell-group v-else inset style="margin-top:8px">
                <van-cell v-for="s in today" :key="s.id || s.voucher_no" :title="s.voucher_no || s.id"
                  :label="labelFor(s)" is-link @click="openDetail(s)">
                  <template #value><span class="pv-amount">{{ fKs(s.net) }} Ks</span></template>
                </van-cell>
              </van-cell-group>
            </van-pull-refresh>
          </van-tab>
        </van-tabs>

        <!-- Sale detail bottom sheet -->
        <van-popup v-model:show="detailOpen" position="bottom" round closeable
          close-on-click-overlay :style="{ maxHeight: '82vh' }">
          <div style="padding:16px 16px 24px">
            <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px">
              <div class="pv-title">Sale Detail</div>
              <div class="muted">{{ detailSale.voucher_no || detailSale.id }}</div>
            </div>
            <van-cell-group inset v-if="detailSale">
              <van-cell title="Date / Time" :value="(detailSale.sale_date||'-') + ' ' + (detailSale.sale_time||'')" />
              <van-cell title="Console" :value="detailSale.console_id || '-'" />
              <van-cell title="Member" :value="memberLabel" :label="memberNameLabel" />
              <van-cell title="Staff" :value="detailSale.staff_name || '-'" />
            </van-cell-group>
            <van-cell-group inset style="margin-top:10px" title="Amounts">
              <van-cell title="Game" :value="fKs(detailSale.amount) + ' Ks'" />
              <van-cell v-if="foodTotal > 0" title="Food" :value="fKs(foodTotal) + ' Ks'" :label="foodLabel" />
              <van-cell v-if="Number(detailSale.discount) > 0" title="Discount" :value="'-' + fKs(detailSale.discount) + ' Ks'" />
              <van-cell title="Net Total" style="font-weight:700">
                <template #value><span class="pv-amount">{{ fKs(detailSale.net) }} Ks</span></template>
              </van-cell>
            </van-cell-group>
            <van-cell-group inset v-if="detailSale.payment_method" style="margin-top:10px" title="Payment">
              <van-cell title="Method" :value="detailSale.payment_method" />
            </van-cell-group>
            <van-cell-group inset v-if="detailSale.notes" style="margin-top:10px" title="Notes">
              <van-cell :value="detailSale.notes" />
            </van-cell-group>
            <div style="margin-top:16px">
              <van-button block type="primary" :loading="printingDetail" @click="doPrintDetail">Print voucher</van-button>
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup() {
      const tab = ref(0);
      const today = ref([]);
      const loadingToday = ref(true);
      const refreshing = ref(false);
      const detailSale = ref(null);
      const detailOpen = ref(false);

      function openDetail(s) {
        detailSale.value = s;
        detailOpen.value = true;
        haptic("light");
      }
      function closeDetail() { detailOpen.value = false; }

      const printingDetail = ref(false);
      async function doPrintDetail() {
        const s = detailSale.value; if (!s) return;
        const items = [];
        if (Number(s.amount)) items.push({ name: "🎮 Game time", qty: 1, price: Number(s.amount) });
        const ft = Number(s.gross) - Number(s.amount);
        if (ft > 0) {
          // 2026-09-20 (owner: "Voucher မှာ food detail မပါဘူး"): itemise the food instead of
          // one lump line. The sale notes carry "Food: item x1, item x2" (the API now writes
          // that from food_cart for session food), so split it into individual lines.
          let fi = s.notes || ""; const mm = /Food:\s*([^|]+)/.exec(fi); if (mm) fi = mm[1].trim();
          // Owner 2026-09-20 11:56: "cost per item တွေလည်း မပြထားဘူး". The notes carry
          // "name xQty @unit = subtotal" (the API writes it), so print each line WITH its cost.
          const parts = String(fi || "").split(/\s*,\s*/).map((x) => x.trim()).filter(Boolean);
          const parsed = parts.map((pp) => {
            const m = /^(.*?)\s+x\s*(\d+)\s*(?:@\s*([\d,]+))?/.exec(pp);
            if (!m) return { name: pp, qty: 0, unit: 0 };
            return { name: String(m[1] || "").trim(), qty: Number(m[2]) || 0,
                     unit: Number(String(m[3] || "0").replace(/,/g, "")) || 0 };
          });
          const priced = parsed.some((x) => x.unit > 0);
          if (parsed.length > 1 || priced) {
            items.push({ name: "🍜 Food:", plain: true });
            for (const x of parsed) {
              if (x.unit > 0) items.push({ name: "   " + x.name, qty: x.qty || 1, price: x.unit });
              else items.push({ name: "   " + (x.name || "Food") + (x.qty ? " x" + x.qty : ""), plain: true });
            }
            items.push({ name: "   Food total", qty: 1, price: ft });
          } else {
            items.push({ name: "🍜 " + (parts[0] || "Food"), qty: 1, price: ft });
          }
        }
        printingDetail.value = true;
        try {
          await PSV.printVoucher({
            voucher_no: s.voucher_no || s.id,
            date: s.sale_date || "",
            time: s.sale_time || "",
            staff: s.staff_name || "",
            member: memberLabel.value,
            items: items,
            total: Number(s.net) || 0,
            balance: (function () { const w = /Wallet:\s*(-\d+\s*mins[^|]*)/.exec(s.notes || ""); return w ? w[1].trim() : ""; })(),
            payment: s.payment_method || "",
            notes: Number(s.discount) > 0 ? ("Discount " + fKs(s.discount) + " Ks") : "",
          });
        } finally { printingDetail.value = false; }
      }

      const memberLabel = computed(() => {
        const s = detailSale.value; if (!s) return '-';
        const m = s.member_id || '-';
        if (m === '0 (Guest)' || m === '0' || m === '-' || m === 'Guest') return 'Guest';
        return m;
      });
      const memberNameLabel = computed(() => {
        const s = detailSale.value; if (!s) return '';
        const name = s.customer_name || '';
        if (!name) return '';
        const m = s.member_id || '-';
        const isGuest = (m === '0 (Guest)' || m === '0' || m === '-' || m === 'Guest');
        // Guest rows → show the guest name.
        if (isGuest) return 'Name: ' + name;
        // Member rows → only show name if it's a real member name (not a leaked staff name).
        if (name !== s.staff_name) return 'Name: ' + name;
        return '';
      });
      const foodTotal = computed(() => {
        const s = detailSale.value; if (!s) return 0;
        return Number(s.gross) - Number(s.amount);
      });
      const foodLabel = computed(() => {
        const s = detailSale.value; if (!s) return '';
        let items = s.notes || '';
        const m = /Food:\s*([^|]+)/.exec(items);
        if (m) items = m[1].trim();
        return items;
      });

      async function loadToday(endpoint) {
        const todayDate = PSV.todayMMT();
        const candidates = [
          endpoint || ("/api/sales/list-today?date=" + todayDate),
          "/api/dashboard/sales-daily?date=" + todayDate,
        ];
        for (const url of candidates) {
          try {
            const r = await PSV.api(url);
            const d = PSV.unwrap(r);
            if (Array.isArray(d)) { today.value = d; break; }
            if (d && Array.isArray(d.records)) { today.value = d.records; break; }
            if (d && Array.isArray(d.sales)) { today.value = d.sales; break; }
          } catch (e) { /* try next */ }
        }
        loadingToday.value = false;
        refreshing.value = false;
      }
      onMounted(loadToday);
      function onSaved() { tab.value = 1; loadToday(); }

      // Build the van-cell label: member · time, plus food / discount detail lines.
      function labelFor(s) {
        const lines = [(s.member_id || '-') + ' · ' + (s.sale_time || '')];
        const foodTotal = Number(s.gross) - Number(s.amount);
        if (foodTotal > 0) {
          let foodItems = s.notes || '';
          const m = /Food:\s*([^|]+)/.exec(foodItems);
          if (m) foodItems = m[1].trim();
          lines.push("🍜 Food: " + foodItems + " (" + fKs(foodTotal) + " Ks)");
        }
        const wm = /Wallet:\s*(-\d+\s*mins[^|]*)/.exec(s.notes || "");
        if (wm) lines.push("💳 " + wm[1].trim());
        if (Number(s.discount) > 0) lines.push("Discount: " + fKs(s.discount) + " Ks");
        return lines.join('\n');
      }
      return { tab, today, loadingToday, refreshing, fKs, loadToday, onSaved, labelFor,
               detailSale, detailOpen, openDetail, closeDetail, memberLabel, memberNameLabel, foodTotal, foodLabel, printingDetail, doPrintDetail };
    },
  };

  // SaleForm component
  const SaleForm = {
    emits: ["saved"],
    template: `
      <div style="padding:8px 14px 16px">
        <div v-if="sessionWarn" class="psv-sess-warn">⚠️ {{ sessionWarn }} — please double-check</div>
        <div v-if="depositRedeem" class="psv-redeem-banner" :class="{ 'is-applied': depositApplied }">
          <span class="psv-redeem-banner__txt"><template v-if="depositApplied">✅ Deposit {{ fKs(depositRedeem.amount) }} Ks (BK#{{ depositRedeem.booking_id }}) — deducted</template><template v-else>↩️ Verified deposit {{ fKs(depositRedeem.amount) }} Ks (BK#{{ depositRedeem.booking_id }}) available</template></span>
          <button v-if="!depositApplied" class="psv-abtn psv-abtn--gold" style="min-height:36px;padding:0 10px" @click="applyDeposit">💰 Deduct {{ fKs(depositRedeem.amount) }} deposit</button>
          <button class="psv-redeem-banner__x" @click="clearRedeem">✕</button>
        </div>
        <van-cell-group inset>
          <van-field v-model="form.voucher_no" label="Voucher" readonly />
          <van-field v-model="form.member_search" label="Member" placeholder="Search / Guest" is-link readonly @click="memberPicker=true" />
          <van-field v-model="form.console_id" label="Console" placeholder="Select console" is-link readonly @click="openConsole" />
          <van-field v-model="form.game_name" label="Game" placeholder="Select game" is-link readonly @click="gamePicker=true" />
          <van-field v-model.number="form.mins" type="number" label="Minutes" :readonly="sessionLocked" placeholder="0" />
          <div v-if="sessionLocked" style="margin:0 16px 4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="color:var(--pv-muted);font-size:11px">Adjust (−10..+10):</span>
            <van-stepper :model-value="Number(form.adjust)||0" min="-10" max="10" integer @change="(v)=>form.adjust=Math.max(-10,Math.min(10,Number(v)||0))" />
            <span style="color:var(--pv-muted);font-size:11px">→ <b>{{ effectiveMins }}m</b></span>
          </div>
          <van-field :model-value="fKs(form.base_rate) + ' Ks/hr'" label="Base Rate" readonly />
          <van-field :model-value="(Number(form.multiplier)||1) + 'x'" label="Multiplier" readonly />
          <van-field v-model.number="form.game_amt" type="number" label="Game (Ks)" placeholder="0" />
          <div style="margin:0 16px 4px;color:var(--pv-muted);font-size:11px">Auto: {{ fKs(autoGameAmt) }} Ks (mins × rate × mult ÷ 60)</div>
          <van-field v-model.number="form.discount" type="number" label="Discount" placeholder="0" />
        </van-cell-group>

        <!-- 🏷️ Promotion + 🎟️ Coupon (WAVE 2 — discounting parity with the sale bot).
             MONEY RULE: the discounted total is ALWAYS computed by the server
             (POST /api/promotions/calc); the client only displays what it returns. -->
        <div v-if="promos.length || promo.id || coupon.applied || coupon.code" style="margin:10px 16px 0">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 10px">
            <span style="font-weight:600">🏷️ Promotion / 🎟️ Coupon</span>
            <span v-if="calcStatus === 'ok' && totalDiscountKs > 0" class="psv-verified">🛡 server verified</span>
            <span v-else-if="calcStatus === 'loading'" style="color:var(--pv-muted);font-size:11px">Calculating…</span>
          </div>
          <div class="psv-card psv-disc-card">
            <div v-if="promo.id" class="psv-pick-row">
              <div class="psv-pick-row__main">
                <div class="psv-pick-row__t">🎁 {{ promo.title }}</div>
                <div class="psv-pick-row__s">{{ promoText(promo) }}</div>
              </div>
              <button class="psv-chip" @click="clearPromo">Remove</button>
            </div>
            <button v-else-if="promos.length" class="psv-btn psv-btn--ghost psv-btn--block" style="min-height:44px" @click="openPromo">🎁 Choose Promotion</button>

            <div style="display:flex;gap:8px;margin-top:10px">
              <input v-model.trim="coupon.code" class="psv-input" style="flex:1" :disabled="coupon.applied" placeholder="Coupon code" @keyup.enter="applyCoupon" />
              <button class="psv-chip" :disabled="coupon.applied || coupon.checking || !coupon.code" @click="applyCoupon">{{ coupon.applied ? '✓ applied' : (coupon.checking ? '…' : 'Check') }}</button>
            </div>
            <div v-if="coupon.message" class="psv-coupon-msg" :class="coupon.applied ? 'is-ok' : 'is-fail'">{{ coupon.message }}</div>
            <div v-if="focItem" style="margin-top:8px;font-size:12px;color:var(--pv-muted)">🍜 FOC: <b>{{ focItem }}</b> <button class="psv-mini-x" @click="focItem=''; scheduleCalc()">✕</button></div>
          </div>
        </div>

        <!-- Food line items (session food orders / picked items) -->
        <div style="margin:10px 16px 0">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 10px">
            <span style="font-weight:600">🍽 Food Orders</span>
            <span style="color:var(--pv-muted);font-size:12px">Total: <span class="pv-amount">{{ fKs(form.food_total) }} Ks</span></span>
          </div>
          <div v-if="foodLines.length" style="margin-top:4px">
            <div v-for="(ln, i) in foodLines" :key="i" style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:var(--pv-card);border-radius:8px;margin-bottom:6px">
              <span style="flex:1">{{ ln.name }}</span>
              <span class="pv-amount" style="margin:0 8px">{{ fKs(ln.qty * ln.price) }} Ks</span>
              <van-stepper :model-value="ln.qty" min="0" integer @change="(v)=>setFoodQty(ln, v)" />
              <van-icon name="cross" style="color:var(--pv-danger);padding:4px;margin-left:4px" @click="removeFood(i)" />
            </div>
          </div>
          <van-empty v-else image-size="48" description="No food items yet — tap below to add" style="padding:8px 0" />
          <van-button size="small" plain type="primary" icon="cart-o" style="width:100%;margin-top:4px" @click="openFoodPicker">+ Add Food</van-button>
        </div>

        <!-- Sub-total breakdown (server-authoritative discount lines) -->
        <van-cell-group inset style="margin-top:10px" title="Amounts">
          <van-cell title="Game" :value="fKs(form.game_amt) + ' Ks'" />
          <van-cell title="Food" :value="fKs(form.food_total) + ' Ks'" />
          <van-cell title="Subtotal" :value="fKs(subtotalKs) + ' Ks'" />
          <van-cell v-for="(b, i) in discountLines" :key="'d' + i" :title="b.label">
            <template #value><span style="color:var(--pv-success)">{{ b.amount < 0 ? '-' + fKs(-b.amount) : fKs(b.amount) }}</span></template>
          </van-cell>
          <van-cell v-if="!discountLines.length && Number(form.discount) > 0" title="Discount" :value="'-' + fKs(form.discount) + ' Ks'" />
          <van-cell title="Total Discount" :value="'-' + fKs(totalDiscountKs) + ' Ks'" />
          <van-cell title="Total" style="font-weight:700">
            <template #value><span class="pv-amount" style="font-size:16px">{{ fKs(payableBeforeDeposit) }} Ks</span></template>
          </van-cell>
          <van-cell v-if="depositApplied" title="Less Deposit">
            <template #value><span style="color:var(--pv-success);font-weight:600">-{{ fKs(depositRedeem.amount) }} Ks</span></template>
          </van-cell>
          <van-cell title="Cash due" style="font-weight:800">
            <template #value><span class="pv-amount" style="font-size:17px">{{ fKs(cashNetTotal) }} Ks</span></template>
          </van-cell>
        </van-cell-group>

        <!-- 🪙 Member Wallet Settle -->
        <van-cell-group v-if="isMember" inset style="margin-top:10px">
          <van-cell title="Wallet Balance" :value="wallet.balance + ' mins'" />
          <van-cell title="Game Cost" :value="effectiveCostMins + ' wallet-mins'" />
          <van-cell v-if="walletSufficient" title="Settle" style="color:var(--pv-success)">
            <template #value><span style="color:var(--pv-success);font-weight:600">Wallet covers game · cash = food only</span></template>
          </van-cell>
          <van-cell v-else-if="wallet.shortfall_mins > 0" title="Shortfall (cash-down)">
            <template #value><span style="color:var(--pv-danger);font-weight:600">{{ wallet.shortfall_mins }} mins ≈ {{ fKs(wallet.shortfall_ks) }} Ks</span></template>
          </van-cell>
          <van-cell v-else title="Cash Payable">
            <template #value><span class="pv-amount">{{ fKs(cashNetTotal) }} Ks</span></template>
          </van-cell>
          <div style="padding:8px 16px 12px;color:var(--pv-muted);font-size:11px">
            {{ walletLoading ? 'Fetching balance…' : (walletSufficient ? 'Wallet −' + effectiveCostMins + ' mins on save · no cash for game time' : 'Wallet −' + wallet.balance + ' mins · pay ' + fKs(wallet.shortfall_ks || 0) + ' Ks cash') }}
          </div>
        </van-cell-group>

        <!-- Split Payment -->
        <div style="margin:10px 16px 0">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px">
            <span style="font-weight:600">Payment</span>
            <span style="color:var(--pv-muted);font-size:13px">Paid: <span class="pv-amount">{{ fKs(paidTotal) }}</span> / Net: {{ fKs(cashNetTotal) }} Ks</span>
          </div>
          <div v-if="!form.payment_splits.length" style="padding:6px 10px;color:var(--pv-muted)">No split — full amount on Cash</div>
          <div v-for="(s, i) in form.payment_splits" :key="i" style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:var(--pv-card);border-radius:8px;margin-bottom:6px">
            <span style="flex:1">{{ s.method }}</span>
            <span class="pv-amount" style="margin:0 10px">{{ fKs(s.amount) }} Ks</span>
            <van-icon name="cross" style="color:var(--pv-danger);padding:4px" @click="removeSplit(i)" />
          </div>
          <van-button size="small" plain type="primary" style="width:100%;margin-top:4px" @click="openPayMethod">+ Add payment</van-button>
        </div>
        <div style="padding:14px 16px">
        <div style="padding:14px 16px">
          <van-button type="primary" block :loading="submitting" @click="submit">Save Sale</van-button>
        </div>

        <!-- Member Picker -->
        <van-popup v-model:show="memberPicker" position="bottom" round>
          <div style="padding:12px 16px 0"><van-search v-model="memberQuery" placeholder="Search name/phone" /></div>
          <div style="max-height:60vh;overflow:auto;padding-bottom:20px">
            <van-cell v-for="m in filteredMembers" :key="m.id" :title="m.name || m.id" :label="(m.phone||'-') + ' · ' + (m.wallet_balance ?? m.liability_mins ?? 0) + ' mins'" @click="pickMember(m)" />
            <van-cell title="🚶 Guest" @click="pickGuest" />
          </div>
        </van-popup>

        <!-- Console Picker -->
        <van-popup v-model:show="consolePicker" position="bottom" round>
          <div style="max-height:60vh;overflow:auto;padding:12px 0 20px">
            <van-cell v-for="c in consoles" :key="c.console_id || c.id" :title="c.console_id || c.id" :label="'Status: ' + (c.status||'-')" @click="pickConsole(c)" />
          </div>
        </van-popup>

        <!-- Game Picker -->
        <van-popup v-model:show="gamePicker" position="bottom" round>
          <van-picker :columns="gameColumns" @confirm="pickGame" @cancel="gamePicker=false" />
        </van-popup>

        <!-- Food Picker (grouped category -> items -> stepper) -->
        <van-popup v-model:show="foodPicker" position="bottom" round style="max-height:80vh">
          <div style="max-height:80vh;overflow:auto;padding:12px 0 20px">
            <van-cell-group v-for="(grp, gi) in foodGroups" :key="gi" inset :title="grp.cat" style="margin-top:8px">
              <van-cell v-for="it in grp.items" :key="it.name" center>
                <template #title>
                  <span>{{ it.name }}</span>
                  <span style="color:var(--pv-muted);margin-left:8px">{{ fKs(it.price) }} Ks</span>
                </template>
                <template #right-icon>
                  <van-stepper :model-value="qtyOf(it.name)" min="0" integer @change="(v)=>setFoodQty(it, v)" />
                </template>
              </van-cell>
            </van-cell-group>
            <van-empty v-if="!foodGroups.length" description="No food items" />
            <div style="padding:14px 16px">
              <van-button type="primary" block @click="foodPicker=false">Done</van-button>
            </div>
          </div>
        </van-popup>

        <!-- Payment Method Picker -->
        <van-popup v-model:show="payPicker" position="bottom" round>
          <van-picker :columns="payColumns" @confirm="pickPayMethod" @cancel="payPicker=false" />
        </van-popup>

        <!-- Split Amount Input -->
        <van-popup v-model:show="payAmountPicker" position="bottom" round>
          <div style="padding:14px 16px">
            <div style="font-weight:600;margin-bottom:10px">Amount for {{ pendingMethod }} <span style="color:var(--pv-muted);font-weight:400">(remaining {{ fKs(splitRemaining) }} Ks)</span></div>
            <van-field v-model.number="payAmountInput" type="number" label="Amount (Ks)" placeholder="0" />
            <div style="display:flex;gap:8px;margin-top:12px">
              <van-button block plain @click="payAmountPicker=false">Cancel</van-button>
              <van-button block type="primary" @click="confirmSplit">Add</van-button>
            </div>
          </div>
        </van-popup>

        <!-- 🧾 Sale complete — voucher preview + 80mm print -->
        <van-popup v-model:show="voucherOpen" position="bottom" round closeable :style="{ maxHeight: '86vh' }">
          <div v-if="lastVoucher" style="padding:18px 16px 24px">
            <div style="text-align:center;margin-bottom:8px">
              <div style="font-size:20px;font-weight:800">PS VIBE</div>
              <div style="font-size:11px;color:var(--pv-muted)">Sale Complete ✓</div>
            </div>
            <van-cell-group inset>
              <van-cell title="Voucher" :value="lastVoucher.voucher_no" />
              <van-cell title="Date / Time" :value="lastVoucher.date + ' ' + lastVoucher.time" />
              <van-cell title="Staff" :value="lastVoucher.staff" />
              <van-cell title="Member" :value="lastVoucher.member" />
            </van-cell-group>
            <van-cell-group inset style="margin-top:10px" title="Items">
              <van-cell v-for="(it, i) in lastVoucher.items" :key="i" :title="it.name">
                <template #value><span class="pv-amount">{{ it.qty }} × {{ fKs(it.price) }} = {{ fKs(it.qty * it.price) }}</span></template>
              </van-cell>
              <van-empty v-if="!lastVoucher.items.length" image-size="40" description="No items" style="padding:6px 0" />
            </van-cell-group>
            <van-cell-group inset style="margin-top:10px">
              <van-cell v-if="lastVoucher.subtotal != null" title="Subtotal" :value="fKs(lastVoucher.subtotal) + ' Ks'" />
              <van-cell v-if="Number(lastVoucher.discount) > 0" title="Discount">
                <template #value><span style="color:var(--pv-success)">-{{ fKs(lastVoucher.discount) }} Ks</span></template>
              </van-cell>
              <van-cell title="Total" style="font-weight:700">
                <template #value><span class="pv-amount" style="font-size:16px">{{ fKs(lastVoucher.total) }} Ks</span></template>
              </van-cell>
              <van-cell v-if="Number(lastVoucher.deposit) > 0" title="Less Deposit">
                <template #value><span style="color:var(--pv-success)">-{{ fKs(lastVoucher.deposit) }} Ks</span></template>
              </van-cell>
              <van-cell v-if="Number(lastVoucher.cashDue) != null && Number(lastVoucher.cashDue) !== Number(lastVoucher.total)" title="Cash due" style="font-weight:800">
                <template #value><span class="pv-amount">{{ fKs(lastVoucher.cashDue) }} Ks</span></template>
              </van-cell>
              <van-cell v-if="lastVoucher.payment" title="Payment" :value="lastVoucher.payment" />
              <van-cell v-if="lastVoucher.balance" title="Wallet" :value="lastVoucher.balance" />
            </van-cell-group>
            <div style="display:flex;gap:10px;margin-top:16px">
              <van-button block plain @click="voucherOpen=false">Close</van-button>
              <van-button block type="primary" :loading="printing" @click="doPrintVoucher">Print voucher</van-button>
            </div>
            <div style="text-align:center;color:var(--pv-muted);font-size:11px;margin-top:10px">80mm thermal via Bluetooth — or share / download the PNG</div>
          </div>
        </van-popup>

        <!-- 🎁 Promotion picker (active promos) -->
        <van-popup v-model:show="promoOpen" position="bottom" round style="max-height:72vh">
          <div style="max-height:72vh;overflow:auto;padding:12px 0 20px">
            <div class="psv-sheet-title" style="padding:0 16px">🎁 Choose Promotion</div>
            <div class="psv-bk-hint" style="padding:4px 16px 8px">The server calculates the discount once you pick a promotion</div>
            <van-empty v-if="!promos.length" description="No active promotions" />
            <div v-for="p in promos" :key="p.id" class="psv-list-row" style="padding:12px 16px" @click="pickPromo(p)">
              <div class="psv-list-row__main">
                <div class="psv-list-row__title">{{ p.title }}</div>
                <div class="psv-list-row__sub">{{ promoText(p) }}</div>
              </div>
              <van-icon name="arrow" />
            </div>
          </div>
        </van-popup>

        <!-- 🍜 FOC item picker (bundle / free_item promo) -->
        <van-popup v-model:show="focOpen" position="bottom" round>
          <div style="padding:12px 0 20px">
            <div class="psv-sheet-title" style="padding:0 16px">🍜 Choose FOC (free) item</div>
            <van-empty v-if="!foodLines.length" description="No food items" />
            <div v-for="(ln, i) in foodLines" :key="'foc' + i" class="psv-list-row" style="padding:12px 16px" @click="pickFoc(ln)">
              <div class="psv-list-row__main"><div class="psv-list-row__title">{{ ln.name }}</div><div class="psv-list-row__sub">{{ fKs(ln.price) }} Ks × {{ ln.qty }}</div></div>
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup(_, { emit }) {
      const PAY_METHODS = ["Cash", "KPay", "WavePay", "AYA Pay"];
      const form = reactive({
        voucher_no: PSV.genVoucherNo(), member_search: "", member_id: "",
        console_id: "", game_name: "", mins: 0, base_rate: 0, multiplier: 1.0,
        game_amt: 0, food_items: "", food_total: 0, discount: 0, payment_method: "Cash",
        payment_splits: [],
        adjust: 0,  // session voucher: bounded −10..+10 adjustment on actual mins
      });
      // ↩️ Deposit redeem (WAVE 1 → completed in WAVE 2): a booking flagged from the
      // Booking detail view. WAVE 2 adds the explicit "Deduct deposit"
      // confirm + a lookup for verified deposits on the picked member/console.
      const depositRedeem = ref(PSV.pendingDepositRedeem || null);
      const depositApplied = ref(false);
      function clearRedeem() { try { PSV.pendingDepositRedeem = null; } catch (_e) {} depositRedeem.value = null; depositApplied.value = false; }
      const depositAppliedAmt = computed(() => (depositApplied.value && depositRedeem.value) ? (Number(depositRedeem.value.amount) || 0) : 0);
      async function applyDeposit() {
        if (!depositRedeem.value) return;
        const amt = Number(depositRedeem.value.amount) || 0;
        const okc = await confirmBox("Deduct deposit?", "Deduct the verified deposit of " + fKs(amt) + " Ks (BK#" + depositRedeem.value.booking_id + ") from this sale payable total.", "Deduct", "Cancel");
        if (!okc) return;
        depositApplied.value = true; haptic("medium");
        showToast("Deposit " + fKs(amt) + " Ks deducted", "success");
      }

      // ── 🏷️ WAVE 2: Promotion / Coupon / server-side discount calc ─────────────
      // MONEY RULE: never compute the discounted total on the client. We post the
      // cart to POST /api/promotions/calc and display the returned subtotal /
      // breakdown / net_total — the server is the single source of truth.
      const promos = ref([]);
      const promoOpen = ref(false);
      const focOpen = ref(false);
      const focItem = ref("");
      const promo = reactive({ id: "", title: "", type: "", target: "overall", pct: 0, notes: "" });
      const coupon = reactive({ code: "", applied: false, checking: false, minutes: 0, discount: 0, message: "" });
      const serverCalc = ref(null);
      const calcStatus = ref("idle");        // idle | loading | ok | error
      let _calcTimer = null;

      function promoText(p) {
        if (!p) return "";
        const t = String(p.type || "").toLowerCase();
        const pct = Number(p.discount_percent) || 0;
        const tgt = String(p.discount_target || "overall");
        const tgtEn = tgt === "game_only" ? "game time" : tgt === "food_only" ? "food" : "total";
        if (t === "bonus_mins") return "+" + (pct || "") + "% bonus playtime";
        if (t === "bundle" || t === "free_item") return "Bundle / FOC — one food item free" + (p.notes ? " · " + p.notes : "");
        if (pct) return tgtEn + " " + pct + "% off" + (p.notes ? " · " + p.notes : "");
        return p.notes || "Promotion";
      }
      function loadPromos() {
        PSV.api("/api/promotions/active-list").then((r) => {
          if (r && r.success === false) { promos.value = []; return; }
          const d = PSV.unwrap(r); promos.value = (d && d.promotions) || [];
        }).catch(() => {});
      }
      function openPromo() { haptic("light"); if (!promos.value.length) loadPromos(); promoOpen.value = true; }
      function pickPromo(p) {
        promoOpen.value = false; focItem.value = "";
        promo.id = p.id; promo.title = p.title || ("Promo #" + p.id);
        promo.type = String(p.type || "").toLowerCase(); promo.target = p.discount_target || "overall";
        promo.pct = Number(p.discount_percent) || 0; promo.notes = p.notes || "";
        if (promo.type === "bundle" || promo.type === "free_item") focOpen.value = true;
        scheduleCalc();
      }
      function clearPromo() { promo.id = ""; promo.title = ""; promo.type = ""; promo.notes = ""; focItem.value = ""; scheduleCalc(); }
      function pickFoc(ln) { focItem.value = ln.name; focOpen.value = false; scheduleCalc(); }

      async function applyCoupon() {
        const code = String(coupon.code || "").trim().toUpperCase();
        if (!code || coupon.applied) return;
        coupon.checking = true; coupon.message = "";
        try {
          const r = await PSV.api("/api/coupons/validate", { method: "POST", headers: HJSON, body: JSON.stringify({ code: code }) });
          if (r && r.success === false) throw new Error(r.error || "Coupon is not valid");
          const d = PSV.unwrap(r); const c = (d && d.coupon) || {};
          if (!c.code) throw new Error("Coupon not found");
          coupon.code = c.code;
          coupon.minutes = Number(c.balance_minutes) || 0;
          coupon.discount = Math.round(coupon.minutes * (Number(form.base_rate) || 0) / 60);
          coupon.applied = true;
          coupon.message = "✓ Valid — " + coupon.minutes + " mins (~" + fKs(coupon.discount) + " Ks). Redeemed when you save.";
          haptic("light"); scheduleCalc();
        } catch (e) {
          coupon.applied = false; coupon.minutes = 0; coupon.discount = 0;
          coupon.message = "✗ " + (e && e.message ? e.message : "Coupon is not valid");
          haptic("medium");
        } finally { coupon.checking = false; }
      }
      function clearCoupon() { coupon.code = ""; coupon.applied = false; coupon.minutes = 0; coupon.discount = 0; coupon.message = ""; scheduleCalc(); }

      // Cart exactly as the server expects — mirrors the sale-bot promo engine.
      function cartForCalc() {
        const ecost = effectiveCostMins.value;
        const base = Number(form.base_rate) || 0;
        const foodTotal = Number(form.food_total) || 0;
        const gameAmt = Number(form.game_amt) || 0;
        const body = {
          food_total: foodTotal, base_rate: base, multiplier: Number(form.multiplier) || 1,
          manual_discount: Number(form.discount) || 0,
          promo_id: promo.id || "",
          coupon_code: coupon.applied ? coupon.code : "",
          foc_item: focItem.value || "",
          food_items: foodLines.value.map((l) => ({ name: l.name, qty: Number(l.qty) || 0, subtotal: (Number(l.qty) || 0) * (Number(l.price) || 0) })),
          is_member: false, game_amt: gameAmt, gross_total: gameAmt + foodTotal,
        };
        const walletCovers = isMember.value && wallet.balance > 0 && ecost > 0 && wallet.balance >= ecost;
        const shortfall = isMember.value && wallet.balance > 0 && ecost > 0 && wallet.balance < ecost;
        if (walletCovers) {
          body.is_member = true; body.wallet_mins = wallet.balance; body.effective_cost_mins = ecost;
          body.game_amt = 0; body.gross_total = foodTotal;
        } else if (shortfall) {
          body.game_amt = Math.round((ecost - wallet.balance) * base / 60);
          body.gross_total = body.game_amt + foodTotal;
        }
        return body;
      }
      function scheduleCalc() { if (_calcTimer) clearTimeout(_calcTimer); _calcTimer = setTimeout(runCalc, 400); }
      async function runCalc() {
        calcStatus.value = "loading";
        try {
          const r = await PSV.api("/api/promotions/calc", { method: "POST", headers: HJSON, body: JSON.stringify(cartForCalc()) });
          if (r && r.success === false) { serverCalc.value = null; calcStatus.value = "error"; return; }
          const d = PSV.unwrap(r);
          serverCalc.value = d || null;
          calcStatus.value = d ? "ok" : "error";
        } catch (e) { serverCalc.value = null; calcStatus.value = "error"; }
      }

      // ── In-session guard (staff mistake prevention) ─────────────────────
      const activeBookings = ref([]);
      async function loadActiveBookings() {
        try { const d = PSV.unwrap(await PSV.api("/api/bookings?status=Active")); activeBookings.value = (d && d.bookings) || []; }
        catch (_e) { activeBookings.value = []; }
      }
      const sessionWarn = computed(() => {
        const cid = String(form.console_id || "").trim().toLowerCase();
        const mid = String(form.member_id || "").trim().toLowerCase();
        if ((!cid) && (!mid || mid === "0 (guest)")) return "";
        const hit = activeBookings.value.find((b) => (cid && String(b.console_id || "").trim().toLowerCase() === cid) || (mid && mid !== "0 (guest)" && String(b.member_id || "").trim().toLowerCase() === mid));
        if (!hit) return "";
        return "This " + (cid && String(hit.console_id || "").trim().toLowerCase() === cid ? "console" : "member") + " already has session BK#" + hit.id + " running (" + (hit.status || "Active") + ")";
      });
      async function ensureNoActiveSession() {
        try {
          const d = PSV.unwrap(await PSV.api("/api/bookings?status=Active"));
          const list = (d && d.bookings) || [];
          const cid = String(form.console_id || "").trim().toLowerCase();
          const mid = String(form.member_id || "").trim().toLowerCase();
          const hitConsole = cid && list.find((b) => String(b.console_id || "").trim().toLowerCase() === cid);
          const hitMember = (mid && mid !== "0 (guest)") && list.find((b) => String(b.member_id || "").trim().toLowerCase() === mid);
          if (!hitConsole && !hitMember) return "ok";
          const bk = hitConsole || hitMember;
          const who = hitConsole ? ("Console " + form.console_id) : ("Member " + form.member_id);
          const okc = await confirmBox("⚠️ Session already active", who + " already has session BK#" + bk.id + " running — continue anyway? Charging again may double-charge the customer.", "Continue", "Cancel", false);
          return okc ? "ok" : "cancel";
        } catch (_e) { return "ok"; } // never hard-block
      }

      // 🪙 Member wallet settle (mirror sale bot: wallet covers game cost → no cash)
      const wallet = reactive({ balance: 0, effective_cost_mins: 0, shortfall_mins: 0, shortfall_ks: 0, is_member: false, settle: false });
      const walletLoading = ref(false);
      const sessionLocked = ref(false);  // true when voucher came from session end (mins locked)
      const effectiveMins = computed(() => {
        const base = Number(form.mins) || 0;
        return sessionLocked.value ? Math.max(0, base + (Number(form.adjust) || 0)) : base;
      });
      const netTotal = computed(() => Math.max(0, (Number(form.game_amt)||0) + (Number(form.food_total)||0) - (Number(form.discount)||0)));
      const autoGameAmt = computed(() => Math.round(( effectiveMins.value * (Number(form.base_rate)||0) * (Number(form.multiplier)||1) ) / 60));
      // ══ Wallet settle computeds ══
      // effective cost in wallet-mins = effectiveMins × multiplier (matches sale bot launch_session_sale)
      const effectiveCostMins = computed(() => Math.round(effectiveMins.value * (Number(form.multiplier)||1)));
      // cash value of the game time (for receipt; recorded even when wallet covers it)
      const gameValueKs = computed(() => Math.round(effectiveCostMins.value * (Number(form.base_rate)||0) / 60));
      // Is the current selection a real member (non-guest)?
      const isMember = computed(() => !!form.member_id && !/^0?\s*\(Guest\)$/i.test(String(form.member_id)) && !/^guest$/i.test(String(form.member_search)) );
      // wallet settles the whole game cost?
      const walletSufficient = computed(() => wallet.is_member && wallet.effective_cost_mins > 0 && wallet.balance >= wallet.effective_cost_mins);
      // shortfall (cash-down) in Ks when wallet insufficient
      const shortfallKs = computed(() => (wallet.is_member && wallet.shortfall_ks > 0) ? wallet.shortfall_ks : 0);
      // rawCash = pre-discount cash payable (client mirror of the server's cash_payable)
      const rawCash = computed(() => {
        const foodTotal = Number(form.food_total) || 0;
        const gameAmt = Number(form.game_amt) || 0;
        const ecost = effectiveCostMins.value;
        if (isMember.value && wallet.balance > 0 && ecost > 0 && wallet.balance >= ecost) return foodTotal;
        if (isMember.value && wallet.balance > 0 && ecost > 0 && wallet.balance < ecost) return Math.round((ecost - wallet.balance) * (Number(form.base_rate) || 0) / 60) + foodTotal;
        return gameAmt + foodTotal;
      });
      // ── Server-authoritative money lines (fall back to the local mirror until calc lands) ──
      const subtotalKs = computed(() => serverCalc.value ? (Number(serverCalc.value.cash_payable) || 0) : rawCash.value);
      const totalDiscountKs = computed(() => serverCalc.value ? (Number(serverCalc.value.total_discount) || 0) : (Number(form.discount) || 0));
      const discountLines = computed(() => (serverCalc.value && Array.isArray(serverCalc.value.breakdown)) ? serverCalc.value.breakdown : []);
      const payableBeforeDeposit = computed(() => serverCalc.value ? (Number(serverCalc.value.net_total) || 0) : Math.max(0, rawCash.value - (Number(form.discount) || 0)));
      // Cash due = server total − applied deposit (Boss: Net Total = the money to pay)
      const cashNetTotal = computed(() => Math.max(0, payableBeforeDeposit.value - depositAppliedAmt.value));
      // Auto-calc game amount instantly whenever mins/adjust/base_rate/multiplier change (no Auto button needed)
      watch(
        [() => form.mins, () => form.adjust, () => form.base_rate, () => form.multiplier],
        () => { form.game_amt = autoGameAmt.value; }
      );

      const members = ref([]);
      const consolesList = ref([]);
      const memberPicker = ref(false);
      const consolePicker = ref(false);
      const gamePicker = ref(false);
      const foodPicker = ref(false);
      const payPicker = ref(false);
      const payAmountPicker = ref(false);
      const pendingMethod = ref("");
      const payAmountInput = ref(null);
      const memberQuery = ref("");
      const submitting = ref(false);
      const payColumns = PAY_METHODS.map((p) => ({ text: p, value: p }));

      // 🧾 Sale-complete voucher preview (drives 80mm thermal print)
      const voucherOpen = ref(false);
      const lastVoucher = ref(null);
      const printing = ref(false);
      const pendingSaved = ref(false);   // defer the "saved" emit until the voucher popup closes
      // Keep the user on the form (with the discount breakdown visible) while the
      // voucher preview is open; refresh the Today list once it is dismissed.
      watch(voucherOpen, (v) => { if (!v && pendingSaved.value) { pendingSaved.value = false; emit("saved"); } });
      async function doPrintVoucher() {
        if (!lastVoucher.value) return;
        printing.value = true;
        try { await PSV.printVoucher(lastVoucher.value); }
        finally { printing.value = false; }
      }

      const paidTotal = computed(() => form.payment_splits.reduce((s, x) => s + (Number(x.amount)||0), 0));
      const splitRemaining = computed(() => Math.max(0, Number(cashNetTotal.value) - paidTotal.value));
      const paymentMethodComputed = computed(() => {
        const active = form.payment_splits.filter((s) => Number(s.amount) > 0);
        return active.length ? active.map((s) => `${s.method}:${s.amount}`).join("|") : "Cash:" + Number(cashNetTotal.value);
      });

      const gamesList = ref([]);
      const gameColumns = computed(() => gamesList.value.map((g) => ({ text: g.game_title || g.title || g.name, value: g.game_title || g.title || g.name })));

      // Food menu grouped + selected line items
      const foodMenu = ref({});
      const foodLines = ref([]); // [{name, price, qty}]
      const foodGroups = computed(() => {
        const m = foodMenu.value || {};
        if (Array.isArray(m)) return [{ cat: "", items: m.map((x) => ({ name: x.item_name || x.name, price: x.price })) }];
        const groups = [];
        for (const cat in m) {
          const v = m[cat];
          const items = [];
          if (v && typeof v === "object" && !Array.isArray(v)) {
            for (const nm in v) items.push({ name: nm, price: v[nm] });
          } else if (Array.isArray(v)) {
            v.forEach((x) => items.push({ name: (x && (x.item_name || x.name)) || x, price: x && x.price }));
          }
          if (items.length) groups.push({ cat, items });
        }
        return groups;
      });

      const filteredMembers = computed(() => {
        const q = memberQuery.value.trim().toLowerCase();
        if (!q) return members.value.slice(0, 50);
        return members.value.filter((m) => (m.name && String(m.name).toLowerCase().includes(q)) || (m.phone && String(m.phone).includes(q)) || (m.id && String(m.id).includes(q))).slice(0, 50);
      });

      function qtyOf(name) { const l = foodLines.value.find((x) => x.name === name); return l ? l.qty : 0; }
      function setFoodQty(it, qty) {
        qty = Number(qty) || 0;
        const i = foodLines.value.findIndex((x) => x.name === it.name);
        if (qty <= 0) { if (i >= 0) foodLines.value.splice(i, 1); }
        else if (i >= 0) foodLines.value[i].qty = qty;
        else foodLines.value.push({ name: it.name, price: it.price, qty });
        recomputeFood();
      }
      function removeFood(i) { foodLines.value.splice(i, 1); recomputeFood(); }
      function recomputeFood() {
        form.food_total = foodLines.value.reduce((s, l) => s + (Number(l.qty)||0) * (Number(l.price)||0), 0);
        form.food_items = foodLines.value.map((l) => `${l.qty} ${l.name}`).join(", ");
      }

      async function loadRefs() {
        const [m, c, g, f] = await Promise.allSettled([
          PSV.api("/api/fetch_members?limit=300"),
          PSV.api("/api/fetch_console_status"),
          PSV.api("/api/fetch_games"),
          PSV.api("/api/fetch_food_menu"),
        ]);
        if (m.status === "fulfilled") { const d = PSV.unwrap(m.value); members.value = (d && d.members) || []; }
        if (c.status === "fulfilled") { const d = PSV.unwrap(c.value); consolesList.value = (d && d.consoles) || []; }
        if (g.status === "fulfilled") { const d = PSV.unwrap(g.value); gamesList.value = Array.isArray(d) ? d : (d && d.games) || []; }
        if (f.status === "fulfilled") { const d = PSV.unwrap(f.value); foodMenu.value = d || {}; }
        // 🧾 Session-end voucher prefill (set by Consoles doEndSession)
        const pending = PSV.pendingSessionSale;
        if (pending) {
          PSV.pendingSessionSale = null;
          if (pending.member_name && pending.member_name !== "Guest" && pending.member_name !== "0 (Guest)") {
            const m = members.value.find((x) => x.id === pending.member_id || x.name === pending.member_name);
            if (m) { form.member_id = m.id; form.member_search = m.name || m.id; setWalletMember(m.id, m.wallet_balance ?? m.liability_mins); }
            else { form.member_search = pending.member_name; form.member_id = pending.member_name; setWalletMember(pending.member_id || pending.member_name, 0); }
          } else { pickGuest(); }
          form.mins = Number(pending.mins) || 0;
          form.adjust = 0;
          sessionLocked.value = true;  // 🔒 session-end voucher: mins locked, only ±10 adjust allowed
          if (pending.console_id) {
            const cc = consolesList.value.find((x) => (x.console_id || x.id) === pending.console_id);
            if (cc) pickConsole(cc); else form.console_id = pending.console_id;
          }
          if (pending.booking_id) {
            PSV.api("/api/food-cart/booking/" + encodeURIComponent(pending.booking_id)).then((res) => {
              const d2 = PSV.unwrap(res);
              const items = (d2 && d2.items) || [];
              items.forEach((it) => {
                const name = it.item_name;
                const qty = Number(it.quantity) || 1;
                const price = Number(it.unit_price) || 0;
                const i = foodLines.value.findIndex((x) => x.name === name);
                if (i >= 0) foodLines.value[i].qty += qty;
                else foodLines.value.push({ name, price, qty });
              });
              recomputeFood();
            }).catch(() => {});
          }
        }
      }
      onMounted(() => { loadRefs().then(() => scheduleCalc()); loadPromos(); loadActiveBookings(); });
      // 🚩 WAVE 2: proactive in-session detection + verified-deposit lookup on picks
      async function probeDeposits() {
        try {
          if (depositRedeem.value) return; // already flagged from the Booking screen
          const arrs = await Promise.all([
            PSV.api("/api/bookings?status=confirmed"),
            PSV.api("/api/bookings?status=checked_in"),
          ]);
          let list = [];
          arrs.forEach((r) => { const d = PSV.unwrap(r); list = list.concat((d && d.bookings) || []); });
          const cid = String(form.console_id || "").trim().toLowerCase();
          const mid = String(form.member_id || "").trim().toLowerCase();
          const cand = list.find((b) => String(b.deposit_status || "").toLowerCase() === "verified" &&
            ((cid && String(b.console_id || "").trim().toLowerCase() === cid) ||
             (mid && mid !== "0 (guest)" && String(b.member_id || "").trim().toLowerCase() === mid)));
          if (cand) {
            try { PSV.pendingDepositRedeem = { booking_id: cand.id, amount: Number(cand.deposit_amount) || 0, method: cand.deposit_method || "", ts: Date.now() }; } catch (_e) {}
            depositRedeem.value = PSV.pendingDepositRedeem;
            depositApplied.value = false;
          }
        } catch (_e) {}
      }

      function openConsole() { if (!consolesList.value.length) loadRefs(); consolePicker.value = true; haptic("light"); }
      function pickMember(m) { form.member_id = m.id; form.member_search = m.name || m.id; memberPicker.value = false; setWalletMember(m.id, m.wallet_balance ?? m.liability_mins); probeDeposits(); scheduleCalc(); }
      function pickGuest() { form.member_id = "0 (Guest)"; form.member_search = "Guest"; memberPicker.value = false; clearWalletMember(); depositRedeem.value = null; depositApplied.value = false; scheduleCalc(); }
      // 🪙 Set member wallet state (auto-settle) + fetch live balance
      function setWalletMember(member_id, fallback) {
        wallet.is_member = true;
        wallet.settle = true;
        wallet.balance = Number(fallback) || 0;
        walletLoading.value = true;
        PSV.api("/api/fetch_wallet_mins/" + encodeURIComponent(member_id)).then((r) => {
          const v = Number(PSV.unwrap(r));
          if (!isNaN(v)) wallet.balance = v;
          computeWalletSettle();
        }).catch(() => {}).finally(() => { walletLoading.value = false; computeWalletSettle(); });
        computeWalletSettle();
      }
      function clearWalletMember() { wallet.is_member = false; wallet.settle = false; wallet.balance = 0; wallet.effective_cost_mins = 0; wallet.shortfall_mins = 0; wallet.shortfall_ks = 0; }
      function computeWalletSettle() {
        if (!wallet.is_member) { wallet.effective_cost_mins = 0; wallet.shortfall_mins = 0; wallet.shortfall_ks = 0; return; }
        wallet.effective_cost_mins = effectiveCostMins.value;
        if (wallet.balance >= wallet.effective_cost_mins) {
          wallet.shortfall_mins = 0; wallet.shortfall_ks = 0;
        } else {
          wallet.shortfall_mins = wallet.effective_cost_mins - wallet.balance;
          wallet.shortfall_ks = Math.round(wallet.shortfall_mins * (Number(form.base_rate)||0) / 60);
        }
      }
      // Recompute wallet settle whenever mins/multiplier/base_rate change
      watch([() => effectiveMins.value, () => form.multiplier, () => form.base_rate], () => { computeWalletSettle(); });
      // 🏷️ WAVE 2: re-run the server-side discount calc whenever the cart changes (debounced)
      watch(
        [() => form.game_amt, () => form.food_total, () => form.discount, () => effectiveMins.value,
         () => form.base_rate, () => form.multiplier, () => wallet.balance, () => isMember.value,
         () => promo.id, () => coupon.applied, () => focItem.value,
         () => foodLines.value.map((l) => l.name + ':' + l.qty).join(',')],
        () => scheduleCalc()
      );
      function pickConsole(c) {
        form.console_id = c.console_id || c.id; consolePicker.value = false;
        probeDeposits(); loadActiveBookings();
        const cid = form.console_id;
        // 📥 Prefill existing session food cart when an active console is picked mid-session
        const cstatus = String(c.status || "").toLowerCase();
        const cbid = c.booking_id;
        if (cbid && (cstatus === "active" || cstatus === "checked_in")) {
          (async () => {
            try {
              const ex = await PSV.api("/api/food-cart/booking/" + encodeURIComponent(cbid));
              const d2 = PSV.unwrap(ex);
              const items = (d2 && d2.items) || [];
              items.forEach((it) => {
                const name = it.item_name;
                const qty = Number(it.quantity) || 1;
                const price = Number(it.unit_price) || 0;
                const i = foodLines.value.findIndex((x) => x.name === name);
                if (i >= 0) foodLines.value[i].qty += qty;
                else foodLines.value.push({ name, price, qty });
              });
              recomputeFood();
            } catch (ee) { /* existing order optional */ }
          })();
        }
        (async () => {
          try {
            const [rateRes, multRes] = await Promise.allSettled([
              PSV.api("/api/fetch_base_rate"),
              PSV.api("/api/fetch_console_multiplier/" + encodeURIComponent(cid)),
            ]);
            if (rateRes.status === "fulfilled") { const v = Number(PSV.unwrap(rateRes.value)); if (!isNaN(v)) form.base_rate = v; }
            if (multRes.status === "fulfilled") { const v = Number(PSV.unwrap(multRes.value)); if (!isNaN(v)) form.multiplier = v; else form.multiplier = 1.0; }
            pickGameAmtAuto();
          } catch (e) { /* keep manual values */ }
        })();
      }
      function pickGameAmtAuto() { form.game_amt = autoGameAmt.value; }
      function openFoodPicker() { if (!foodMenu.value || !Object.keys(foodMenu.value).length) loadRefs(); foodPicker.value = true; haptic("light"); }
      function pickGame({ selectedOptions }) { form.game_name = selectedOptions[0].value; gamePicker.value = false; }
      function openPayMethod() { if (!form.payment_splits.length) form.payment_splits = []; payPicker.value = true; haptic("light"); }
      function pickPayMethod({ selectedOptions }) { pendingMethod.value = selectedOptions[0].value; payPicker.value = false; payAmountInput.value = splitRemaining.value; payAmountPicker.value = true; }
      function confirmSplit() {
        const amt = Number(payAmountInput.value) || 0;
        if (amt <= 0) { showToast("Enter amount", "warning"); return; }
        if (amt > splitRemaining.value + 0.001) { showToast("Amount exceeds remaining " + splitRemaining.value, "warning"); return; }
        if (!form.payment_splits) form.payment_splits = [];
        form.payment_splits.push({ method: pendingMethod.value, amount: amt });
        payAmountPicker.value = false;
      }
      function removeSplit(i) { form.payment_splits.splice(i, 1); }
      function pickPay({ selectedOptions }) { form.payment_method = selectedOptions[0].value; payPicker.value = false; }

      async function submit() {
        if (submitting.value) return;
        haptic("medium");
        // 🚨 In-session guard (WAVE 2): warn (amber) if the member/console is mid-session.
        // Never hard-blocks — staff can override with awareness.
        const guard = await ensureNoActiveSession();
        if (guard === "cancel") return;
        submitting.value = true;
        try {
          const member_id = form.member_id || form.member_search || "0 (Guest)";
          const isMemberSale = isMember.value;

          // ── 🏷️ MONEY RULE: ask the server for the discounted total (fresh calc) ──
          await runCalc();
          const sc = serverCalc.value;
          const netAfterDiscount = sc ? (Number(sc.net_total) || 0) : Math.max(0, rawCash.value - (Number(form.discount) || 0));
          const discTotal = sc ? (Number(sc.total_discount) || 0) : (Number(form.discount) || 0);
          const depAmt = depositAppliedAmt.value;
          const payableNow = Math.max(0, netAfterDiscount - depAmt);

          // 🪙 Wallet settle (mirror sale bot launch_session_sale / confirm_summary):
          //    sufficient → wallet covers game (NO cash for game); insufficient → cash-down shortfall.
          let wallet_deduct = 0;
          let final_game_amt = Number(form.game_amt) || 0;
          let final_pm = paymentMethodComputed.value;
          if (isMemberSale && effectiveMins.value > 0) {
            computeWalletSettle();
            const ecost = wallet.effective_cost_mins || effectiveCostMins.value;
            const bal = Number(wallet.balance) || 0;
            if (bal >= ecost) {
              wallet_deduct = ecost;
            } else {
              wallet_deduct = bal;
              final_game_amt = Math.round(Math.max(0, ecost - bal) * (Number(form.base_rate) || 0) / 60);
            }
          }
          if (form.payment_splits.length) {
            // Validate split only against the CASH portion actually payable
            if (Number(paidTotal.value) < payableNow - 0.001) {
              showToast("Payments total " + fKs(paidTotal.value) + " < Cash due " + fKs(payableNow), "warning");
              submitting.value = false; return;
            }
            final_pm = paymentMethodComputed.value;
          }

          // ── 🎟️ Coupon redemption is performed by the SERVER inside the /api/sales/record
          //    money path (idempotent on sale_ref), so the client never double-redeems.
          //    The UI shows the real deducted minutes from the response below.
          const saleRef = PSV.genClientRef();
          const cart = cartForCalc();
          const payload = {
            voucher_no: form.voucher_no, member_id: member_id,
            console_id: form.console_id, mins: form.mins, base_rate: form.base_rate,
            multiplier: form.multiplier, game_amt: final_game_amt, food_items: form.food_items,
            food_total: form.food_total, discount: Number(form.discount) || 0, net_total: Math.max(0, rawCash.value - (Number(form.discount) || 0)),
            // NOTE: discount/net above are the MANUAL-only fallback. When promo_id or coupon_code
            // is present the server recomputes discount+net (compute_totals) and ignores these.
            payment_method: final_pm, staff: PSV.getStaff().name,
            wallet_deduct: wallet_deduct,
            // ── money-path parity: same cart the preview calc used ──
            promotion_name: promo.title || "",
            promo_id: promo.id || "",
            coupon_code: coupon.applied ? coupon.code : "",
            coupon_minutes: coupon.applied ? coupon.minutes : 0,
            manual_discount: Number(form.discount) || 0,
            foc_item: focItem.value || "",
            is_member: !!cart.is_member,
            wallet_mins: cart.wallet_mins || 0,
            effective_cost_mins: cart.effective_cost_mins || 0,
            gross_total: (Number(final_game_amt) || 0) + (Number(form.food_total) || 0),
            booking_id: (depositApplied.value && depositRedeem.value) ? depositRedeem.value.booking_id : "",
            client_ref: saleRef,
          };
          const res = PSV.unwrap(await PSV.api("/api/sales/record", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }));
          // 🎟️ Reflect the server's coupon redemption (authoritative).
          if (coupon.applied && res && res.coupon_redeem) {
            const cm = res.coupon_redeem;
            coupon.message = "✓ Redeemed — " + (cm.deducted_minutes != null ? cm.deducted_minutes : coupon.minutes) + " mins" + (cm.remaining_minutes != null ? (" (" + cm.remaining_minutes + " left)") : "");
            showToast("Coupon " + coupon.code + " · -" + (cm.deducted_minutes != null ? cm.deducted_minutes : coupon.minutes) + " mins", "success");
          }
          if (res && res.duplicate) showToast("Duplicate voucher " + (res.voucher_no || ""), "warning");
          else {
            // 🧾 Stock-out for food items (mirror sale bot _sale_bg → api_add_stock_out)
            // Ensures Mini App sales also decrement inventory / log stock_out rows.
            const vno = (res && res.voucher_no) || form.voucher_no;
            try {
              for (const fl of foodLines.value) {
                const name = (fl.name || "").trim();
                const qty = Number(fl.qty) || 0;
                const price = Number(fl.price) || 0;
                if (!name || qty <= 0) continue;
                await PSV.api("/api/stock-out/log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ item_name: name, qty, unit_cost: price, total_cost: qty * price, staff: PSV.getStaff().name, notes: "Voucher: " + vno }) });
              }
            } catch (soe) { console.warn("stock-out log warning:", soe); }
            // 🧾 Voucher snapshot for on-screen preview + 80mm thermal print
            const vItems = [];
            if (effectiveMins.value > 0 || (Number(form.game_amt) || 0) > 0) {
              vItems.push({ name: "🎮 " + (form.game_name || "Game time") + " (" + effectiveMins.value + "m)", qty: 1, price: Number(form.game_amt) || 0 });
            }
            for (const fl of foodLines.value) {
              const nm = (fl.name || "").trim(); const q = Number(fl.qty) || 0; const p = Number(fl.price) || 0;
              if (nm && q > 0) vItems.push({ name: "🍜 " + nm, qty: q, price: p });
            }
            const vSubtotal = sc ? (Number(sc.cash_payable) || 0) : rawCash.value;
            // Server response is authoritative for the recorded discount / payable / deposit (idempotent)
            const serverDiscount = (res && res.discount != null) ? Number(res.discount) : discTotal;
            const cashDueFinal = (res && res.payable_total != null) ? Number(res.payable_total) : payableNow;
            const depFinal = (res && res.deposit_deducted != null) ? Number(res.deposit_deducted) : depAmt;
            const vPay = final_pm + (depFinal > 0 ? (" + Deposit " + fKs(depFinal) + " Ks") : "");
            lastVoucher.value = {
              voucher_no: vno,
              date: PSV.todayMMT(),
              time: new Date().toTimeString().slice(0, 5),
              staff: PSV.getStaff().name,
              member: isMemberSale ? (form.member_search || form.member_id) : "Guest",
              items: vItems,
              subtotal: vSubtotal,
              discount: serverDiscount,
              total: cashDueFinal + depFinal,
              deposit: depFinal,
              cashDue: cashDueFinal,
              payment: vPay,
              balance: wallet_deduct > 0 ? ("Wallet −" + wallet_deduct + " mins" + (res && res.wallet_balance_after != null ? (" (bal " + res.wallet_balance_after + ")") : "")) : "",
              notes: "Subtotal: " + fKs(vSubtotal) + " Ks" + (serverDiscount > 0 ? (" | Discount: -" + fKs(serverDiscount) + " Ks") : "") + (depFinal > 0 ? (" | Deposit: -" + fKs(depFinal) + " Ks") : "") + (promo.title ? (" | Promo: " + promo.title) : "") + (coupon.applied ? (" | Coupon: " + coupon.code) : ""),
            };
            voucherOpen.value = true;
            try { window.PSVLastVoucher = { raw: lastVoucher.value }; } catch (_e) {}
            showToast("Saved ✓ " + vno + (serverDiscount > 0 ? (" · Discount −" + fKs(serverDiscount)) : "") + (wallet_deduct > 0 ? (" · Wallet −" + wallet_deduct + "m") : ""), "success");
            form.voucher_no = PSV.genVoucherNo(); form.member_id = ""; form.member_search = ""; form.console_id = "";
            form.game_name = ""; form.mins = 0; form.base_rate = 0; form.multiplier = 1; form.game_amt = 0;
            form.food_items = ""; form.food_total = 0; form.discount = 0;
            form.payment_splits = [];
            foodLines.value = [];
            clearWalletMember();
            // WAVE 2: reset discount + deposit state for the next sale
            promo.id = ""; promo.title = ""; promo.type = ""; promo.notes = ""; focItem.value = "";
            coupon.code = ""; coupon.applied = false; coupon.minutes = 0; coupon.discount = 0; coupon.message = "";
            depositRedeem.value = null; depositApplied.value = false;
            try { PSV.pendingDepositRedeem = null; } catch (_e2) {}
            serverCalc.value = null;
            loadActiveBookings();
            pendingSaved.value = true;   // emit("saved") fires when the voucher popup closes
          }
        } catch (e) { showToast("Error: " + e.message, "fail"); }
        finally { submitting.value = false; }
      }

      return { form, wallet, walletLoading, sessionLocked, effectiveMins, effectiveCostMins, gameValueKs, isMember, walletSufficient, shortfallKs, cashNetTotal, netTotal, autoGameAmt, paidTotal, splitRemaining, members, consoles: consolesList, memberPicker, consolePicker, gamePicker, foodPicker, payPicker, payAmountPicker, pendingMethod, payAmountInput, memberQuery, submitting, filteredMembers, payColumns, gameColumns, foodGroups, foodLines, qtyOf, setFoodQty, removeFood, openConsole, pickMember, pickGuest, pickConsole, pickGame, pickPay, pickPayMethod, openPayMethod, confirmSplit, removeSplit, openFoodPicker, submit, voucherOpen, lastVoucher, printing, doPrintVoucher, fKs, depositRedeem, clearRedeem,
               // ── WAVE 2: promo / coupon / discount / deposit / in-session guard ──
               sessionWarn, depositApplied, depositAppliedAmt, applyDeposit, promos, promoOpen, promo, promoText, openPromo, pickPromo, clearPromo, focOpen, focItem, pickFoc, coupon, applyCoupon, clearCoupon, calcStatus, discountLines, subtotalKs, totalDiscountKs, payableBeforeDeposit, rawCash, scheduleCalc };
    },
  };

  // ── VIEW: Members ───────────────────────────────────────
  const Members = {
    template: `
      <div class="pv-page">
        <van-sticky><van-search v-model="query" placeholder="Search name / phone / ID" /></van-sticky>
        <div style="padding:12px 16px 4px">
          <van-button type="primary" block icon="plus" @click="router.push('/members/register')">New Member</van-button>
        </div>
        <van-skeleton title :row="4" v-if="loading" style="margin:12px" />
        <van-empty v-else-if="!filtered.length" description="No members found" />
        <van-cell-group v-else inset style="margin-top:8px">
          <van-cell v-for="m in filtered" :key="m.id" :title="m.name || m.id" :label="'ID ' + (m.id||'-') + ' · ' + (m.phone||'-') + ' · ' + (m.wallet_balance ?? m.liability_mins ?? 0) + ' mins'" is-link @click="openDetail(m)" />
        </van-cell-group>

        <van-action-sheet v-model:show="detail.show" :title="detail.title">
          <div style="padding:8px 16px 24px">
            <van-cell-group inset>
              <van-cell title="Wallet (mins)" :value="fKs(detail.balance)" />
              <van-cell title="Net Spend" :value="fKs(detail.net_spend) + ' Ks'" />
              <van-cell title="Rank" :value="detail.rank" />
              <van-cell title="Referral" :value="detail.referral || '-'" />
            </van-cell-group>
            <div style="display:flex;gap:10px;padding:14px 0 0">
              <van-button type="primary" block @click="act('topup')">Top Up</van-button>
              <van-button type="warning" block @click="act('deduct')">Deduct</van-button>
              <van-button plain type="default" block @click="act('referral')">Referral</van-button>
            </div>
          </div>
        </van-action-sheet>

        <van-dialog v-model:show="dlg.show" :title="dlg.title" show-cancel-button @confirm="confirmAction">
          <div style="padding:16px">
            <van-field v-if="dlg.mode !== 'referral'" v-model.number="dlg.amount" type="number" :label="dlg.amountLabel" placeholder="0" />
            <van-field v-else v-model="dlg.code" label="Referral Code" placeholder="CODE" />
          </div>
        </van-dialog>
      </div>
    `,
    setup() {
      const query = ref("");
      const loading = ref(true);
      const members = ref([]);
      const detail = reactive({ show: false, title: "", member_id: "", balance: 0, net_spend: 0, rank: "-", referral: "" });
      const dlg = reactive({ show: false, title: "", mode: "", amount: 0, amountLabel: "", code: "" });

      const filtered = computed(() => {
        const q = query.value.trim().toLowerCase();
        if (!q) return members.value;
        return members.value.filter((m) => (m.name && String(m.name).toLowerCase().includes(q)) || (m.phone && String(m.phone).includes(q)) || (m.id && String(m.id).includes(q)));
      });

      async function load() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_members?limit=300")); members.value = (d && d.members) || []; }
        catch (e) { showToast("Load failed: " + e.message, "fail"); members.value = []; }
        finally { loading.value = false; }
      }
      onMounted(load);

      async function openDetail(m) {
        haptic("light");
        detail.member_id = m.id; detail.title = (m.name || m.id) + " · " + (m.phone || "");
        detail.balance = m.wallet_balance ?? m.liability_mins ?? 0; detail.show = true;
        try {
          const [data, wallet] = await Promise.allSettled([
            PSV.api("/api/fetch_member_data/" + encodeURIComponent(m.id)),
            PSV.api("/api/fetch_wallet_mins/" + encodeURIComponent(m.id)),
          ]);
          if (data.status === "fulfilled") { const d = PSV.unwrap(data.value); if (d) { detail.net_spend = d.net_spend ?? 0; detail.rank = d.rank_raw ?? "-"; detail.referral = d.referral_code ?? ""; if (d.wallet_mins != null) detail.balance = d.wallet_mins; } }
          if (wallet.status === "fulfilled") { detail.balance = PSV.unwrap(wallet.value) ?? detail.balance; }
        } catch (_e) {}
      }

      function act(mode) {
        detail.show = false; dlg.mode = mode;
        if (mode === "topup") { dlg.title = "Top Up — " + detail.member_id; dlg.amountLabel = "Amount (Ks)"; }
        else if (mode === "deduct") { dlg.title = "Wallet Deduct — " + detail.member_id; dlg.amountLabel = "Deduct (mins)"; }
        else { dlg.title = "Referral — " + detail.member_id; dlg.code = detail.referral || ""; }
        dlg.amount = 0; dlg.show = true;
      }

      async function confirmAction() {
        const mid = detail.member_id;
        try {
          if (dlg.mode === "topup") {
            const amt = Number(dlg.amount) || 0;
            const res = PSV.unwrap(await PSV.api("/api/topup/log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member_id: mid, amount: amt, mins_added: amt, payment_method: "Cash", staff_name: PSV.getStaff().name, client_ref: PSV.genClientRef() }) }));
            if (res && res.duplicate) showToast("Already applied — " + (res.balance_mins ?? "?") + " mins", "warning");
            else showToast("Top-up done ✓ " + (res.balance_mins ?? "?") + " mins", "success");
          } else if (dlg.mode === "deduct") {
            const mins = Number(dlg.amount) || 0;
            const res = PSV.unwrap(await PSV.api("/api/wallet/deduct", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member_id: mid, deduct_mins: mins, total_mins: mins }) }));
            showToast("Deducted ✓ " + (res.balance_after ?? "?"), "success");
          } else {
            const res = PSV.unwrap(await PSV.api("/api/save_referral_code", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member_id: mid, referral_code: dlg.code }) }));
            showToast("Referral saved ✓", "success");
          }
          load();
        } catch (e) { showToast("Error: " + e.message, "fail"); }
      }

      return { query, loading, members, filtered, detail, dlg, fKs, router, openDetail, act, confirmAction };
    },
  };

  // ── VIEW: Member register ───────────────────────────────
  const MemberRegister = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="New Member" back="/members" />
        <van-cell-group inset style="margin-top:8px">
          <van-field v-model="form.member_id" label="Member ID" placeholder="auto" />
          <van-field v-model="form.name" label="Name" placeholder="Name" />
          <van-field v-model="form.phone" label="Phone" type="tel" placeholder="09..." />
          <van-field v-model.number="form.initial_mins" type="number" label="Initial Mins" placeholder="0" />
          <van-field v-if="!form.is_gift" v-model.number="form.amount" type="number" label="Amount (Ks)" placeholder="0" />
          <van-field v-if="!form.is_gift" v-model.number="form.kpay" type="number" label="KPay (Ks)" placeholder="0" />
          <van-field v-if="!form.is_gift" v-model.number="form.cash" type="number" label="Cash (Ks)" placeholder="0" />
          <van-field v-model="form.referral_code" label="Referral Code" placeholder="(optional)" />
        </van-cell-group>
        <van-cell-group inset style="margin-top:10px">
          <van-cell title="Gift / Free card" label="Issue free minutes — requires the owner PIN">
            <template #right-icon><van-switch v-model="form.is_gift" size="22" @change="onGiftToggle" /></template>
          </van-cell>
          <van-field v-if="form.is_gift" v-model="form.pin" type="password" label="Owner PIN" placeholder="Enter owner PIN" autocomplete="off" />
        </van-cell-group>
        <div v-if="form.is_gift" style="margin:0 16px 4px;color:var(--pv-muted);font-size:12px">Gift card grants free minutes with no payment. A wrong PIN shows an error.</div>
        <div style="padding:14px 16px">
          <van-button type="primary" block :loading="submitting" @click="submit">Register</van-button>
        </div>
      </div>
    `,
    setup() {
      const form = reactive({ member_id: "", name: "", phone: "", initial_mins: 0, amount: 0, kpay: 0, cash: 0, referral_code: "", is_gift: false, pin: "" });
      const submitting = ref(false);
      function onGiftToggle(v) { haptic("light"); if (v) { form.amount = 0; form.kpay = 0; form.cash = 0; } }
      async function nextId() { try { const id = PSV.unwrap(await PSV.api("/api/next_member_id")); if (id) form.member_id = String(id); } catch (_e) {} }
      onMounted(nextId);
      async function submit() {
        submitting.value = true; haptic("medium");
        try {
          const payload = { member_id: form.member_id, name: form.name, phone: form.phone, staff: PSV.getStaff().name, initial_mins: form.initial_mins, amount: form.amount, kpay: form.kpay, cash: form.cash, mins_added: form.initial_mins, referral_code: form.referral_code };
          if (form.is_gift) { payload.is_gift = true; payload.pin = form.pin; }  // PIN is never logged
          const raw = await PSV.api("/api/members/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
          if (raw && raw.success === false) throw new Error(raw.error || "Register failed");
          const res = PSV.unwrap(raw);
          showToast("Saved ✓ " + (res && res.member_id ? res.member_id : ""), "success");
          form.name = ""; form.phone = ""; form.initial_mins = 0; form.amount = 0; form.kpay = 0; form.cash = 0; form.referral_code = ""; form.is_gift = false; form.pin = "";
          nextId();
        } catch (e) {
          const m = String(e && e.message ? e.message : "");
          if (/owner pin|invalid pin|403/i.test(m)) showToast("Owner PIN required — the PIN is incorrect.", "fail");
          else showToast("Error: " + m, "fail");
        }
        finally { submitting.value = false; }
      }
      return { form, submitting, submit, onGiftToggle };
    },
  };

  // ── WAVE 1: booking ops + deposit helpers ───────────────
  const BK_REJECT_REASONS = [
    { text: "Slot unavailable", note: "No free time slot", value: "slot_unavailable" },
    { text: "Deposit not received", note: "Payment not received", value: "payment_not_received" },
    { text: "Other", note: "Add a note", value: "other" },
  ];
  const BK_CANCEL_REASONS = [
    { text: "Customer cancelled", note: "Customer requested", value: "customer_cancelled" },
    { text: "Console unavailable", note: "Console busy or switched", value: "console_unavailable" },
    { text: "No-show", note: "Customer did not arrive", value: "no_show" },
    { text: "Other", note: "Add a note", value: "other" },
  ];
  const BK_STATUS = {
    pending:          { en: "Pending",    tone: "warn" },
    pending_check_in: { en: "Confirmed",  tone: "warn" },
    confirmed:        { en: "Confirmed",  tone: "ok" },
    checked_in:       { en: "Checked-in", tone: "ok" },
    active:           { en: "Active",     tone: "gold" },
    done:             { en: "Completed",  tone: "dim" },
    completed:        { en: "Completed",  tone: "dim" },
    cancelled:        { en: "Cancelled",  tone: "danger" },
    rejected:         { en: "Rejected",   tone: "danger" },
  };
  function bkKey(s) { return String(s == null ? "" : s).toLowerCase().trim(); }
  function bkStatus(s) { return BK_STATUS[bkKey(s)] || { en: (s || "-"), tone: "dim" }; }
  const HJSON = { "Content-Type": "application/json" };
  function psvStaffName() { try { return (PSV.getStaff() || {}).name || "staff"; } catch (_e) { return "staff"; } }
  function confirmBox(title, message, okText, cancelText, danger) {
    return new Promise(function (resolve) {
      try {
        if (window.vant && vant.showConfirmDialog) {
          vant.showConfirmDialog({
            title: title,
            message: message,
            confirmButtonText: okText || "OK",
            cancelButtonText: cancelText || "Cancel",
            confirmButtonColor: danger ? "#f87171" : "#f0b429",
            messageAlign: "left",
          }).then(function () { resolve(true); }).catch(function () { resolve(false); });
          return;
        }
      } catch (_e) {}
      try { resolve(window.confirm(title + " — " + message)); } catch (_e2) { resolve(false); }
    });
  }

  // ══════════════════════════════════════════════════════════
  //  WAVE 4 — BOOKING TIMELINE (consoles × hours, mobile-first)
  //  Modelled on the web dashboard's TimelineView.vue: console rows × time
  //  axis, coloured status blocks, date navigation, a "now" indicator and a
  //  stale-response guard. Layout is CSS grid (sticky console column + sticky
  //  hour header); hour lines are static — there is NO per-minute DOM.
  //  Tapping a block reuses the existing booking detail sheet (embedded mode
  //  emits `pick`; standalone mode deep-links into /bookings).
  // ══════════════════════════════════════════════════════════
  const TL_OPEN_HOUR = 9, TL_CLOSE_HOUR = 23, TL_HOUR_W = 64;
  const TL_GRID_HOURS = (function () { const a = []; for (let h = TL_OPEN_HOUR; h < TL_CLOSE_HOUR; h++) a.push(h); return a; })();
  const TL_DEAD = ["cancelled", "canceled", "rejected", "no_show", "noshow", "no-show", "expired"];
  function tlMin(t) {
    const m = String(t == null ? "" : t).match(/(\d{1,2}):(\d{2})/);
    if (!m) return null;
    return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  }
  function tlFmt(mins) {
    const h = Math.floor(mins / 60), m = mins % 60;
    const mer = h < 12 ? "AM" : "PM";
    return (h % 12 || 12) + ":" + String(m).padStart(2, "0") + " " + mer;
  }
  function tlHourLabel(h) {
    const mer = h < 12 ? "AM" : "PM";
    return (h % 12 || 12) + ":00 " + mer;
  }
  function tlStatusTone(s) {
    const k = bkKey(s);
    if (k === "pending") return "pending";
    if (k === "pending_check_in" || k === "confirmed") return "confirmed";
    if (k === "checked_in") return "checkedin";
    if (k === "active") return "active";
    return "done"; // done / completed / cancelled / rejected / no_show …
  }
  function tlMMT(offsetDays) {
    const d = new Date(Date.now() + 6.5 * 3600000 + (offsetDays || 0) * 86400000);
    return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0") + "-" + String(d.getUTCDate()).padStart(2, "0");
  }

  const TimelineGrid = {
    components: { PageHeader },
    props: { embedded: { type: Boolean, default: false } },
    emits: ["pick"],
    template: `
      <div class="pv-page psv-tl">
        <PageHeader v-if="!embedded" title="Booking Timeline" back="/more" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <div class="psv-tl-datenav">
            <button class="psv-chip" :class="{ 'psv-chip--on': isToday }" @click="goToday">📅 Today</button>
            <button class="psv-chip" :class="{ 'psv-chip--on': isTomorrow }" @click="goTomorrow">➡ Tomorrow</button>
            <div class="psv-tl-datebar">
              <button class="psv-tl-arrow" aria-label="Previous day" @click="step(-1)">‹</button>
              <span class="psv-tl-datelbl">{{ dayLabel }}</span>
              <button class="psv-tl-arrow" aria-label="Next day" @click="step(1)">›</button>
            </div>
          </div>

          <div class="psv-tl-legend">
            <span class="psv-tl-lg"><i class="psv-tl-sw is-pending"></i>Pending</span>
            <span class="psv-tl-lg"><i class="psv-tl-sw is-confirmed"></i>Confirmed</span>
            <span class="psv-tl-lg"><i class="psv-tl-sw is-checkedin"></i>Checked-in</span>
            <span class="psv-tl-lg"><i class="psv-tl-sw is-active"></i>Active</span>
            <span class="psv-tl-lg"><i class="psv-tl-sw is-done"></i>Done / cancelled</span>
          </div>

          <div v-if="loading" class="psv-tl-pad">
            <div class="psv-skeleton" style="height:150px"></div>
            <div class="psv-skeleton" style="height:150px;margin-top:12px"></div>
          </div>
          <div v-else-if="err" class="psv-empty">
            <div style="font-size:30px">📡</div>
            <div style="margin-top:8px">{{ err }}</div>
            <button class="psv-btn psv-btn--ghost" style="margin-top:14px" @click="load">Retry</button>
          </div>
          <div v-else-if="!bookings.length" class="psv-empty">
            <div style="font-size:34px">🗓</div>
            <div style="margin-top:8px">No bookings on {{ dayLabel }}</div>
            <div class="psv-tl-empty__sub">{{ consoles.length }} consoles · {{ openHourLabel }}–{{ closeHourLabel }}</div>
          </div>
          <div v-else class="psv-tl-wrap">
            <div class="psv-tl-scroll" ref="scroller">
              <div class="psv-tl-inner">
                <div v-if="showNow" class="psv-tl-line" :style="{ transform: 'translateX(' + nowLinePx + 'px)' }"></div>
                <div class="psv-tl-corner">Console</div>
                <div class="psv-tl-head-hours">
                  <div class="psv-tl-hour" v-for="h in gridHours" :key="h">{{ hourLabel(h) }}</div>
                  <div class="psv-tl-hour psv-tl-hour--end">{{ closeHourLabel }}</div>
                  <div v-if="showNow" class="psv-tl-now" :style="{ left: nowPx + 'px' }"><span class="psv-tl-now__tag">NOW</span></div>
                </div>
                <template v-for="r in tlRows" :key="r.id">
                  <div class="psv-tl-label">
                    <span class="psv-tl-label__id">{{ r.id }}</span>
                    <span v-if="r.utilPct > 0" class="psv-tl-label__util" :class="r.utilTone">{{ r.utilPct }}%</span>
                  </div>
                  <div class="psv-tl-track">
                    <button v-for="b in r.blocks" :key="b.id" class="psv-tl-block" :class="['is-' + tone(b.status), { 'is-narrow': isNarrow(b) }]" :style="blockStyle(b)" @click="pick(b)">
                      <span class="psv-tl-block__name">{{ b.customerName || b.memberId || 'Guest' }}</span>
                      <span class="psv-tl-block__time">{{ blockTime(b) }}</span>
                    </button>
                  </div>
                </template>
              </div>
            </div>
            <div class="psv-tl-foot">{{ bookings.length }} booking(s) · {{ dayLabel }}</div>
          </div>
        </van-pull-refresh>
      </div>
    `,
    setup(props, ctx) {
      const emit = ctx.emit;
      const day = ref(PSV.todayMMT());
      const bookings = ref([]);
      const consoles = ref([]);
      const loading = ref(true);
      const refreshing = ref(false);
      const err = ref("");
      const scroller = ref(null);
      // ── Timeline visibility (owner spec 2026-09-20) ────────────────────────
      // Show ONLY:  • live sessions (active / checked_in)
      //             • confirmed bookings that are still UPCOMING (start >= now MMT)
      //             • forfeited-deposit bookings, drawn as a MAX 1 HOUR block
      // Hide: done / cancelled / rejected / no-show / past-confirmed / pending.
      const TL_LIVE = ["active", "checked_in"];
      const TL_UPCOMING = ["confirmed", "pending_check_in"];
      const clock = ref(Date.now());
      const gridHours = TL_GRID_HOURS;
      const trackW = (TL_CLOSE_HOUR - TL_OPEN_HOUR + 1) * TL_HOUR_W;
      const TL_LABEL_W = 120; // sticky console-column width (matches grid-template-columns)
      const openHourLabel = tlHourLabel(TL_OPEN_HOUR);
      const closeHourLabel = tlHourLabel(TL_CLOSE_HOUR);
      let fetchId = 0;
      let jumped = false;
      let tick = null;
      const _get = (p, ttl) => (PSV.apiCached ? PSV.apiCached(p, {}, ttl == null ? 15000 : ttl) : PSV.api(p));

      const isToday = computed(() => day.value === PSV.todayMMT());
      const isTomorrow = computed(() => day.value === tlMMT(1));
      const dayLabel = computed(() => {
        const p = day.value.split('-');
        const d = new Date(Date.UTC(Number(p[0]), Number(p[1]) - 1, Number(p[2])));
        const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
        return wd + ", " + p[2] + "/" + p[1] + "/" + p[0];
      });
      const nowPx = computed(() => {
        const d = new Date(clock.value + 6.5 * 3600000);
        const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
        return ((mins - TL_OPEN_HOUR * 60) / 60) * TL_HOUR_W;
      });
      const showNow = computed(() => isToday.value && nowPx.value >= 0 && nowPx.value <= trackW);
      // x of the single overlay now-line inside .psv-tl-inner (label column + hour offset)
      const nowLinePx = computed(() => TL_LABEL_W + nowPx.value);
      // WAVE 6 smoothness — per-console row metadata computed ONCE per data change
      // (Map keyed by console id). No O(rows x bookings) filtering during the 15s clock tick.
      const tlMeta = computed(() => {
        const m = new Map();
        for (const c of consoles.value) {
          const blocks = blocksOf(c.id);
          const utilPct = utilOf(c.id);
          m.set(c.id, { blocks: blocks, utilPct: utilPct, utilTone: utilPct > 70 ? "is-hi" : (utilPct > 40 ? "is-mid" : "is-lo") });
        }
        return m;
      });
      const tlRows = computed(() => consoles.value.map((c) => Object.assign({ id: c.id }, tlMeta.value.get(c.id))));

      function hourPx(h) { return (h - TL_OPEN_HOUR) * TL_HOUR_W; }
      function hourLabel(h) { return tlHourLabel(h); }
      function tone(s) { return tlStatusTone(s); }
      function cidOf(b) { return String(b.consoleId || b.console_id || b.consoleType || "").trim(); }
      function blocksOf(cid) {
        const key = String(cid).trim();
        return bookings.value.filter((b) => cidOf(b) === key && tlVisible(b));
      }
      function tlIsForfeit(b) {
        return String((b && (b.deposit_status || b.depositStatus)) || "").toLowerCase().trim() === "forfeited";
      }
      function tlNowMin() {
        const d = new Date(clock.value + 6.5 * 3600000);
        return d.getUTCHours() * 60 + d.getUTCMinutes();
      }
      function tlVisible(b) {
        const k = bkKey(b.status);
        if (TL_LIVE.indexOf(k) >= 0) return true;                 // live session
        if (tlIsForfeit(b)) return true;                          // forfeit (capped to 1 h in minutesOf)
        if (TL_UPCOMING.indexOf(k) >= 0) {
          // Confirmed / pending_check_in (owner 2026-09-20 11:43: "confirmed upcoming bookings မပြဘူး"):
          // keep the booking visible until its WINDOW ENDS, not merely until its start time — a
          // confirmed 18:00-19:00 booking must still show at 18:12 (staff may not have checked in yet).
          const r = minutesOf(b);
          if (!r) { const st = tlMin(b.timeSlot || b.startTime || b.start_time); return st != null && st >= tlNowMin(); }
          return r.end >= tlNowMin();
        }
        return false;
      }
      function minutesOf(b) {
        const st = tlMin(b.timeSlot || b.startTime || b.start_time || b.time_slot || b.planned_start);
        if (st == null) return null;
        const dur = Number(b.durationMins || b.duration_mins || b.planned_duration_mins) || 0;
        const et = tlMin(b.endTime || b.end_time || b.planned_end);
        let end = dur > 0 ? (st + dur) : (et != null ? et : st + 60);
        if (end <= st) end = st + 60;
        // Owner spec: a forfeited booking shows as a 1-hour block only (it never played).
        if (tlIsForfeit(b) && end > st + 60) end = st + 60;
        return { st: st, end: end };
      }
      function blockStyle(b) {
        const r = minutesOf(b);
        if (!r) return { left: "0px", width: "34px" };
        const left = ((r.st - TL_OPEN_HOUR * 60) / 60) * TL_HOUR_W;
        const w = Math.max(((r.end - r.st) / 60) * TL_HOUR_W, 34);
        return { left: Math.max(left, 0) + "px", width: w + "px" };
      }
      function isNarrow(b) {
        const r = minutesOf(b);
        if (!r) return true;
        return ((r.end - r.st) / 60) * TL_HOUR_W < 120;
      }
      function blockTime(b) {
        const r = minutesOf(b);
        if (!r) return "—";
        if (isNarrow(b)) return tlFmt(r.st);
        const openEnd = !(Number(b.durationMins || b.duration_mins || b.planned_duration_mins) > 0) && tlMin(b.endTime || b.end_time || b.planned_end) == null;
        return tlFmt(r.st) + "–" + (openEnd ? "Open" : tlFmt(r.end));
      }
      function range(b) {
        const r = minutesOf(b);
        if (!r) return "—";
        const openEnd = !(Number(b.durationMins || b.duration_mins || b.planned_duration_mins) > 0) && tlMin(b.endTime || b.end_time || b.planned_end) == null;
        return tlFmt(r.st) + "–" + (openEnd ? "Open" : tlFmt(r.end));
      }
      function utilOf(cid) {
        const list = blocksOf(cid).filter((b) => TL_DEAD.indexOf(bkKey(b.status)) < 0);
        const total = (TL_CLOSE_HOUR - TL_OPEN_HOUR) * 60;
        let used = 0;
        for (const b of list) {
          const r = minutesOf(b);
          if (!r) continue;
          used += Math.max(Math.min(r.end, TL_CLOSE_HOUR * 60) - Math.max(r.st, TL_OPEN_HOUR * 60), 0);
        }
        return Math.round((used / total) * 100);
      }
      function utilTone(cid) { const u = utilOf(cid); return u > 70 ? "is-hi" : (u > 40 ? "is-mid" : "is-lo"); }
      function pick(b) { haptic("light"); if (props.embedded) emit("pick", b); else openBooking(b.id); }
      function goToday() { haptic("light"); day.value = PSV.todayMMT(); jumped = false; load(); }
      function goTomorrow() { haptic("light"); day.value = tlMMT(1); jumped = false; load(); }
      function step(d) {
        haptic("light");
        const p = day.value.split("-").map(Number);
        const dt = new Date(Date.UTC(p[0], p[1] - 1, p[2] + d));
        day.value = dt.toISOString().slice(0, 10);
        jumped = false;
        load();
      }
      function scrollToNow() {
        if (jumped) return;                 // user already scrolled / already handled this day
        const el = scroller.value;
        if (!el) return;
        jumped = true;
        const view = el.clientWidth;
        const lineX = showNow.value ? nowLinePx.value : 0;
        const target = showNow.value ? Math.max(0, lineX - view * 0.6) : 0;
        // Only move when the now-line is NOT already comfortably in view.
        const cur = Math.max(0, el.scrollLeft);
        const comfortable = showNow.value && lineX >= cur + view * 0.15 && lineX <= cur + view * 0.85;
        if (comfortable) return;
        // Fire after paint (double rAF) so the smooth scroll starts on a settled frame.
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            const e2 = scroller.value;
            if (!e2) return;
            let ok = false;
            try {
              ok = typeof e2.scrollTo === "function" && ("scrollBehavior" in document.documentElement.style);
              if (ok) e2.scrollTo({ left: target, behavior: "smooth" });
            } catch (_e) { ok = false; }
            if (!ok) { try { e2.scrollLeft = target; } catch (_e2) {} }
          });
        });
      }
      // Keep the grid the ONLY vertical scroller: bound its height to the space left
      // between its own top and the fixed tabbar (+ the footer line under it), so the
      // page itself never scrolls and the last console row is never cut off.
      // Fit-to-height (Osmo: "C01 to C10 ကို 1 page ထဲ ပြနိုင်မှ အဆင်ပြေမယ်"): measure the
      // space left between the grid's own top and the fixed tab bar and shrink the
      // ROWS so every console is visible at once — no inner vertical scrolling.
      // Only when even the minimum readable row can't fit do we fall back to scrolling.
      function sizeScroller() {
        const el = scroller.value;
        if (!el) return;
        psvSyncChrome();                 // refresh --psv-tabbar-h before measuring
        const r = el.getBoundingClientRect();
        const top = r.top + (window.scrollY || 0);   // document-space (scroll-safe)
        const vpH = document.documentElement.clientHeight || window.innerHeight;
        const tb = document.querySelector(".van-tabbar");
        const tabH = tb ? tb.getBoundingClientRect().height : 0;
        const foot = el.parentElement ? el.parentElement.querySelector(".psv-tl-foot") : null;
        const footH = foot ? foot.getBoundingClientRect().height : 0;
        // The app shell already RESERVES `--psv-tabbar-h` (+ Android system-nav) as #app
        // bottom padding. If we only cleared the bar itself, that reserved padding would
        // push the document past the viewport → a stray page scroll. Reserve the LARGER
        // of (bar height, shell padding) so the page fits AND no row hides behind the bar.
        const appEl = document.getElementById("app");
        const appPad = appEl ? (parseFloat(getComputedStyle(appEl).paddingBottom) || 0) : 0;
        const reserve = Math.max(tabH, appPad);
        let avail = Math.floor(vpH - top - reserve - footH - 16);
        const rows = Math.max(1, (tlRows.value || []).length);
        const head = el.querySelector(".psv-tl-head-hours");
        const headH = head ? Math.round(head.getBoundingClientRect().height) || 34 : 34;
        const MINH = 24, MAXH = 56;
        let ideal = Math.floor((avail - headH) / rows);
        // Short screens + the native update banner: hide the legend and re-measure once so the
        // 10 console rows still fit one page (owner 2026-09-20 11:53).
        const wrap = el.closest(".psv-tl-wrap") || el.parentElement;
        if (wrap) {
          const tight = ideal < MINH;
          if (wrap.classList.contains("is-tight") !== tight) {
            wrap.classList.toggle("is-tight", tight);
            const r2 = el.getBoundingClientRect();
            const top2 = r2.top + (window.scrollY || 0);
            avail = Math.floor(vpH - top2 - reserve - footH - 16);
            ideal = Math.floor((avail - headH) / rows);
          }
        }
        const fits = ideal >= MINH;
        const rowH = Math.max(MINH, Math.min(MAXH, ideal || MAXH));
        el.style.setProperty("--psv-tl-rowh", rowH + "px");
        el.classList.toggle("is-compact", rowH < 40);
        el.classList.toggle("is-fit", fits);
        if (avail > 0) {
          // Fits → size the grid exactly to its content so there is no inner scroll.
          // +14 = the scroller's own padding/border (8px native padding + 2px borders + slack).
          el.style.maxHeight = (fits ? headH + rows * rowH + 14 : Math.max(220, avail)) + "px";
        } else {
          el.style.maxHeight = "";
        }
      }
      async function load() {
        const _id = ++fetchId;
        loading.value = true; err.value = "";
        try {
          // Two cached calls only (bookings for the day + console roster).
          const [bk, cs] = await Promise.all([
            _get("/api/bookings?date=" + encodeURIComponent(day.value), 15000),
            _get("/api/fetch_console_status", 15000),
          ]);
          if (_id !== fetchId) return;   // stale-response guard
          const bd = PSV.unwrap(bk);
          const bArr = Array.isArray(bd) ? bd : (bd && bd.bookings) || [];
          bookings.value = bArr.filter((b) => String(b.date || b.booking_date || "").slice(0, 10) === day.value);
          const cd = PSV.unwrap(cs);
          const cArr = Array.isArray(cd) ? cd : (cd && cd.consoles) || [];
          const rows = cArr
            .map((c) => ({ id: String(c.console_id || c.id || c.consoleId || "").trim() }))
            .filter((x) => x.id);
          rows.sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }));
          if (rows.length) consoles.value = rows;
          else if (!consoles.value.length) {
            const def = [];
            for (let i = 1; i <= 10; i++) def.push({ id: "C-" + String(i).padStart(2, "0") });
            consoles.value = def;
          }
        } catch (e) {
          if (_id !== fetchId) return;
          err.value = (e && e.message) ? e.message : "Failed to load timeline";
          bookings.value = [];
        } finally {
          if (_id === fetchId) {
            loading.value = false;
            refreshing.value = false;
            nextTick(() => { sizeScroller(); scrollToNow(); requestAnimationFrame(() => { sizeScroller(); psvSyncChrome(); }); });
          }
        }
      }
      const onResize = () => { psvSyncChrome(); sizeScroller(); };
      onMounted(() => {
        load();
        // 15s tick → the single now-line glides (transition: transform 15s linear)
        // instead of jumping once a minute.
        tick = setInterval(() => { clock.value = Date.now(); }, 15000);
        window.addEventListener("psv:retry", load);
        // Native update banner appears/disappears at any time (fixed bottom bar injected by the
        // bridge) -> re-measure + re-fit so the last row never hides under it (2026-09-20 11:53).
        window.addEventListener("psv:chrome", onResize);
        window.addEventListener("resize", onResize);
        window.addEventListener("orientationchange", onResize);
      });
      onUnmounted(() => {
        if (tick) clearInterval(tick);
        window.removeEventListener("psv:retry", load);
        window.removeEventListener("psv:chrome", onResize);
        window.removeEventListener("resize", onResize);
        window.removeEventListener("orientationchange", onResize);
      });
      return { embedded: props.embedded, refreshing, day, bookings, consoles, loading, err, scroller, gridHours, tlRows, nowLinePx,
        isToday, isTomorrow, dayLabel, nowPx, showNow, openHourLabel, closeHourLabel,
        hourPx, hourLabel, tone, blockStyle, isNarrow, blockTime, range, pick, goToday, goTomorrow, step, load };
    },
  };

  // ── VIEW: Bookings (WAVE 1 — full staff ops + deposits) ─
  const Bookings = {
    components: { PageHeader, TimelineGrid },
    template: `
      <div class="pv-page psv-bk">
        <PageHeader title="Bookings" />
        <div class="psv-seg" role="tablist">
          <button class="psv-seg__b" :class="{ 'is-on': view === 'list' }" role="tab" @click="setView('list')">☰ List</button>
          <button class="psv-seg__b" :class="{ 'is-on': view === 'timeline' }" role="tab" @click="setView('timeline')">🕒 Timeline</button>
        </div>
        <template v-if="view === 'list'">
        <div class="psv-bk-chips">
          <button v-for="c in chips" :key="c.id" class="psv-chip" :class="{ 'psv-chip--on': chip === c.id }" @click="setChip(c.id)">{{ c.icon }} {{ c.label }}</button>
        </div>
        <div class="psv-bk-hint">{{ chipHint }}</div>

        <van-pull-refresh v-model="refreshing" @refresh="load">
          <div v-if="loading" class="psv-pad">
            <div class="psv-skeleton psv-sk-card" v-for="n in 3" :key="n"></div>
          </div>
          <div v-else-if="!rows.length" class="psv-empty">
            <div style="font-size:34px">📭</div>
            <div style="margin-top:8px">{{ emptyText }}</div>
            <button class="psv-btn psv-btn--ghost" style="margin-top:14px" @click="goNew">➕ New Booking</button>
          </div>
          <div v-else class="psv-bk-list">
            <div v-for="b in rows" :key="b.id" class="psv-bk-card" :class="'is-' + bkStatus(b.status).tone">
              <div class="psv-bk-card__top" @click="openDetail(b)">
                <div class="psv-bk-card__who">
                  <div class="psv-bk-card__name">{{ b.customerName || b.member_id || 'Guest' }}</div>
                  <div class="psv-bk-card__sub">BK#{{ b.id }} · {{ b.console_id || b.consoleType || '-' }}</div>
                </div>
                <span class="psv-pill" :class="'psv-pill--' + bkStatus(b.status).tone">{{ bkStatus(b.status).en }}</span>
              </div>
              <div class="psv-bk-card__meta">
                <span>📅 {{ b.date }}</span>
                <span>⏰ {{ fmt12h(b.timeSlot || b.startTime) }}<template v-if="b.durationMins"> · {{ b.durationMins }}m</template></span>
                <span v-if="b.gameName && b.gameName !== 'Any'">🎮 {{ b.gameName }}</span>
              </div>
              <div v-if="b.deposit_status && b.deposit_status !== 'none'" class="psv-bk-card__dep">
                <span class="psv-pill psv-pill--gold">💰 {{ depositText(b) }}</span>
                <span v-if="depRef(b)" class="psv-bk-card__ref">Ref: {{ depRef(b) }}</span>
              </div>
              <div class="psv-bk-actions">
                <button v-for="a in cardActions(b)" :key="a.id" class="psv-abtn" :class="'psv-abtn--' + a.tone" :disabled="busy === b.id" @click.stop="runAction(a, b)">
                  <span class="psv-abtn__ico">{{ a.icon }}</span>{{ a.en }}
                </button>
                <button class="psv-abtn psv-abtn--ghost" :disabled="busy === b.id" @click.stop="openDetail(b)">⋯ Details</button>
              </div>
            </div>
          </div>
        </van-pull-refresh>
        </template>
        <TimelineGrid v-else embedded @pick="onPickTimeline" />

        <!-- Booking detail + full action sheet -->
        <van-popup v-model:show="detailOpen" position="bottom" round class="psv-sheet" :style="{ maxHeight: '90vh' }">
          <div class="psv-sheet__handle"></div>
          <div class="psv-sheet__body" v-if="detail">
            <div class="psv-bk-detail__head">
              <div>
                <div class="psv-bk-detail__name">{{ detail.customerName || detail.member_id || 'Guest' }}</div>
                <div class="psv-bk-detail__id">BK#{{ detail.id }}</div>
              </div>
              <span class="psv-pill" :class="'psv-pill--' + bkStatus(detail.status).tone">{{ bkStatus(detail.status).en }}</span>
            </div>
            <div class="psv-card psv-rows">
              <div class="psv-row"><span>Console</span><b>{{ detail.console_id || detail.consoleType || '-' }}</b></div>
              <div class="psv-row"><span>Date / Time</span><b>{{ detail.date }} · {{ fmt12h(detail.timeSlot || detail.startTime) }}</b></div>
              <div class="psv-row"><span>Duration</span><b>{{ detail.durationMins || '-' }} min</b></div>
              <div class="psv-row" v-if="detail.gameName"><span>Game</span><b>{{ detail.gameName }}</b></div>
              <div class="psv-row" v-if="detail.phone"><span>Phone</span><b>{{ detail.phone }}</b></div>
              <div class="psv-row" v-if="detail.member_id"><span>Member</span><b>{{ detail.member_id }}</b></div>
              <div class="psv-row" v-if="detail.deposit_status && detail.deposit_status !== 'none'"><span>Deposit</span><b>{{ depositText(detail) }}</b></div>
            </div>
            <div v-if="detail.notes" class="psv-card">
              <div class="psv-field-label">Notes</div>
              <div style="font-size:13px;line-height:1.6">{{ detail.notes }}</div>
            </div>
            <div class="psv-section-title">Actions</div>
            <div class="psv-bk-detail__grid">
              <button v-for="a in detailActions(detail)" :key="a.id" class="psv-abtn psv-abtn--block" :class="'psv-abtn--' + a.tone" :disabled="busy === detail.id" @click="runAction(a, detail)">
                <span class="psv-abtn__ico">{{ a.icon }}</span>{{ a.en }}
              </button>
              <div v-if="!detailActions(detail).length" class="psv-empty" style="padding:16px">No actions available</div>
            </div>
          </div>
        </van-popup>

        <!-- Reject / cancel reason picker -->
        <van-popup v-model:show="reasonOpen" position="bottom" round class="psv-sheet">
          <div class="psv-sheet__handle"></div>
          <div class="psv-sheet__body">
            <div class="psv-sheet-title">{{ reasonMode === 'reject' ? '❌ Reason for rejecting' : '⛔ Reason for cancelling' }}</div>
            <div class="psv-reason" v-for="r in reasonList" :key="r.value" :class="{ 'is-on': reasonPick === r.value }" @click="reasonPick = r.value">
              <div class="psv-reason__radio"></div>
              <div class="psv-reason__main"><div class="psv-reason__t">{{ r.text }}</div><div class="psv-reason__s">{{ r.note }}</div></div>
            </div>
            <van-field v-if="reasonPick === 'other'" v-model="reasonNote" rows="2" autosize type="textarea" placeholder="Type a reason…" />
            <div class="psv-btn-row" style="margin-top:12px">
              <button class="psv-btn psv-btn--ghost psv-btn--block" @click="reasonOpen = false">Cancel</button>
              <button class="psv-btn psv-btn--danger psv-btn--block" :disabled="!reasonPick" @click="confirmReason">Confirm</button>
            </div>
          </div>
        </van-popup>

        <!-- Deposit proof -->
        <van-popup v-model:show="proofOpen" position="bottom" round class="psv-sheet">
          <div class="psv-sheet__handle"></div>
          <div class="psv-sheet__body">
            <div class="psv-sheet-title">👁 Deposit Proof — BK#{{ proof.booking_id }}</div>
            <div v-if="proof.restricted" class="psv-card"><div class="psv-field-label">Restricted</div><div style="font-size:13px;line-height:1.6">Proof is not available for your role. Ask an owner / admin to view it.</div></div>
            <template v-else-if="proof.isImg">
              <div v-if="proof.imgLoading" class="psv-proof-spin"><span class="psv-proof-spin__c"></span><span class="psv-proof-spin__t">SS ဖွင့်နေသည်…</span></div>
              <img v-else-if="proof.imgUrl" :src="proof.imgUrl" class="psv-proof-img" @click="maybeProofFull" />
              <div v-else class="psv-card">
                <div class="psv-field-label">SS ဖွင့်လို့ မရပါ — ပြန်စမ်းပါ</div>
                <div v-if="proof.imgError" style="font-size:12px;color:var(--pv-muted);margin-top:4px;word-break:break-all">{{ proof.imgError }}</div>
                <button class="psv-btn psv-btn--ghost psv-btn--block" style="margin-top:8px" @click="loadProof">ပြန်စမ်းပါ</button>
                <div style="font-size:12px;color:var(--pv-muted);margin-top:10px;word-break:break-all">Ref: {{ proof.ref }}</div>
              </div>
            </template>
            <div v-else class="psv-card"><div class="psv-field-label">Ref / details</div><div style="font-size:14px;word-break:break-all">{{ proof.ref }}</div></div>
            <button class="psv-btn psv-btn--primary psv-btn--block" style="margin-top:12px" @click="proofOpen = false">Close</button>
          </div>
        </van-popup>

        <!-- Deposit proof — full-screen viewer (native shell only) -->
        <div v-if="proofFull" class="psv-proof-full" @click="proofFull = false">
          <img :src="proof.imgUrl" class="psv-proof-full__img" />
        </div>

        <!-- New booking (kept from previous pass) -->
        <van-popup v-model:show="newOpen" position="bottom" round class="psv-sheet" :style="{ height: '92vh' }">
          <div class="psv-sheet__handle"></div>
          <div style="overflow:auto;height:calc(92vh - 20px)"><BookingForm @saved="onSaved" /></div>
        </van-popup>
      </div>
    `,
    setup() {
      const CHIPS = [
        { id: "today", icon: "📅", label: "Today", hint: "All bookings for today (cancelled & no-shows excluded)" },
        { id: "open", icon: "🔓", label: "Open", hint: "Pending · Confirmed · Checked-in · Active" },
        { id: "pending", icon: "⏳", label: "Pending", hint: "Waiting for approve / reject" },
        { id: "confirmed", icon: "✅", label: "Confirmed", hint: "Confirmed — check in when the customer arrives" },
        { id: "checked_in", icon: "🎟", label: "Checked-in", hint: "Arrived — start the console session" },
        { id: "active", icon: "▶️", label: "Active", hint: "Session in progress" },
        { id: "deposit", icon: "💰", label: "Deposit", hint: "Verify / redeem a deposit" },
        { id: "cancelled", icon: "⛔", label: "Cancelled", hint: "Cancelled, rejected & no-show bookings" },
      ];
      const chip = ref("open");
      const view = ref("list");
      const rows = ref([]);
      const loading = ref(true);
      const refreshing = ref(false);
      const busy = ref(null);
      const detail = ref(null);
      const detailOpen = ref(false);
      const newOpen = ref(false);
      const reasonOpen = ref(false);
      const reasonMode = ref("reject");
      const reasonPick = ref("");
      const reasonNote = ref("");
      const reasonTarget = ref(null);
      const proofOpen = ref(false);
      const proofFull = ref(false);
      const proof = reactive({ ref: "", isImg: false, booking_id: "", restricted: false, imgUrl: "", imgKey: "", imgLoading: false, imgError: "" });

      const chipMeta = computed(() => CHIPS.find((c) => c.id === chip.value) || CHIPS[1]);
      const chipHint = computed(() => chipMeta.value.hint);
      const emptyText = computed(() => (chip.value === "deposit" ? "No deposits to verify" : "No bookings"));
      const reasonList = computed(() => (reasonMode.value === "reject" ? BK_REJECT_REASONS : BK_CANCEL_REASONS));

      function hasDepositPending(b) { return ["paid", "pending"].includes(bkKey(b.deposit_status)); }
      function isVerified(b) { return bkKey(b.deposit_status) === "verified"; }
      function depRef(b) { return b.deposit_ref || b.depositRef || b.deposit_slip || ""; }
      function depositText(b) {
        const s = bkKey(b.deposit_status);
        const amount = b.deposit_amount ? Number(b.deposit_amount).toLocaleString() : "";
        const method = (b.deposit_method || "").toUpperCase();
        const lbl = amount ? amount + (method ? " " + method : "") : "-";
        const map = { paid: "Received (verify) " + lbl, pending: "Pending " + lbl, verified: "Verified " + lbl, redeemed: "Redeemed " + lbl, forfeited: "Forfeited " + lbl, refunded: "Refunded " + lbl };
        return map[s] || ("Deposit " + lbl);
      }
      function cardActions(b) {
        const k = bkKey(b.status);
        const out = [];
        if (k === "pending") {
          out.push({ id: "approve", en: "Approve", icon: "✅", tone: "ok", kind: "approve" });
          out.push({ id: "reject", en: "Reject", icon: "❌", tone: "danger", kind: "reject" });
          if (hasDepositPending(b)) out.push({ id: "verify", en: "Verify deposit", icon: "💰", tone: "gold", kind: "verify" });
          out.push({ id: "cancel", en: "Cancel", icon: "⛔", tone: "danger", kind: "cancel" });
        } else if (k === "confirmed" || k === "pending_check_in") {
          out.push({ id: "checkin", my: "Check-in", en: "Arrived", icon: "🎟", tone: "ok", kind: "checkin" });
          if (hasDepositPending(b)) out.push({ id: "verify", en: "Verify deposit", icon: "💰", tone: "gold", kind: "verify" });
          out.push({ id: "cancel", en: "Cancel", icon: "⛔", tone: "danger", kind: "cancel" });
        } else if (k === "checked_in") {
          out.push({ id: "consoles", en: "Start session", icon: "▶️", tone: "gold", kind: "consoles" });
          out.push({ id: "cancel", en: "Cancel", icon: "⛔", tone: "danger", kind: "cancel" });
        }
        return out;
      }
      function detailActions(b) {
        const out = cardActions(b);
        if (isVerified(b)) out.push({ id: "redeem", en: "Redeem at sale", icon: "↩️", tone: "gold", kind: "redeem" });
        if (depRef(b)) out.push({ id: "proof", en: "View proof", icon: "👁", tone: "ghost", kind: "proof" });
        if (bkKey(b.status) === "active") out.push({ id: "consoles", en: "View session", icon: "🖥", tone: "ghost", kind: "consoles" });
        return out;
      }

      // WAVE 4 — request de-bursting.
      // The "Open" chip used to fire FOUR parallel /api/bookings?status= calls —
      // that burst is what tripped Cloudflare's rate-limit ban. We now prefer the
      // single ?status=open endpoint; if it is missing or empty we fall back to
      // ONE ?status= call filtered client-side. Never 4 parallel status requests.
      const DEAD_ST = ["cancelled", "canceled", "rejected", "no_show", "noshow", "no-show", "expired"];
      const OPEN_ST = ["pending", "pending_check_in", "confirmed", "checked_in", "active"];
      const isDead = (b) => DEAD_ST.indexOf(bkKey(b.status)) >= 0;
      const _get = (p, ttl) => (PSV.apiCached ? PSV.apiCached(p, {}, ttl == null ? 15000 : ttl) : PSV.api(p));
      async function fetchStatus(s) { const d = PSV.unwrap(await _get("/api/bookings?status=" + encodeURIComponent(s))); return (d && d.bookings) || []; }
      async function fetchAll() { const d = PSV.unwrap(await _get("/api/bookings?status=")); return (d && d.bookings) || []; }
      async function loadOpen() {
        try {
          const arr = await fetchStatus("open");
          if (arr.length) return arr.filter((b) => !isDead(b));
          const all = await fetchAll();                 // ONE call, client-side filter
          return all.filter((b) => OPEN_ST.indexOf(bkKey(b.status)) >= 0);
        } catch (_e) {                                   // endpoint missing → fallback
          const all = await fetchAll();
          return all.filter((b) => OPEN_ST.indexOf(bkKey(b.status)) >= 0);
        }
      }
      async function load() {
        loading.value = true;
        try {
          let list = [];
          const c = chip.value;
          if (c === "today") {
            const all = await fetchAll();
            const today = PSV.todayMMT();
            list = all.filter((b) => String(b.date || b.booking_date || "").slice(0, 10) === today && !isDead(b));
          } else if (c === "open") {
            list = await loadOpen();
          } else if (c === "deposit") {
            const all = await fetchAll();
            list = all.filter((b) => hasDepositPending(b) && !isDead(b));
          } else if (c === "cancelled") {
            const all = await fetchAll();
            list = all.filter(isDead);
          } else {
            list = (await fetchStatus(c)).filter((b) => !isDead(b));
          }
          const seen = {};
          list = list.filter((b) => { if (seen[b.id]) return false; seen[b.id] = 1; return true; });
          list.sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")) || String(b.timeSlot || "").localeCompare(String(a.timeSlot || "")));
          rows.value = list;
        } catch (e) {
          rows.value = [];
          showToast("Load failed: " + e.message, "fail");
        } finally { loading.value = false; refreshing.value = false; }
      }
      function setChip(id) { haptic("light"); chip.value = id; load(); nextTick(scrollChipIntoView); }
      // Keep the active chip visible when the row is wider than the screen
      // (e.g. the new "Cancelled" chip sits at the far right).
      function scrollChipIntoView() {
        try {
          const el = document.querySelector(".psv-bk-chips .psv-chip--on");
          if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", inline: "center" });
        } catch (_e) {}
      }
      function setView(id) { haptic("light"); view.value = id; }
      function onPickTimeline(b) { haptic("light"); detail.value = b; detailOpen.value = true; }
      function goNew() { haptic("light"); newOpen.value = true; }
      function onSaved() { newOpen.value = false; chip.value = "open"; load(); }

      async function fetchOne(id) {
        try {
          const d = PSV.unwrap(await PSV.api("/api/bookings/" + encodeURIComponent(id)));
          return (d && d.booking) || null;
        } catch (_e) { return null; }
      }
      async function openById(id) {
        const found = rows.value.find((b) => String(b.id) === String(id));
        if (found) { detail.value = found; detailOpen.value = true; return; }
        const one = await fetchOne(id);
        if (one) { detail.value = one; detailOpen.value = true; }
        else showToast("BK#" + id + " not found", "fail");
      }
      function openDetail(b) { haptic("light"); detail.value = b; detailOpen.value = true; }

      async function refreshDetail() {
        if (!detailOpen.value || !detail.value) return;
        const fresh = await fetchOne(detail.value.id);
        if (fresh) detail.value = fresh;
      }
      async function afterMutation(id, patch) {
        rows.value = rows.value.map((b) => (String(b.id) === String(id) ? Object.assign({}, b, patch || {}) : b));
        if (detail.value && String(detail.value.id) === String(id)) detail.value = Object.assign({}, detail.value, patch || {});
        await load();
        await refreshDetail();
      }

      async function doApprove(b) {
        const okc = await confirmBox("Approve booking?", "Approve BK#" + b.id + " (" + (b.customerName || "Guest") + "). The customer will be notified on Telegram.", "Approve", "Cancel");
        if (!okc) return;
        busy.value = b.id;
        try {
          await PSV.api("/api/bookings/" + b.id + "/status", { method: "PATCH", headers: HJSON, body: JSON.stringify({ status: "confirmed", staffName: psvStaffName() }) });
          haptic("medium"); showToast("Approved ✓", "success");
          await afterMutation(b.id, { status: "confirmed" });
        } catch (e) { showToast("Failed: " + e.message, "fail"); }
        finally { busy.value = null; }
      }
      async function doCheckin(b) {
        const okc = await confirmBox("Check in?", "Mark BK#" + b.id + " as arrived (play time has not started yet).", "Check-in", "Cancel");
        if (!okc) return;
        busy.value = b.id;
        try {
          await PSV.api("/api/bookings/checkin", { method: "POST", headers: HJSON, body: JSON.stringify({ id: b.id, staff_name: psvStaffName() }) });
          haptic("medium"); showToast("Checked in 🎟", "success");
          await afterMutation(b.id, { status: "checked_in" });
        } catch (e) { showToast("Failed: " + e.message, "fail"); }
        finally { busy.value = null; }
      }
      async function doVerify(b) {
        const amt = Number(b.deposit_amount) || 0;
        const okc = await confirmBox("Verify deposit?", "BK#" + b.id + " · " + amt.toLocaleString() + " Ks (" + (b.deposit_method || "").toUpperCase() + "). This marks it Verified, confirms the booking and records a cash injection.", "Verified", "Cancel");
        if (!okc) return;
        busy.value = b.id;
        try {
          await PSV.api("/api/bookings/" + b.id + "/deposit/verify", { method: "PATCH", headers: HJSON, body: JSON.stringify({ verified_by: psvStaffName() }) });
          haptic("medium"); showToast("Deposit verified 💰", "success");
          await afterMutation(b.id, { deposit_status: "verified", status: "confirmed" });
        } catch (e) { showToast("Failed: " + e.message, "fail"); }
        finally { busy.value = null; }
      }
      async function doRedeem(b) {
        try {
          const d = PSV.unwrap(await PSV.api("/api/bookings/" + b.id + "/deposit/pre-redeem", { method: "POST" }));
          const amt = Number((d && d.deposit_amount) || 0);
          if (!amt) { showToast("No verified deposit", "warning"); return; }
          try { PSV.pendingDepositRedeem = { booking_id: b.id, amount: amt, method: (d && d.deposit_method) || "", ts: Date.now() }; } catch (_e) {}
          haptic("light");
          showToast("Deposit " + amt.toLocaleString() + " Ks — linked to the sale", "success");
          detailOpen.value = false;
          router.push("/sales");
        } catch (e) { showToast("Failed: " + e.message, "fail"); }
      }
      function resetProofImg() {
        if (proof.imgKey) { try { PSV.revokeBlob(proof.imgKey); } catch (_e) {} }
        proof.imgUrl = ""; proof.imgKey = ""; proof.imgLoading = false; proof.imgError = "";
        proofFull.value = false;
      }
      async function loadProof() {
        const id = proof.booking_id;
        if (!id) return;
        proof.imgLoading = true; proof.imgError = "";
        try {
          const p = "/api/bookings/" + id + "/deposit/proof";
          const url = await PSV.blobUrl(p);
          if (!proofOpen.value) { try { PSV.revokeBlob(p); } catch (_e) {} return; }
          proof.imgKey = p; proof.imgUrl = url; proof.imgError = "";
        } catch (e) {
          proof.imgUrl = ""; proof.imgError = (e && e.message) || "failed";
        } finally { proof.imgLoading = false; }
      }
      function maybeProofFull() {
        // Full-screen viewer is a native-shell affordance only (Telegram stays as-is).
        try { if (!document.documentElement.classList.contains("psv-native")) return; } catch (_e) { return; }
        if (proof.imgUrl) { proofFull.value = true; haptic("light"); }
      }
      function showProof(b) {
        const ref = depRef(b);
        // New booking objects carry can_view_proof; when false the ref is redacted.
        const canView = (b.can_view_proof == null) ? !!ref : !!b.can_view_proof;
        resetProofImg();
        proof.booking_id = b.id;
        if (!canView || !ref) { proof.ref = ""; proof.isImg = false; proof.restricted = true; proofOpen.value = true; haptic("light"); return; }
        proof.ref = ref;
        const rtype = String(b.deposit_ref_type || "").toLowerCase();
        proof.isImg = rtype === "image" || rtype === "both" || (!rtype && (/^https?:\/\//i.test(ref) || /\.(png|jpe?g|webp|gif)$/i.test(ref)));
        proof.restricted = false;
        proofOpen.value = true;
        haptic("light");
        if (proof.isImg) loadProof();
      }

      function openReason(mode, b) { reasonMode.value = mode; reasonPick.value = ""; reasonNote.value = ""; reasonTarget.value = b; reasonOpen.value = true; haptic("light"); }
      async function confirmReason() {
        const b = reasonTarget.value;
        if (!b || !reasonPick.value) return;
        const r = reasonList.value.find((x) => x.value === reasonPick.value) || { note: reasonPick.value };
        const label = reasonPick.value === "other" && reasonNote.value.trim() ? reasonNote.value.trim() : r.note;
        reasonOpen.value = false;
        const isReject = reasonMode.value === "reject";
        const conseq = isReject
          ? "This will reject BK#" + b.id + " and notify the customer. Any paid deposit may need a refund."
          : "This will cancel BK#" + b.id + " and notify the customer. A verified deposit is forfeited (no voucher).";
        const okc = await confirmBox(isReject ? "Reject booking?" : "Cancel booking?", conseq + " Reason: " + label, isReject ? "Reject" : "Cancel", "Back", true);
        if (!okc) return;
        busy.value = b.id;
        try {
          if (isReject) {
            await PSV.api("/api/bookings/" + b.id + "/status", { method: "PATCH", headers: HJSON, body: JSON.stringify({ status: "rejected", staffNote: label, staffName: psvStaffName() }) });
            showToast("Rejected ❌", "success");
            await afterMutation(b.id, { status: "rejected" });
          } else {
            await PSV.api("/api/bookings/cancel", { method: "POST", headers: HJSON, body: JSON.stringify({ id: b.id, booking_id: b.id, reason: label, staff_name: psvStaffName() }) });
            showToast("Cancelled ⛔", "success");
            await afterMutation(b.id, { status: "cancelled" });
          }
        } catch (e) { showToast("Failed: " + e.message, "fail"); }
        finally { busy.value = null; }
      }

      async function runAction(a, b) {
        if (a.kind === "approve") return doApprove(b);
        if (a.kind === "checkin") return doCheckin(b);
        if (a.kind === "verify") return doVerify(b);
        if (a.kind === "redeem") return doRedeem(b);
        if (a.kind === "proof") return showProof(b);
        if (a.kind === "reject") return openReason("reject", b);
        if (a.kind === "cancel") return openReason("cancel", b);
        if (a.kind === "consoles") { haptic("light"); detailOpen.value = false; router.push("/consoles?bind=" + encodeURIComponent(b.id)); return; }
      }

      onMounted(async () => {
        window.addEventListener("psv:retry", load);
        await load();
        nextTick(scrollChipIntoView);
        const pid = pendingBooking.value;
        if (pid) { pendingBooking.value = null; await openById(pid); }
      });
      onUnmounted(() => { window.removeEventListener("psv:retry", load); resetProofImg(); });
      watch(pendingBooking, async (pid) => { if (pid) { pendingBooking.value = null; await openById(pid); } });
      watch(proofOpen, (open) => { if (!open) resetProofImg(); });

      return { chips: CHIPS, chip, chipHint, emptyText, rows, loading, refreshing, busy, detail, detailOpen, newOpen,
        view, setView, onPickTimeline,
        reasonOpen, reasonMode, reasonPick, reasonNote, reasonList, proofOpen, proof, proofFull, loadProof, maybeProofFull,
        bkStatus, depositText, depRef, cardActions, detailActions, setChip, goNew, onSaved, load, openDetail, runAction, confirmReason, fmt12h };
    },
  };

  const BookingForm = {
    emits: ["saved"],
    components: { PageHeader },
    template: `
      <div style="padding:8px 14px 16px">
        <van-cell-group inset>
          <van-field v-model="form.customer_name" label="Guest" placeholder="Name" />
          <van-field :model-value="form.member_id || ''" label="Member" placeholder="Select member (optional)" is-link readonly @click="openMember" @clear="clearMember" clearable />
          <van-field v-model="form.console_id" label="Console" placeholder="Select console" is-link readonly @click="openConsole" />
          <van-field v-model="form.game_name" label="Game" placeholder="Select game" is-link readonly @click="openGame" />
          <van-field v-model="form.booking_date" label="Date (YYYY-MM-DD)" placeholder="2026-08-25" />
          <van-field :model-value="fmt12h(form.start_time)" label="Time" placeholder="Select time" is-link readonly @click="timePicker=true" />
          <van-field v-model.number="form.duration_mins" type="number" label="Duration (mins)" placeholder="60" />
        </van-cell-group>
        <van-cell-group inset style="margin-top:8px" title="Deposit">
          <van-cell center title="Deposit"><template #right-icon><van-switch v-model="form.add_deposit" size="22" /></template></van-cell>
          <template v-if="form.add_deposit">
            <van-field v-model.number="form.deposit_amount" type="number" label="Deposit Amount (Ks)" placeholder="0" />
            <van-field :model-value="fmtDepositMethod(form.deposit_method)" label="Payment Method" is-link readonly @click="depPicker=true" />
          </template>
        </van-cell-group>
        <van-cell-group inset style="margin-top:8px">
          <van-field v-model="form.notes" label="Notes" placeholder="(optional)" />
        </van-cell-group>
        <div style="padding:14px 16px">
          <van-button type="primary" block :loading="submitting" @click="submit">Book</van-button>
        </div>

        <van-popup v-model:show="consolePicker" position="bottom" round>
          <div style="max-height:60vh;overflow:auto;padding:12px 0 20px">
            <van-cell v-for="c in consoles" :key="c.console_id || c.id" :title="c.console_id || c.id" :label="'Status: ' + (c.status||'-')" @click="pickConsole(c)" />
          </div>
        </van-popup>
        <PSVPicker v-if="nativeMode" v-model:show="memberPicker" title="Select Member"
                   :items="memberPickItems" :model-value="form.member_id || ''" @confirm="pickMemberVal" />
        <van-popup v-else v-model:show="memberPicker" position="bottom" round>
          <div style="max-height:70vh;display:flex;flex-direction:column">
            <van-search v-model="memberQuery" placeholder="Search name / phone / ID" />
            <div style="overflow:auto;padding:4px 0 16px">
              <van-cell v-for="m in filteredMembers" :key="m.id" :title="m.name || m.id" :label="'ID ' + (m.id||'-') + ' · ' + (m.phone||'-')" @click="pickMember(m)" />
              <van-empty v-if="!filteredMembers.length" description="No members found" />
            </div>
          </div>
        </van-popup>
        <van-popup v-model:show="depPicker" position="bottom" round>
          <van-picker :columns="depColumns" @confirm="pickDep" @cancel="depPicker=false" />
        </van-popup>
        <PSVPicker v-if="nativeMode" v-model:show="gamePicker" title="Select Game"
                   :items="gamePickItems" :model-value="form.game_name || ''" @confirm="pickGameVal" />
        <van-popup v-else v-model:show="gamePicker" position="bottom" round>
          <van-picker :columns="gameColumns" title="Select Game" @confirm="pickGame" @cancel="gamePicker=false" />
        </van-popup>
        <van-popup v-model:show="timePicker" position="bottom" round>
          <van-picker :columns="timeColumns" title="Select Time" @confirm="pickTime" @cancel="timePicker=false" />
        </van-popup>
      </div>
    `,
    setup(_, { emit }) {
      const DEP_METHODS = ["kpay", "wavepay", "aya_pay"];
      const form = reactive({ customer_name: "", member_id: "", console_id: "", game_name: "", booking_date: PSV.todayMMT(), start_time: "", duration_mins: 60, add_deposit: false, deposit_amount: 0, deposit_method: "kpay", notes: "" });
      const consoles = ref([]);
      const consolePicker = ref(false);
      const depPicker = ref(false);
      const gamePicker = ref(false);
      const timePicker = ref(false);
      const memberPicker = ref(false);
      const members = ref([]);
      const memberQuery = ref("");
      const submitting = ref(false);
      const gamesList = ref([]);
      const depColumns = DEP_METHODS.map((p) => ({ text: p.toUpperCase(), value: p }));
      function fmtDepositMethod(m) { if (!m) return ""; const s = String(m); return s === "aya_pay" ? "AYA PAY" : s.toUpperCase(); }
      const gameColumns = computed(() => gamesList.value.map((g) => ({ text: g.game_title || g.title || g.name, value: g.game_title || g.title || g.name })));
      // WAVE 5b — premium picker item lists (same members/games arrays; markup only).
      const memberPickItems = computed(() => members.value.map((m) => ({
        value: m.id || m.member_id,
        label: m.name || m.id,
        sub: "ID " + (m.id || "-") + (m.phone ? " · " + m.phone : ""),
        meta: (m.wallet_balance != null || m.liability_mins != null) ? ("⏱ " + fKs(m.wallet_balance != null ? m.wallet_balance : (m.liability_mins != null ? m.liability_mins : 0)) + " min") : "",
        avatar: String(m.name || m.id || "?").trim().slice(0, 2).toUpperCase(),
      })));
      const gamePickItems = computed(() => gamesList.value.map((g) => ({
        value: g.game_title || g.title || g.name,
        label: g.game_title || g.title || g.name,
        sub: [g.genre, g.solo_multi].filter(Boolean).join(" · "),
        meta: g.final_status || "",
        icon: "🎮",
      })));
      const TIME_HOURS = ["1","2","3","4","5","6","7","8","9","10","11","12"];
      const TIME_MINS = ["00","05","10","15","20","25","30","35","40","45","50","55"];
      const timeColumns = [
        { values: TIME_HOURS },
        { values: TIME_MINS },
        { values: ["AM", "PM"] },
      ];
      const filteredMembers = computed(() => {
        const q = (memberQuery.value || "").trim().toLowerCase();
        if (!q) return members.value;
        return members.value.filter((m) => ((m.name || "") + " " + (m.id || "") + " " + (m.phone || "")).toLowerCase().includes(q));
      });
      async function loadMembers() {
        if (members.value.length) return;
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_members?limit=300")); members.value = (d && d.members) || []; } catch (_e) { members.value = []; }
      }
      function openMember() { if (!members.value.length) loadMembers(); memberPicker.value = true; haptic("light"); }
      function pickMember(m) { form.member_id = m.id || m.member_id; if (!form.customer_name && m.name) form.customer_name = m.name; memberPicker.value = false; }
      function pickMemberVal(val) {
        const it = memberPickItems.value.find((x) => String(x.value) === String(val));
        form.member_id = String(val == null ? "" : val);
        if (!form.customer_name && it) form.customer_name = it.label;
        memberPicker.value = false;
      }
      function pickGameVal(val) { form.game_name = String(val == null ? "" : val); gamePicker.value = false; }
      function clearMember() { form.member_id = ""; }
      async function loadConsoles() {
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_console_status")); consoles.value = (d && d.consoles) || []; } catch (_e) {}
      }
      onMounted(loadConsoles);
      function openConsole() { if (!consoles.value.length) loadConsoles(); consolePicker.value = true; haptic("light"); }
      function pickConsole(c) { form.console_id = c.console_id || c.id; consolePicker.value = false; }
      function pickDep({ selectedOptions }) { form.deposit_method = selectedOptions[0].value; depPicker.value = false; }
      async function loadGames() {
        if (gamesList.value.length) return;
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_games")); gamesList.value = Array.isArray(d) ? d : (d && d.games) || []; } catch (_e) { gamesList.value = []; }
      }
      function openGame() { if (!gamesList.value.length) loadGames(); gamePicker.value = true; haptic("light"); }
      function pickGame({ selectedOptions }) { form.game_name = selectedOptions[0].value; gamePicker.value = false; }
      function pickTime({ selectedOptions }) {
        const h12 = parseInt(selectedOptions[0].value, 10);
        const mm = selectedOptions[1].value;
        const mer = selectedOptions[2].value;
        let hh = h12 % 12;
        if (mer === "PM") hh += 12;
        form.start_time = String(hh).padStart(2, "0") + ":" + mm;
        timePicker.value = false;
      }
      async function submit() {
        submitting.value = true; haptic("medium");
        try {
          if (form.add_deposit && !form.deposit_method) { showToast("Please select a payment method", "warning"); return; }
          if (form.add_deposit && (!form.deposit_amount || Number(form.deposit_amount) <= 0)) { showToast("Please enter a valid deposit amount", "warning"); return; }
          const body = { customer_name: form.customer_name, member_id: form.member_id, console_id: form.console_id, game_name: form.game_name, booking_date: form.booking_date, start_time: form.start_time, duration_mins: form.duration_mins, notes: form.notes };
          if (form.add_deposit) { body.add_deposit = true; body.deposit_amount = form.deposit_amount; body.deposit_method = form.deposit_method; }
          const res = PSV.unwrap(await PSV.api("/api/bookings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
          showToast("Booking saved ✓" + (res && res.booking_id ? " BK#" + res.booking_id : ""), "success");
          form.customer_name = ""; form.member_id = ""; form.console_id = ""; form.game_name = ""; form.start_time = ""; form.duration_mins = 60; form.add_deposit = false; form.deposit_amount = 0; form.notes = "";
          emit("saved");
        } catch (e) { showToast("Error: " + e.message, "fail"); }
        finally { submitting.value = false; }
      }
      return { form, consoles, consolePicker, depPicker, gamePicker, timePicker, memberPicker, members, memberQuery, filteredMembers, memberPickItems, gamePickItems, pickMemberVal, pickGameVal, nativeMode: NATIVE, submitting, depColumns, gameColumns, timeColumns, fmt12h, fmtDepositMethod, openConsole, pickConsole, openMember, pickMember, clearMember, pickDep, openGame, pickGame, pickTime, submit };
    },
  };

  // ── VIEW: More — category-organized staff menu (WAVE 1) ──
  // App mode: English-only, category sections, roadmap items greyed "Soon".
  // Telegram mode: unchanged legacy grid (MORE_ITEMS) — no visual regression.
  const MORE_ITEMS = [
    { path: "/consoles", icon: "🕹️", label: "Consoles" },
    { path: "/food-orders", icon: "🍔", label: "Food Orders" },
    { path: "/inventory", icon: "📦", label: "Inventory" },
    { path: "/reports", icon: "📊", label: "Reports" },
    { path: "/attendance", icon: "🕐", label: "Attendance" },
    { path: "/opex", icon: "💸", label: "OPEX" },
    { path: "/staff", icon: "👥", label: "Staff" },
    { path: "/games", icon: "🎮", label: "Games" },
    { path: "/food", icon: "🍜", label: "Food" },
    { path: "/promotions", icon: "🎁", label: "Promo" },
    { path: "/salary", icon: "💰", label: "Salary" },
    { path: "/transfer-history", icon: "🧾", label: "Transfer history" },
    { path: "/notifications", icon: "🔔", label: "Alerts" },
  ];
  const MENU_GROUPS = [
    { id: "sales", emoji: "🎮", en: "Sales", tone: "gold", items: [
      { en: "New walk-in sale", sub: "Open the sale form", icon: "🛒", path: "/sales" },
      { en: "End-of-session sale", sub: "Check out a console session", icon: "🎫", path: "/sales" },
      { en: "Food & drink sale", sub: "Sell food without game time", icon: "🍔", path: "/food-orders" },
      { en: "Food menu list", sub: "Browse the food menu", icon: "🍜", path: "/food" },
      { en: "Discount / Promo", sub: "View active promotions", icon: "🏷️", path: "/promotions" },
      { en: "Coupon", sub: "Validate & redeem a coupon", icon: "🎟️", path: "/sales" },
    ] },
    { id: "booking", emoji: "📅", en: "Bookings", tone: "warn", items: [
      { en: "Today's bookings", sub: "All bookings for today", icon: "📋", path: "/bookings" },
      { en: "Approve or reject", sub: "Process pending bookings", icon: "✅", path: "/bookings" },
      { en: "Check-in", sub: "Mark customer as arrived", icon: "🎟️", path: "/bookings" },
      { en: "Cancel a booking", sub: "Cancel with a reason", icon: "⛔", path: "/bookings" },
      { en: "Verify deposit", sub: "Confirm a deposit payment", icon: "💰", path: "/bookings" },
      { en: "Reminders", sub: "Booking reminders", icon: "🔔", path: "/notifications" },
    ] },
    { id: "sessions", emoji: "🖥", en: "Sessions & consoles", tone: "indigo", items: [
      { en: "Booking timeline", sub: "Consoles × time, day view", icon: "🕒", path: "/timeline" },
      { en: "Live sessions", sub: "Consoles in use", icon: "🖥", path: "/consoles" },
      { en: "Extend session", sub: "Add more minutes", icon: "⏱", path: "/consoles" },
      { en: "End session", sub: "Close a session", icon: "⏹", path: "/consoles" },
      { en: "Game library", sub: "Installed games", icon: "🎮", path: "/games" },
      { en: "Move session", sub: "Move a session to another console", icon: "🔀", path: "/sessions" },
      { en: "Swap sessions", sub: "Swap two sessions", icon: "🔁", path: "/sessions" },
    ] },
    { id: "member", emoji: "👥", en: "Members", tone: "teal", items: [
      { en: "Find a member", sub: "Search members", icon: "🔍", path: "/members" },
      { en: "Register new member", sub: "Create a member", icon: "➕", path: "/members/register" },
      { en: "Wallet top-up", sub: "Add credit / minutes", icon: "💳", path: "/deposit" },
      { en: "Gift / Free card", sub: "Issue free minutes (owner PIN)", icon: "🎁", path: "/gift-card" },
      { en: "Rank / Bonus", sub: "Rank & bonus table", icon: "🏅", path: "/rank-bonus" },
      { en: "Call member", sub: "Call a member", icon: "📞", tag: "Soon" },
    ] },
    { id: "inventory", emoji: "📦", en: "Inventory", tone: "gold", items: [
      { en: "Stock-in", sub: "Receive stock", icon: "📥", path: "/inventory" },
      { en: "Stock-out", sub: "Manual stock-out", icon: "📤", path: "/stock-out" },
      { en: "Low-stock alerts", sub: "Items running low", icon: "⚠️", path: "/stock-alerts" },
      { en: "Stock check", sub: "Reconcile stock", icon: "🧮", path: "/stock-check" },
    ] },
    { id: "print", emoji: "🖨", en: "Printing", tone: "indigo", items: [
      { en: "Print voucher", sub: "Print a sale voucher", icon: "🧾", path: "/print" },
      { en: "Test print", sub: "Send a test page", icon: "🖨", path: "/print" },
      { en: "Top-up receipt", sub: "Print a top-up receipt", icon: "📃", path: "/deposit" },
    ] },
    { id: "report", emoji: "📊", en: "Reports", tone: "teal", items: [
      { en: "Today's summary", sub: "Daily totals", icon: "📊", path: "/reports" },
      { en: "Operating expenses (OPEX)", sub: "Owner only", icon: "💸", tag: "Owner only" },
      { en: "Account balance", sub: "Cash position (read-only)", icon: "🏦", path: "/balances" },
    ] },
    { id: "accounts", emoji: "🏦", en: "Accounts", tone: "gold", items: [
      { en: "Account Balances", sub: "ACM's Acc & KBZ Bank", icon: "🏦", path: "/account-balances", ownerOnly: true },
      { en: "Transfer", sub: "Move money between accounts", icon: "🔄", path: "/transfer" },
      { en: "Transfer history", sub: "Recent account transfers", icon: "🧾", path: "/transfer-history" },
    ] },
    { id: "me", emoji: "🕒", en: "Me & staff", tone: "dim", items: [
      { en: "Attendance in / out", sub: "Shift check-in", icon: "🕐", path: "/attendance" },
      { en: "Salary advance", sub: "Owner-only request", icon: "💰", path: "/salary", tag: "Owner only" },
      { en: "Staff list", sub: "View staff", icon: "👥", path: "/staff" },
      { en: "Notifications", sub: "Alerts & reminders", icon: "🔔", path: "/notifications" },
      { en: "Settings & profile", sub: "Account settings", icon: "⚙️", path: "/me" },
      { en: "Sign out", sub: "Log out of the app", icon: "🚪", action: "logout" },
    ] },
  ];

  const MoreMenu = {
    template: `
      <div class="psv-pad psv-menu-body">
        <div class="psv-card psv-profile">
          <div class="psv-avatar psv-avatar--lg">{{ initials }}</div>
          <div class="psv-list-row__main">
            <div class="psv-profile__name">{{ staff.name || 'Staff' }}</div>
            <span class="psv-account-role">{{ staff.role || 'staff' }}</span>
          </div>
        </div>
        <div v-for="g in groups" :key="g.id" class="psv-menu-group" :class="'psv-tone-' + g.tone">
          <div class="psv-menu-head">
            <span class="psv-menu-head__ico">{{ g.emoji }}</span>
            <span class="psv-menu-head__my">{{ g.en }}</span>
          </div>
          <div class="psv-card psv-list psv-menu-list">
            <div v-for="it in g.items" :key="it.en" class="psv-menu-item" :class="{ 'is-soon': it.tag }" @click="tap(it)">
              <span class="psv-menu-item__ico">{{ it.icon }}</span>
              <div class="psv-menu-item__main">
                <div class="psv-menu-item__my">{{ it.en }}</div>
                <div class="psv-menu-item__en">{{ it.sub }}</div>
              </div>
              <span v-if="badge(it)" class="psv-menu-badge">{{ badge(it) > 99 ? '99+' : badge(it) }}</span>
              <span v-if="it.tag" class="psv-soon">{{ it.tag }}</span>
              <span v-else-if="ownerBadge(it)" class="psv-soon">Owner only</span>
              <van-icon v-else name="arrow" />
            </div>
          </div>
        </div>
        <button class="psv-btn psv-btn--danger psv-btn--block" @click="logout">🚪 Log out</button>
        <div class="psv-foot">PS VIBE Staff · Play The Game. Share The VIBE!</div>
      </div>
    `,
    setup() {
      const staff = PSV.getStaff() || {};
      const initials = String(staff.name || "?").trim().slice(0, 2).toUpperCase();
      // Admin/owner (role==='admin') sees owner-only rows without the lock badge.
      const isAdmin = computed(() => String(staff.role || "").toLowerCase() === "admin");
      function ownerBadge(it) { return !!(it && it.ownerOnly && !isAdmin.value); }
      // WAVE 3: live low-stock count drives the badge on the "Low-stock alerts" row.
      const lowStock = ref(0);
      async function loadBadges() {
        try {
          const arr = PSV.unwrap(await PSV.api("/api/dashboard/inventory/alerts"));
          lowStock.value = Array.isArray(arr) ? arr.length : Number((arr && arr.total) || 0);
        } catch (_e) { lowStock.value = 0; }
      }
      onMounted(loadBadges);
      function badge(it) { return (it.path === "/stock-alerts" && lowStock.value > 0) ? lowStock.value : 0; }
      function tap(it) {
        haptic("light");
        if (it.action === "logout") { logout(); return; }
        if (it.path) { router.push(it.path); return; }
        if (it.tag === "Owner only") { showToast("🔒 " + it.en + " — owner only, ask an admin", "text"); return; }
        if (it.tag) { showToast("⏳ " + it.en + " — coming soon", "text"); return; }
      }
      function logout() { haptic("medium"); PSV.logout(); showToast("Signed out", "success"); }
      return { groups: MENU_GROUPS, staff, initials, tap, logout, badge, lowStock, isAdmin, ownerBadge };
    },
  };

  const More = {
    components: { PageHeader, MoreMenu },
    template: `
      <div class="pv-page psv-menu">
        <PageHeader :title="nativeMode ? 'Menu' : 'More'" />
        <MoreMenu v-if="nativeMode" />
        <div v-else class="pv-more">
          <div class="pv-action" v-for="it in items" :key="it.path" @click="go(it.path)">
            <div class="pv-action__icon">{{ it.icon }}</div>
            <div class="pv-action__label">{{ it.label }}</div>
          </div>
        </div>
      </div>
    `,
    setup() {
      function go(p) { haptic("light"); router.push(p); }
      return { items: MORE_ITEMS, go, nativeMode: NATIVE };
    },
  };

  // ── VIEW: Consoles ──────────────────────────────────────
  const Consoles = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Consoles" back="/more" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <van-skeleton title :row="3" v-if="loading" style="margin:12px" />
          <van-empty v-else-if="!list.length" description="No consoles" />
          <van-cell-group v-else inset style="margin-top:8px">
            <van-cell v-for="c in list" :key="c.console_id || c.id" :title="c.console_id || c.id" is-link @click="openActions(c)">
              <template #label>
                <span class="pv-dot" :class="'pv-dot--' + dotClass(c.status)"></span>{{ c.status || '-' }}
                <span v-if="c.current_game" style="margin-left:8px;color:var(--pv-muted)">{{ c.current_game }}</span>
                <div v-if="isActive(c)" style="margin-top:2px;color:var(--pv-muted);font-size:12px">
                  {{ c.current_member || c.customer_name || '-' }} · {{ fmtTime(c.start_time_dt || c.start_time) }}<template v-if="c.plan_end"> → {{ c.plan_end }}</template><template v-if="c.duration_mins"> · {{ c.duration_mins }}m</template><template v-if="c.elapsed_mins != null"> · ⏱ {{ c.elapsed_mins }}m elapsed</template>
                </div>
              </template>
              <template #value>
                <van-tag v-if="c.current_member || c.customer_name" :type="isActive(c) ? 'primary' : 'default'">{{ c.current_member || c.customer_name }}</van-tag>
              </template>
            </van-cell>
          </van-cell-group>
        </van-pull-refresh>

        <van-action-sheet v-model:show="sheetShow" :actions="sheetActions" cancel-text="Cancel" @select="onAction" />

        <van-dialog v-model:show="startShow" title="Start Session" show-cancel-button @confirm="submitStart" :before-close="noop" :confirm-button-text="startConfirmLabel">
          <div style="padding:14px 16px 6px">
            <div v-if="startBind.bookings.length" class="psv-bind">
              <div class="psv-bind__head">🧩 {{ startBind.bookings.length > 1 ? startBind.bookings.length + ' bookings for this console — pick one' : 'Booking found for this console' }}</div>
              <div v-for="b in startBind.bookings" :key="b.id" class="psv-bind__row" :class="{ 'psv-bind__row--sel': startBind.selId === b.id }" @click="selectBind(b)">
                <span class="psv-bind__radio">{{ startBind.selId === b.id ? '◉' : '○' }}</span>
                <div class="psv-bind__main">
                  <div class="psv-bind__title">BK#{{ b.id }} · {{ b.member_id || b.customer_name || 'Guest' }}</div>
                  <div class="psv-bind__meta"><template v-if="b.game_name">{{ b.game_name }}</template><template v-if="b.start_time"> · {{ fmtTime(b.start_time) }}</template><template v-if="b.duration_mins"> · {{ b.duration_mins }}m</template></div>
                </div>
                <span class="psv-bind__badge" :class="isCheckedIn(b) ? 'psv-bind__badge--ok' : 'psv-bind__badge--warn'">{{ bindStatusLabel(b) }}</span>
              </div>
            </div>
            <van-field :model-value="startForm.member_name || 'Select member…'" label="Member Name" readonly is-link @click="startMemberPicker = true" />
            <van-field v-model.number="startForm.duration_mins" type="number" label="Minutes" placeholder="60" />
            <div style="padding:2px 16px 8px;display:flex;gap:6px;flex-wrap:wrap">
              <span v-for="d in startDurPresets" :key="d.v" class="psv-dchip" :class="{ 'psv-dchip--on': Number(startForm.duration_mins) === d.v }" @click="startForm.duration_mins = d.v">{{ d.l }}</span>
            </div>
            <div v-if="Number(startForm.duration_mins) === 0" style="padding:0 16px 8px;font-size:12px;color:#8a93a6">📌 စမှတ် = no timer — စတင်ချိန် ပဲ မှတ်၊ အချိန်စက် မလုပ် (ဆော့ပြီး end လုပ်ချိန်မှ တွက်)</div>
            <van-field :model-value="startForm.game_name || 'Select game…'" label="Game" readonly is-link @click="startGamePicker = true" />
          </div>
        </van-dialog>

        <PSVPicker v-if="nativeMode" v-model:show="startMemberPicker" title="Select Member"
                   :items="startMemberPickItems" :model-value="startMemberSel" @confirm="pickStartMemberVal" />
        <van-popup v-else v-model:show="startMemberPicker" position="bottom" round>
          <van-picker :columns="startMemberCols" title="Select Member" @confirm="pickStartMember" @cancel="startMemberPicker=false" />
        </van-popup>
        <PSVPicker v-if="nativeMode" v-model:show="startGamePicker" title="Select Game"
                   :items="startGamePickItems" :model-value="startForm.game_name || ''" @confirm="pickStartGameVal" />
        <van-popup v-else v-model:show="startGamePicker" position="bottom" round>
          <van-picker :columns="startGameCols" title="Select Game" @confirm="pickStartGame" @cancel="startGamePicker=false" />
        </van-popup>

        <van-dialog v-model:show="endDlg.show" :title="endDlg.title" show-cancel-button @confirm="doEndSession">
          <div style="padding:16px">This will end the session and free the console.</div>
        </van-dialog>

        <van-dialog v-model:show="extendDlg.show" title="Extend Session" show-cancel-button @confirm="doExtend">
          <div style="padding:14px 16px 6px">
            <van-field v-model.number="extendDlg.extra" type="number" label="Extra Minutes" placeholder="60" />
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 4px">
              <van-button v-for="n in [15, 30, 60, 120]" :key="n" size="small" :type="extendDlg.extra === n ? 'primary' : 'default'" @click="extendDlg.extra = n">{{ n }}m</van-button>
            </div>
          </div>
        </van-dialog>

        <!-- 🍔 Session Food Order popup (charged at session end voucher) -->
        <van-popup v-model:show="foodShow" position="bottom" round style="max-height:80vh">
          <div style="max-height:80vh;overflow:auto;padding:12px 0 20px">
            <div style="padding:0 16px 8px;font-weight:600">🍔 Food Order — {{ foodConsole ? (foodConsole.console_id || foodConsole.id) : '' }} <span style="color:var(--pv-muted);font-weight:400;font-size:12px">(charged at session end)</span></div>
            <div v-if="foodNote" style="padding:0 16px 8px;color:var(--pv-muted);font-size:12px">{{ foodNote }}</div>
            <van-cell-group v-for="(grp, gi) in sessionFoodGroups" :key="gi" inset :title="grp.cat" style="margin-top:8px">
              <van-cell v-for="it in grp.items" :key="it.name" center>
                <template #title>
                  <span>{{ it.name }}</span>
                  <span style="color:var(--pv-muted);margin-left:8px">{{ fKs(it.price) }} Ks</span>
                </template>
                <template #right-icon>
                  <van-stepper :model-value="sQty(it.name)" min="0" integer @change="(v)=>sSetQty(it, v)" />
                </template>
              </van-cell>
            </van-cell-group>
            <van-empty v-if="!sessionFoodGroups.length" description="No food items" />
            <div v-if="foodSel.length" style="padding:10px 16px 0;display:flex;align-items:center;gap:8px">
              <span style="font-weight:600">🛒 Adding ({{ foodSel.length }})</span>
              <span style="color:var(--pv-muted);font-size:12px;flex:1">Pick quantities then tap Save — existing items are kept and these are added</span>
              <span style="font-weight:700;color:var(--pv-primary)">{{ fKs(foodSel.reduce((s,x)=>s+(Number(x.qty)||0)*(Number(x.price)||0),0)) }} Ks</span>
            </div>
            <div style="padding:14px 16px;display:flex;gap:8px">
              <van-button block plain @click="foodShow=false">Cancel</van-button>
              <van-button block type="primary" :loading="foodSaving" :disabled="!foodSel.length" @click="saveSessionFood">{{ foodSel.length ? ('Add ' + foodSel.length + ' item(s)') : 'Select items to add' }}</van-button>
            </div>
          </div>
        </van-popup>

        <!-- 🔀 Move / 🔁 Swap session -->
        <van-popup v-model:show="xsess.show" position="bottom" round style="max-height:70vh">
          <div style="max-height:70vh;overflow:auto;padding:12px 0 20px">
            <div class="psv-sheet-title" style="padding:0 16px">{{ xsess.mode === 'move' ? '🔀 Move session to console' : '🔁 Swap with session' }}</div>
            <van-empty v-if="!xsess.options.length" :description="xsess.mode === 'move' ? 'No other consoles' : 'No other active sessions'" />
            <div v-for="o in xsess.options" :key="o.console_id || o.id" class="psv-list-row" style="padding:12px 16px" @click="xsess.mode === 'move' ? doMove(o) : doSwap(o)">
              <div class="psv-list-row__main">
                <div class="psv-list-row__title">{{ o.console_id || o.id }}</div>
                <div class="psv-list-row__sub">{{ o.status || '-' }}<template v-if="o.current_member || o.customer_name"> · {{ o.current_member || o.customer_name }}</template></div>
              </div>
              <van-icon name="arrow" />
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup() {
      const list = ref([]);
      const loading = ref(true);
      const refreshing = ref(false);
      const sheetShow = ref(false);
      const sheetActions = ref([]);
      const activeConsole = ref(null);
      const startShow = ref(false);
      const startForm = reactive({ member_id: "", member_name: "", duration_mins: 60, game_name: "", linked_booking_id: null });
      // "စမှတ်" = start marker only / no timer (duration 0 = API No Timer mode) — owner ask 2026-09-20
      const startDurPresets = [{ v: 30, l: "30m" }, { v: 60, l: "60m" }, { v: 90, l: "90m" }, { v: 120, l: "120m" }, { v: 0, l: "📌 စမှတ် (no timer)" }];
      const startMemberPicker = ref(false);
      const startGamePicker = ref(false);
      const startMemberCols = ref([]);
      const startGameCols = ref([]);
      // WAVE 5b: raw lists feeding the premium picker sheets (same fetch, markup only).
      const startMembers = ref([]);
      const startGames = ref([]);
      const startMemberPickItems = computed(() => {
        const guest = { value: "Guest", label: "Guest (Walk-in)", sub: "No membership", icon: "🚶" };
        const arr = startMembers.value.map((m) => ({
          value: m.id || m.name,
          label: m.name || m.id,
          sub: "ID " + (m.id || "-") + (m.phone ? " · " + m.phone : ""),
          meta: (m.wallet_balance != null || m.liability_mins != null) ? ("⏱ " + fKs(m.wallet_balance != null ? m.wallet_balance : (m.liability_mins != null ? m.liability_mins : 0)) + " min") : "",
          avatar: String(m.name || m.id || "?").trim().slice(0, 2).toUpperCase(),
        }));
        return [guest].concat(arr);
      });
      const startGamePickItems = computed(() => startGames.value.filter((g) => g.game_title).map((g) => ({
        value: g.game_title,
        label: g.game_title,
        sub: [g.genre, g.solo_multi].filter(Boolean).join(" · "),
        meta: g.final_status || "",
        icon: "🎮",
      })));
      const startMemberSel = computed(() => (startForm.member_name === "Guest" ? "Guest" : (startForm.member_id || "")));
      // Bind candidates for the active console (today). `bookings` = every candidate
      // with its REAL status; `selId` = the staff-selected one.
      const startBind = reactive({ bookings: [], selId: null });
      // Deep-link stash: /consoles?bind=<id> → preselect that booking once loaded.
      const pendingBindId = ref(null);
      const endDlg = reactive({ show: false, title: "", booking_id: null, console_id: "", member_name: "", mins: 0 });
      const extendDlg = reactive({ show: false, console_id: "", extra: 60 });
      const noop = () => true;

      function dotClass(s) { return ({ Active: "active", Free: "free", Reserved: "reserved", Busy: "busy", Maintenance: "maintenance" })[s] || "idle"; }
      // A candidate is REALLY checked-in only when the API says so (session row or
      // booking status), never guessed client-side.
      function isCheckedIn(b) {
        if (!b) return false;
        return String(b.session_status || "").toLowerCase() === "checked_in" || String(b.status || "").toLowerCase() === "checked_in";
      }
      function bindStatusLabel(b) {
        if (isCheckedIn(b)) return "Checked-in";
        const s = String(b.status || "").toLowerCase();
        if (s === "pending_check_in" || s === "pending") return "Pending check-in";
        return "Confirmed — not checked in yet";
      }
      const startConfirmLabel = computed(() => {
        const noTimer = Number(startForm.duration_mins) === 0;
        const b = startBind.bookings.find((x) => x.id === startBind.selId);
        const base = !b ? "Start" : (isCheckedIn(b) ? "Start with this booking" : "Check in & start");
        return noTimer ? ("📌 စမှတ် · " + base) : base;
      });
      function isActive(c) { const s = String(c.status || "").toLowerCase(); return s === "active" || s === "checked_in"; }
      function fmtTime(v) {
        if (!v) return "--:--";
        const d = new Date(v);
        if (!isNaN(d.getTime())) { const hh = String(d.getHours()).padStart(2, "0"); const mm = String(d.getMinutes()).padStart(2, "0"); return hh + ":" + mm; }
        const m = String(v).match(/(\d{2}:\d{2})/);
        return m ? m[1] : String(v).slice(0, 5);
      }

      async function load() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_console_status")); list.value = (d && d.consoles) || []; }
        catch (e) { showToast("Load failed: " + e.message, "fail"); list.value = []; }
        finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(async () => {
        await load();
        const bindId = (router.query.value || {}).bind;
        if (bindId) await openBindFromQuery(bindId);
      });

      // Deep-link from Booking detail "Start session" → /consoles?bind=<id>:
      // resolve the booking's console, open its Start Session sheet and preselect it.
      async function openBindFromQuery(bindId) {
        try {
          const d = PSV.unwrap(await PSV.api("/api/bookings/" + encodeURIComponent(bindId)));
          const bk = (d && d.booking) || null;
          if (!bk) { showToast("Booking #" + bindId + " not found", "fail"); return; }
          const cid = bk.console_id || "";
          const c = list.value.find((x) => (x.console_id || x.id) === cid);
          if (!c) { showToast("Console " + (cid || "?") + " not found", "warning"); return; }
          activeConsole.value = c;
          pendingBindId.value = bk.id;
          if (String(c.status || "").toLowerCase() === "free") {
            startForm.member_id = ""; startForm.member_name = ""; startForm.duration_mins = 60; startForm.game_name = ""; startForm.linked_booking_id = null;
            startBind.bookings = []; startBind.selId = null;
            startShow.value = true;
            loadStartOptions();
          } else {
            openActions(c);
          }
        } catch (e) { showToast("Bind failed: " + e.message, "fail"); }
      }

      function openActions(c) {
        activeConsole.value = c;
        const actions = [];
        const st = String(c.status || "").toLowerCase();
        const hasBooking = !!(c.booking_id);
        if (st === "free") {
          actions.push({ name: "▶ Start Session", value: "start", color: "#07c160" });
          actions.push({ name: "📅 Book", value: "book", color: "#1989fa" });
        } else if (st === "active" && hasBooking) {
          actions.push({ name: "🍔 Food Order", value: "food", color: "#ff976a" });
          actions.push({ name: "⏱ Extend Session", value: "extend", color: "#1989fa" });
          actions.push({ name: "🔀 Move to console", value: "move", color: "#1989fa" });
          actions.push({ name: "🔁 Swap with session", value: "swap", color: "#1989fa" });
          actions.push({ name: "⏹ End Session", value: "end", color: "#ee0a24" });
        }
        if (!actions.length) {
          showToast("No actions available for this console", "info");
          return;
        }
        sheetActions.value = actions;
        sheetShow.value = true;
        haptic("light");
      }

      function onAction(action) {
        sheetShow.value = false;
        if (!action || !action.value) return;
        const c = activeConsole.value;
        if (action.value === "start") {
          startForm.member_id = ""; startForm.member_name = ""; startForm.duration_mins = 60; startForm.game_name = ""; startForm.linked_booking_id = null;
          startBind.value = { booking: null };
          startShow.value = true;
          loadStartOptions();
        } else if (action.value === "book") {
          router.push("/bookings");
        } else if (action.value === "extend") {
          extendDlg.console_id = c.console_id || c.id;
          extendDlg.extra = 60;
          extendDlg.show = true;
        } else if (action.value === "food") {
          openSessionFood(c);
        } else if (action.value === "move") {
          openMove(c);
        } else if (action.value === "swap") {
          openSwap(c);
        } else if (action.value === "end") {
          confirmEnd(c);
        }
      }

      // 🔀 Move / 🔁 Swap (Wave 2 contract). The server enforces overlap guards and
      // returns a conflict message which we surface verbatim.
      const xsess = reactive({ show: false, mode: "move", src: null, options: [] });
      function openMove(c) {
        xsess.mode = "move"; xsess.src = c;
        const cid = c.console_id || c.id;
        xsess.options = list.value.filter((x) => (x.console_id || x.id) !== cid);
        xsess.show = true; haptic("light");
      }
      function openSwap(c) {
        xsess.mode = "swap"; xsess.src = c;
        xsess.options = list.value.filter((x) => x.booking_id && x.booking_id !== c.booking_id && String(x.status || "").toLowerCase() === "active");
        xsess.show = true; haptic("light");
      }
      async function doMove(target) {
        const c = xsess.src; if (!c || !target) return;
        const to = target.console_id || target.id;
        xsess.show = false; haptic("medium");
        try {
          await PSV.api("/api/sessions/move", { method: "POST", headers: HJSON, body: JSON.stringify({ booking_id: c.booking_id, to_console_id: to }) });
          showToast("Moved to " + to, "success"); load();
        } catch (e) { showToast(e.message || "Move failed", "fail"); }
      }
      async function doSwap(target) {
        const c = xsess.src; if (!c || !target) return;
        xsess.show = false; haptic("medium");
        try {
          await PSV.api("/api/sessions/swap", { method: "POST", headers: HJSON, body: JSON.stringify({ booking_id_1: c.booking_id, booking_id_2: target.booking_id }) });
          showToast("Sessions swapped", "success"); load();
        } catch (e) { showToast(e.message || "Swap failed", "fail"); }
      }

      // 🍔 Session Food Order (mirror sale bot cmd_session_food_order → food_cart by booking)
      const foodShow = ref(false);
      const foodMenu = ref({});
      const foodSel = ref([]);  // [{name, price, qty}]
      const foodSaving = ref(false);
      const foodConsole = ref(null);

      const foodNote = ref(""); // subtle note when existing items are already recorded
      async function openSessionFood(c) {
        foodConsole.value = c;
        foodSel.value = [];
        foodNote.value = "";
        foodShow.value = true;
        haptic("light");
        try {
          const d = PSV.unwrap(await PSV.api("/api/fetch_food_menu"));
          foodMenu.value = d || {};
        } catch (e) { /* menu loads lazily; picker shows what it has */ }
        // 🍔 ADD-only flow: do NOT prefill existing items (staff might accidentally
        // zero/delete them). Just show a subtle count note so staff know items exist.
        const bid = c && c.booking_id;
        if (bid) {
          try {
            const ex = PSV.unwrap(await PSV.api("/api/food-cart/booking/" + encodeURIComponent(bid)));
            const items = (ex && ex.items) || [];
            if (items && items.length) {
              foodNote.value = items.length + " items already ordered — pick more and tap Save to add";
            }
          } catch (ee) { /* existing count optional */ }
        }
      }
      const sessionFoodGroups = computed(() => {
        const m = foodMenu.value || {};
        if (Array.isArray(m)) return [{ cat: "", items: m.map((x) => ({ name: x.item_name || x.name, price: x.price })) }];
        const groups = [];
        for (const cat in m) {
          const v = m[cat];
          const items = [];
          if (v && typeof v === "object" && !Array.isArray(v)) {
            for (const nm in v) items.push({ name: nm, price: v[nm] });
          } else if (Array.isArray(v)) {
            v.forEach((x) => items.push({ name: (x && (x.item_name || x.name)) || x, price: x && x.price }));
          }
          if (items.length) groups.push({ cat, items });
        }
        return groups;
      });
      function sQty(name) { const l = foodSel.value.find((x) => x.name === name); return l ? l.qty : 0; }
      function sSetQty(it, qty) {
        qty = Number(qty) || 0;
        const i = foodSel.value.findIndex((x) => x.name === it.name);
        if (qty <= 0) { if (i >= 0) foodSel.value.splice(i, 1); }
        else if (i >= 0) foodSel.value[i].qty = qty;
        else foodSel.value.push({ name: it.name, price: it.price, qty });
      }
      async function saveSessionFood() {
        const c = foodConsole.value;
        const bid = c && c.booking_id;
        if (!bid) { foodShow.value = false; return; }
        foodSaving.value = true;
        try {
          if (!foodSel.value.length) {
            // nothing selected → just close (DO NOT clear existing cart)
            foodShow.value = false;
            return;
          }
          // 🔄 ADD mode — merge onto existing cart (existing rows preserved, new items added)
          const res = PSV.unwrap(await PSV.api("/api/food-cart/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ booking_id: String(bid), mode: "add", items: foodSel.value.map((x) => ({ item_name: x.name, quantity: x.qty, unit_price: x.price })) }) }));
          const total = (res && res.total) || foodSel.value.reduce((s, x) => s + (Number(x.qty)||0) * (Number(x.price)||0), 0);
          showToast("Added " + foodSel.value.length + " item(s) = " + PSV.fmtKs(total) + " Ks — charged at session end", "success");
          foodShow.value = false;
          load();
        } catch (e) {
          showToast("Food save failed: " + e.message, "fail");
        } finally { foodSaving.value = false; }
      }

      function confirmEnd(c) {
        const name = c.console_id || c.id;
        endDlg.title = "End Session — " + name + "?";
        endDlg.booking_id = c.booking_id;
        endDlg.console_id = c.console_id || c.id;
        endDlg.member_name = c.current_member || c.customer_name || "Guest";
        // actual elapsed mins (live) preferred; fallback planned duration
        endDlg.mins = (c.elapsed_mins != null && Number(c.elapsed_mins) > 0) ? Math.round(Number(c.elapsed_mins)) : (c.duration_mins || 0);
        endDlg.show = true;
      }

      async function doEndSession() {
        const bid = endDlg.booking_id;
        if (!bid) { showToast("No booking for this console", "fail"); return; }
        haptic("heavy");
        try {
          await PSV.api("/api/end_booking/" + encodeURIComponent(bid), { method: "PUT" });
          // 🧾 Open Sales voucher prefilled with session data (mirror bot launch_session_sale)
          PSV.pendingSessionSale = {
            console_id: endDlg.console_id,
            member_name: endDlg.member_name,
            mins: Number(endDlg.mins) || 0,
            booking_id: bid,
          };
          showToast("Session ended — voucher ready", "success");
          endDlg.show = false;
          load();
          router.push("/sales");
        } catch (e) { showToast("End failed: " + e.message, "fail"); }
      }

      async function loadStartOptions() {
        // member dropdown options
        if (!startMemberCols.value.length) {
          try {
            const d = PSV.unwrap(await PSV.api("/api/fetch_members?limit=300"));
            const ms = (d && d.members) || [];
            startMembers.value = ms;
            startMemberCols.value = [{ text: "Guest (Walk-in)", value: "Guest" }].concat(ms.map((m) => ({ text: (m.name || m.id) + (m.id && m.name && m.id !== m.name ? "  (" + m.id + ")" : ""), value: m.id || m.name })));
          } catch (e) { showToast("Member load failed", "fail"); }
        }
        // game dropdown options
        if (!startGameCols.value.length) {
          try {
            const d = PSV.unwrap(await PSV.api("/api/fetch_games"));
            const gs = (d && d.games) || [];
            startGames.value = gs;
            startGameCols.value = gs.filter((g) => g.game_title).map((g) => ({ text: g.game_title, value: g.game_title }));
          } catch (e) { /* optional */ }
        }
        // bind candidates for this console (today) — REAL status per row, no guessing
        const c = activeConsole.value;
        if (!c) return;
        try {
          const d = PSV.unwrap(await PSV.api("/api/get-confirmed-booking?console_id=" + encodeURIComponent(c.console_id || c.id)));
          const cands = (d && (d.bookings || (d.booking ? [d.booking] : []))) || [];
          startBind.bookings = cands;
          startBind.selId = null;
          startForm.linked_booking_id = null;
          const want = pendingBindId.value;
          const target = (want != null && cands.find((x) => String(x.id) === String(want))) || (cands.length === 1 ? cands[0] : null);
          if (target) selectBind(target);
          pendingBindId.value = null;
        } catch (e) { startBind.bookings = []; startBind.selId = null; }
      }

      function pickStartMember({ selectedOptions }) {
        const opt = selectedOptions && selectedOptions[0];
        if (!opt) return;
        const val = String(opt.value || "");
        if (val === "Guest") {
          startForm.member_id = "";
          startForm.member_name = "Guest";
        } else {
          startForm.member_id = val;
          startForm.member_name = String(opt.text || val).replace(/\s*\(.*\)$/, "");
        }
        startMemberPicker.value = false;
      }

      function pickStartGame({ selectedOptions }) {
        const opt = selectedOptions && selectedOptions[0];
        if (!opt) return;
        startForm.game_name = String(opt.value || opt.text || "");
        startGamePicker.value = false;
      }

      // WAVE 5b: premium picker handlers (same resulting form state; value in, no columns parsing).
      function pickStartMemberVal(val) {
        const v = String(val == null ? "" : val);
        if (v === "Guest") { startForm.member_id = ""; startForm.member_name = "Guest"; }
        else {
          const it = startMemberPickItems.value.find((x) => String(x.value) === v);
          startForm.member_id = v;
          startForm.member_name = it ? it.label : v;
        }
        startMemberPicker.value = false;
      }
      function pickStartGameVal(val) {
        startForm.game_name = String(val == null ? "" : val);
        startGamePicker.value = false;
      }

      // Staff taps a candidate row → select it (radio) AND bind it to this session.
      function selectBind(b) {
        if (!b) return;
        startBind.selId = b.id;
        startForm.member_id = b.member_id || b.customer_name || "";
        startForm.member_name = b.member_id || b.customer_name || ("BK#" + b.id);
        if (b.game_name) startForm.game_name = b.game_name;
        if (b.duration_mins && Number(b.duration_mins) > 0) startForm.duration_mins = Number(b.duration_mins);
        startForm.linked_booking_id = b.id;
        haptic("medium");
        showToast("Bound to BK#" + b.id + (isCheckedIn(b) ? " ✓" : " — not checked in yet"), isCheckedIn(b) ? "success" : "warning");
      }

      async function doExtend() {
        const cid = extendDlg.console_id;
        const extra = Number(extendDlg.extra);
        if (!cid || !extra || extra <= 0) { showToast("Enter extra minutes", "warning"); return; }
        haptic("medium");
        try {
          const res = PSV.unwrap(await PSV.api("/api/bookings/extend-duration", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ console_id: cid, extra_mins: extra }),
          }));
          showToast("Extended +" + extra + "m ✓ (total " + (res && res.new_duration_mins != null ? res.new_duration_mins : "?") + ")", "success");
          extendDlg.show = false;
          load();
        } catch (e) { showToast("Extend failed: " + e.message, "fail"); }
      }

      async function submitStart() {
        const c = activeConsole.value;
        if (!c) return;
        const name = startForm.member_name.trim();
        const dur = Number(startForm.duration_mins);
        if (!name) { showToast("Member name required", "warning"); return; }
        haptic("medium");
        const bid = startForm.linked_booking_id;
        const cand = bid ? startBind.bookings.find((x) => x.id === bid) : null;
        try {
          // Bind flow: sessions/start REQUIRES a checked_in session row for the linked
          // booking, so if the chosen candidate is still 'confirmed' we check it in
          // first (POST /api/bookings/checkin creates the checked_in row) then start.
          if (bid && cand && !isCheckedIn(cand)) {
            await PSV.api("/api/bookings/checkin", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ id: bid, staff_name: PSV.getStaff().name }),
            });
          }
          await PSV.api("/api/sessions/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              console_id: c.console_id || c.id,
              member_id: startForm.member_id || undefined,
              member_name: name,
              duration_mins: isNaN(dur) ? 0 : dur,
              game_name: (startForm.game_name || "").trim(),
              linked_booking_id: bid || undefined,
              staff_name: PSV.getStaff().name,
            }),
          });
          showToast("Session started ✓", "success");
          startShow.value = false;
          load();
        } catch (e) { showToast("Start failed: " + e.message, "fail"); }
      }

      return { list, loading, refreshing, dotClass, isActive, fmtTime, load, sheetShow, sheetActions, openActions, onAction, xsess, doMove, doSwap, startShow, startForm, startMemberPicker, startGamePicker, startMemberCols, startGameCols, startMemberPickItems, startGamePickItems, startMemberSel, pickStartMemberVal, pickStartGameVal, nativeMode: NATIVE, startBind, endDlg, extendDlg, noop, submitStart, doEndSession, doExtend, loadStartOptions, pickStartMember, pickStartGame, selectBind, isCheckedIn, bindStatusLabel, startConfirmLabel, startDurPresets, foodShow, foodConsole, foodNote, sessionFoodGroups, foodSel, sQty, sSetQty, foodSaving, saveSessionFood, openSessionFood, fKs };
    },
  };

  // ── VIEW: Food Orders (active sessions food carts) ─────────────
  const FoodOrders = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="🍔 Active Food Orders" back="/more" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <van-empty v-if="!loading && !list.length" description="No active food orders" />
          <van-cell-group v-else inset style="margin-top:8px">
            <van-cell v-for="s in list" :key="s.session_id" :title="s.console_id + ' · ' + (s.customer_name || 'Guest')" is-link @click="openEdit(s)">
              <template #label>
                <div v-for="(it, i) in s.items" :key="i" style="color:var(--pv-muted);font-size:12px">
                  {{ it.item_name }} ×{{ it.quantity }} = {{ fKs(it.quantity * it.unit_price) }} Ks
                </div>
              </template>
              <template #value>
                <van-tag type="warning" style="margin-left:8px">{{ fKs(s.total) }} Ks</van-tag>
              </template>
            </van-cell>
          </van-cell-group>
        </van-pull-refresh>

        <!-- Edit popup: shows ONLY ordered items (item + qty), not the whole menu -->
        <van-popup v-model:show="editShow" position="bottom" round style="max-height:85vh">
          <div style="max-height:85vh;overflow:auto;padding:12px 0 20px">
            <div style="padding:0 16px 8px;font-weight:600">🍔 Food Order — {{ editTarget ? (editTarget.console_id + ' · ' + (editTarget.customer_name || 'Guest')) : '' }} <span style="color:var(--pv-muted);font-weight:400;font-size:12px">(charged at session end)</span></div>
            <div style="padding:0 16px 8px;color:var(--pv-muted);font-size:12px">Set qty to 0 to remove an item — then tap Save</div>
            <van-cell-group inset style="margin-top:8px">
              <van-cell v-for="it in editSel" :key="it.name" center>
                <template #title>
                  <span>{{ it.name }}</span>
                  <span style="color:var(--pv-muted);margin-left:8px">{{ fKs(it.price) }} Ks</span>
                </template>
                <template #right-icon>
                  <van-stepper :model-value="it.qty" min="0" integer @change="(v)=>setQty(it, v)" />
                </template>
              </van-cell>
            </van-cell-group>
            <van-empty v-if="!editSel.length" description="No items — add more from Consoles" />
            <div v-if="editSel.length" style="padding:10px 16px 0;display:flex;align-items:center;gap:8px">
              <span style="font-weight:600">🛒 Total ({{ editSel.length }})</span>
              <span style="font-weight:700;color:var(--pv-primary);flex:1;text-align:right">{{ fKs(editSel.reduce((s,x)=>s+(Number(x.qty)||0)*(Number(x.price)||0),0)) }} Ks</span>
            </div>
            <div style="padding:14px 16px;display:flex;gap:8px">
              <van-button block plain @click="editShow=false">Cancel</van-button>
              <van-button block type="primary" :loading="saving" @click="saveEdit">{{ editSel.length ? ('Save ' + editSel.length + ' items') : 'Clear order' }}</van-button>
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup() {
      const list = ref([]);
      const loading = ref(true);
      const refreshing = ref(false);
      const editShow = ref(false);
      const editTarget = ref(null);
      const editSel = ref([]); // [{name, price, qty}] — only ordered items (item + qty)
      const saving = ref(false);

      async function load() {
        loading.value = true;
        try {
          const d = PSV.unwrap(await PSV.api("/api/food-orders/active"));
          const data = (d && d.data) || (Array.isArray(d) ? d : []);
          list.value = data || [];
        } catch (e) { showToast("Load failed: " + e.message, "fail"); list.value = []; }
        finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(load);

      function setQty(it, qty) {
        qty = Number(qty) || 0;
        const i = editSel.value.findIndex((x) => x.name === it.name);
        if (qty <= 0) { if (i >= 0) editSel.value.splice(i, 1); }
        else if (i >= 0) editSel.value[i].qty = qty;
        else editSel.value.push({ name: it.name, price: it.price, qty });
      }

      async function openEdit(s) {
        editTarget.value = s;
        editSel.value = (s.items || []).map((x) => ({ name: x.item_name || x.name, price: Number(x.unit_price || x.price || 0), qty: Number(x.quantity || 0) })).filter((x) => x.name && x.qty > 0);
        editShow.value = true;
        haptic("light");
      }

      async function saveEdit() {
        const s = editTarget.value;
        if (!s || s.booking_id == null) { editShow.value = false; return; }
        saving.value = true;
        try {
          if (!editSel.value.length) {
            await PSV.api("/api/food-cart/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ booking_id: String(s.booking_id), items: [] }) });
            showToast("Food order cleared", "success");
            editShow.value = false;
            load();
            return;
          }
          const res = PSV.unwrap(await PSV.api("/api/food-cart/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ booking_id: String(s.booking_id), items: editSel.value.map((x) => ({ item_name: x.name, quantity: x.qty, unit_price: x.price })) }) }));
          const total = (res && res.total) || editSel.value.reduce((s2, x) => s2 + (Number(x.qty) || 0) * (Number(x.price) || 0), 0);
          showToast(editSel.value.length + " items = " + PSV.fmtKs(total) + " Ks — charged at session end", "success");
          editShow.value = false;
          load();
        } catch (e) {
          showToast("Food save failed: " + e.message, "fail");
        } finally { saving.value = false; }
      }

      return { list, loading, refreshing, load, editShow, editTarget, editSel, setQty, openEdit, saveEdit, saving, fKs };
    },
  };

  // ── VIEW: Inventory (stock list + stock-in) ─────────────
  const Inventory = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Inventory" back="/more" />
        <van-tabs v-model:active="tab" sticky>
          <van-tab title="List">
            <van-pull-refresh v-model="refreshing" @refresh="loadStock">
              <van-empty v-if="!stock.length && !loading" description="No stock" />
              <van-cell-group v-else inset style="margin-top:8px">
                <van-cell v-for="s in stock" :key="s.id || s.item_name" :title="s.item_name" :label="'Qty: ' + (s.quantity ?? '-')">
                  <template #value><span class="pv-amount">{{ fKs(s.unit_price) }} Ks</span></template>
                </van-cell>
              </van-cell-group>
            </van-pull-refresh>
          </van-tab>
          <van-tab title="Stock In">
            <van-cell-group inset style="margin-top:8px">
              <template v-for="(row, ri) in rows" :key="ri">
                <div style="display:flex;align-items:flex-start;gap:0">
                  <van-cell-group inset style="margin-top:8px;flex:1">
                    <van-field :model-value="row.item_name" label="Item" placeholder="Select item" is-link readonly @click="rowItemPicker = ri" />
                    <van-field v-if="row.isNew" v-model="row.custom_name" label="New Item Name" placeholder="Type new item name" />
                    <van-field v-model.number="row.quantity" type="number" label="Quantity" placeholder="0" />
                    <van-field v-model.number="row.unit_cost" type="number" label="Unit Cost" placeholder="0" />
                  </van-cell-group>
                  <van-button v-if="rows.length > 1" icon="cross" size="small" plain type="danger" style="margin:14px 0 0 8px" @click="rows.splice(ri, 1)" />
                </div>
              </template>
              <div style="padding:10px 16px">
                <van-button block plain type="primary" icon="plus" @click="addRow">Add Item</van-button>
              </div>
              <van-field :model-value="form.payment_method" label="Payment" is-link readonly @click="payPicker=true" />
            </van-cell-group>
            <div class="psv-stockin-total" v-if="validCount">
              <span>Total cost · {{ validCount }} item{{ validCount === 1 ? '' : 's' }}</span>
              <b class="psv-tabnum">{{ fKs(totalCost) }} Ks</b>
            </div>
            <div style="padding:14px 16px">
              <van-button type="primary" block :loading="submitting" :disabled="!validCount" @click="submit">Stock In ({{ validCount }} item{{ validCount === 1 ? '' : 's' }})</van-button>
            </div>
          </van-tab>
        </van-tabs>
        <van-popup v-model:show="payPicker" position="bottom" round>
          <van-picker :columns="payColumns" @confirm="pickPay" @cancel="payPicker=false" />
        </van-popup>
        <van-popup :show="rowItemPickerShow" position="bottom" round @update:show="(v) => { if (!v) rowItemPicker = null; }">
          <van-picker :columns="itemColumns" @confirm="pickRowItem" @cancel="rowItemPicker=null" />
        </van-popup>
      </div>
    `,
    setup() {
      const tab = ref(0);
      const stock = ref([]);
      const loading = ref(true);
      const refreshing = ref(false);
      const payPicker = ref(false);
      const rowItemPicker = ref(null);
      const submitting = ref(false);
      const PAY_METHODS = ["Cash", "KPay", "WavePay", "AYA Pay"];
      const payColumns = PAY_METHODS.map((p) => ({ text: p, value: p }));
      const form = reactive({ payment_method: "Cash" });
      const rows = ref([{ item_name: "", custom_name: "", quantity: 0, unit_cost: 0, isNew: false }]);
      const itemColumns = computed(() => [
        ...stock.value.map((s) => ({ text: s.item_name, value: s.item_name })),
        { text: "🆕 New Item…", value: "__new__" },
      ]);
      const rowItemPickerShow = computed(() => rowItemPicker.value !== null);
      // WAVE 5 UI fixes: valid-line count (was rows.length, which counted empty rows),
      // and a live total-cost line so staff can see the batch value before submitting.
      function rowValid(r) { return !!((r.isNew ? (r.custom_name || "").trim() : r.item_name) && Number(r.quantity) > 0); }
      const validCount = computed(() => rows.value.filter(rowValid).length);
      const totalCost = computed(() => rows.value.reduce((s, r) => s + (rowValid(r) ? (Number(r.quantity) || 0) * (Number(r.unit_cost) || 0) : 0), 0));
      function addRow() { rows.value.push({ item_name: "", custom_name: "", quantity: 0, unit_cost: 0, isNew: false }); haptic("light"); }
      async function loadStock() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/dashboard/inventory")); stock.value = Array.isArray(d) ? d : (d && d.items) || []; }
        catch (e) { stock.value = []; }
        finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(loadStock);
      function pickPay({ selectedOptions }) { form.payment_method = selectedOptions[0].value; payPicker.value = false; }
      function pickRowItem({ selectedOptions }) {
        const ri = rowItemPicker.value; rowItemPicker.value = null;
        if (ri === null || !selectedOptions || !selectedOptions[0]) return;
        const val = selectedOptions[0].value;
        const row = rows.value[ri]; if (!row) return;
        if (val === "__new__") { row.isNew = true; row.item_name = "🆕 New Item…"; }
        else {
          row.isNew = false; row.item_name = val;
          // WAVE 5 UI fix: prefill unit cost from the item's last known price (only when blank).
          if (!Number(row.unit_cost)) {
            const hit = stock.value.find((s) => (s.item_name || "") === val);
            const price = hit ? Number(hit.unit_price) : 0;
            if (price > 0) row.unit_cost = price;
          }
        }
      }
      async function submit() {
        const items = [];
        for (const row of rows.value) {
          const name = row.isNew ? (row.custom_name || "").trim() : row.item_name;
          if (!name || !row.quantity || row.quantity <= 0) continue;
          items.push({ item_name: name, quantity: row.quantity, unit_cost: row.unit_cost || 0 });
        }
        if (!items.length) { showToast("Select at least one item with qty", "fail"); return; }
        submitting.value = true; haptic("medium");
        try {
          const res = PSV.unwrap(await PSV.api("/api/stock/in/batch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items, payment_method: form.payment_method, staff_name: PSV.getStaff().name }) }));
          showToast("Stock-in done ✓ " + (res && res.batch_id ? res.batch_id : "") + " · " + items.length + " item(s)", "success");
          rows.value = [{ item_name: "", custom_name: "", quantity: 0, unit_cost: 0, isNew: false }];
          loadStock();
        } catch (e) { showToast("Error: " + e.message, "fail"); }
        finally { submitting.value = false; }
      }
      return { tab, stock, loading, refreshing, payPicker, rowItemPicker, rowItemPickerShow, submitting, payColumns, itemColumns, rows, form, addRow, loadStock, pickPay, pickRowItem, submit, validCount, totalCost, fKs };
    },
  };

  // ── VIEW: Reports (today) ───────────────────────────────
  const Reports = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Today's Summary" back="/more" />
        <van-skeleton title :row="4" v-if="loading" style="margin:12px" />
        <template v-else>
          <div v-if="!stats" class="psv-empty">Summary unavailable — pull to refresh</div>
          <template v-else>
            <van-cell-group inset title="Totals" style="margin-top:8px">
              <van-cell title="Sales total" :value="fKs(stats.today_revenue) + ' Ks'" />
              <van-cell title="Food & drink" :value="fKs(foodTotal) + ' Ks'" />
              <van-cell title="Transactions" :value="String(stats.sales_count ?? '-') + ' · ' + String(stats.today_bookings ?? '-') + ' bookings'" />
              <van-cell title="Active players" :value="String(stats.active_players ?? '-') + ' · ' + String(stats.total_members ?? '-') + ' members'" />
            </van-cell-group>
            <van-cell-group inset title="Per staff" style="margin-top:14px">
              <van-cell v-for="s in byStaff" :key="s.staff_name" :title="s.staff_name || '—'" :label="s.count + ' sale(s) · food ' + fKs(s.food) + ' Ks'" :value="fKs(s.net) + ' Ks'" />
              <van-cell v-if="!byStaff.length" title="No sales yet today" />
            </van-cell-group>
            <van-cell-group inset title="Low stock" style="margin-top:14px">
              <van-cell v-for="a in lowStock" :key="a.id" :title="a.item_name" :label="'Qty ' + a.quantity + ' / reorder ' + a.reorder_level">
                <template #value><van-tag :type="a.status === 'critical' ? 'danger' : 'warning'">{{ a.status }}</van-tag></template>
              </van-cell>
              <van-cell v-if="!lowStock.length" title="All good" label="No items below reorder level" />
            </van-cell-group>
            <div style="padding:14px 16px">
              <van-button block plain type="primary" @click="openBalances">🏦 Account balances (read-only)</van-button>
            </div>
          </template>
        </template>
      </div>
    `,
    setup() {
      const stats = ref(null);
      const lowStock = ref([]);
      const loading = ref(true);
      const byStaff = computed(() => ((stats.value && stats.value.sales_by_staff) || []).slice());
      const foodTotal = computed(() => byStaff.value.reduce((sum, s) => sum + (Number(s.food) || 0), 0));
      function openBalances() { haptic("light"); router.push("/balances"); }
      async function load() {
        loading.value = true;
        const [st, al] = await Promise.allSettled([
          PSV.api("/api/dashboard/stats"),
          PSV.api("/api/dashboard/inventory/alerts"),
        ]);
        stats.value = st.status === "fulfilled" ? (PSV.unwrap(st.value) || null) : null;
        lowStock.value = al.status === "fulfilled" ? (PSV.unwrap(al.value) || []) : [];
        loading.value = false;
      }
      onMounted(load);
      return { stats, lowStock, loading, byStaff, foodTotal, openBalances, fKs };
    },
  };

  // ── VIEW: Attendance + Salary advances ──────────────────
  const Attendance = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Attendance" back="/more" />
        <van-cell-group inset style="margin-top:8px">
          <van-field v-model="month" label="Month (YYYY-MM)" placeholder="2026-08" />
          <van-field v-model="staffName" label="Staff" readonly />
        </van-cell-group>
        <div style="display:flex;gap:10px;padding:14px 16px 0">
          <van-button type="primary" block @click="checkin('Present')">Check-In</van-button>
          <van-button type="success" block @click="checkin('Late')">Late</van-button>
        </div>
        <div style="display:flex;gap:10px;padding:10px 16px 0">
          <van-button type="warning" block @click="checkout">Check-Out</van-button>
          <van-button plain block @click="loadToday">Refresh</van-button>
        </div>
        <div class="psv-card psv-rows" style="margin-top:12px">
          <div class="psv-row"><span>Today's status</span><b>{{ today.status || 'Not checked in' }}</b></div>
          <div class="psv-row" v-if="today.check_in || today.check_out"><span>In / Out</span><b>{{ today.check_in || '-' }} / {{ today.check_out || '-' }}</b></div>
        </div>
        <div style="padding:14px 16px">
          <van-button plain block @click="loadAdvances">View Salary Advances</van-button>
        </div>
        <van-cell-group v-if="advances.length" inset>
          <van-cell v-for="a in advances" :key="a.advance_date + a.staff_name" :title="a.staff_name" :label="a.advance_date" :value="fKs(a.amount) + ' Ks'" />
        </van-cell-group>
        <van-empty v-else-if="advancesLoaded" description="No advances" />
      </div>
    `,
    setup() {
      const month = ref(PSV.monthMMT());
      const staffName = ref(PSV.getStaff().name || "");
      const advances = ref([]);
      const advancesLoaded = ref(false);
      const today = reactive({ status: "", check_in: "", check_out: "" });
      function _applyRow(row) {
        if (!row) { today.status = ""; today.check_in = ""; today.check_out = ""; return; }
        today.status = row.status || "";
        today.check_in = row.check_in ? String(row.check_in).slice(11, 16) : "";
        today.check_out = row.check_out ? String(row.check_out).slice(11, 16) : "";
      }
      async function loadToday() {
        try { const d = PSV.unwrap(await PSV.api("/api/attendance/today?staff_name=" + encodeURIComponent(staffName.value))); _applyRow(d && d.today); }
        catch (_e) {}
      }
      async function checkin(status) {
        haptic("medium");
        try {
          const d = PSV.unwrap(await PSV.api("/api/save_attendance", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ staff_name: staffName.value, status }) }));
          _applyRow(d && d.today);
          showToast("Checked in ✓", "success");
        } catch (e) { showToast("Error: " + e.message, "fail"); }
      }
      async function checkout() {
        haptic("medium");
        try {
          const d = PSV.unwrap(await PSV.api("/api/save_attendance", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ staff_name: staffName.value, status: "check-out" }) }));
          _applyRow(d && d.today);
          showToast("Checked out ✓", "success");
        } catch (e) { showToast("Error: " + e.message, "fail"); }
      }
      onMounted(loadToday);
      async function loadAdvances() {
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_salary_advances/" + month.value)); advances.value = (d && d.advances) || []; }
        catch (e) { advances.value = []; }
        advancesLoaded.value = true;
      }
      return { month, staffName, advances, advancesLoaded, today, checkin, checkout, loadToday, loadAdvances, fKs };
    },
  };

  // ── VIEW: OPEX ──────────────────────────────────────────
  const Opex = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="OPEX" back="/more" />
        <van-notice-bar wrapable text="Owner only — OPEX is managed from the dashboard" left-icon="info-o" />
      </div>
    `,
    setup() { return {}; },
  };

  // ── VIEW: Staff ─────────────────────────────────────────
  const Staff = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Staff" back="/more" />
        <van-skeleton title :row="3" v-if="loading" style="margin:12px" />
        <van-empty v-else-if="!list.length" description="No staff" />
        <van-cell-group v-else inset style="margin-top:8px">
          <van-cell v-for="s in list" :key="s.id ?? s.name ?? s.staff_id" :title="s.name || s.staff_name || s.staff_id" :label="s.role || s.position || '-'" />
        </van-cell-group>
      </div>
    `,
    setup() {
      const list = ref([]);
      const loading = ref(true);
      async function load() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_staff")); list.value = Array.isArray(d) ? d : []; }
        catch (e) { list.value = []; }
        finally { loading.value = false; }
      }
      onMounted(load);
      return { list, loading };
    },
  };

  // ── VIEW: Games ─────────────────────────────────────────
  const Games = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Games" back="/more" />
        <van-skeleton title :row="4" v-if="loading" style="margin:12px" />
        <van-empty v-else-if="!list.length" description="No games" />
        <van-cell-group v-else inset style="margin-top:8px">
          <van-cell v-for="g in list" :key="g.game_title ?? g.title ?? g.name ?? g.id" :title="g.game_title || g.title || g.name || g.id" :label="g.genre || g.category || '-'">
            <template #value>
              <van-tag v-if="g.final_status" :type="g.final_status === 'Available' ? 'success' : 'default'" plain>{{ g.final_status }}</van-tag>
              <span v-if="g.disc_count != null" style="margin-left:6px;color:var(--pv-muted)">{{ g.disc_count }} disc</span>
            </template>
          </van-cell>
        </van-cell-group>
      </div>
    `,
    setup() {
      const list = ref([]);
      const loading = ref(true);
      async function load() {
        loading.value = true;
        try {
          let d = PSV.unwrap(await PSV.api("/api/fetch_game_library"));
          list.value = Array.isArray(d) ? d : (d && d.games) || [];
          if (!list.value.length) {
            d = PSV.unwrap(await PSV.api("/api/fetch_games"));
            list.value = Array.isArray(d) ? d : (d && d.games) || [];
          }
        } catch (e) { list.value = []; }
        finally { loading.value = false; }
      }
      onMounted(load);
      return { list, loading };
    },
  };

  // ── VIEW: Food ──────────────────────────────────────────
  const Food = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Food" back="/more" />
        <van-skeleton title :row="4" v-if="loading" style="margin:12px" />
        <van-empty v-else-if="!list.length" description="No food items" />
        <van-cell-group v-else inset style="margin-top:8px">
          <van-cell v-for="(f, i) in list" :key="i" :title="f.item_name || f.name || f.title" :label="f.category || '-'">
            <template #value><span class="pv-amount">{{ fKs(f.price ?? f.sell_price ?? f.unit_price) }} Ks</span></template>
          </van-cell>
        </van-cell-group>
      </div>
    `,
    setup() {
      const list = ref([]);
      const loading = ref(true);
      // Flatten a dict-of-dicts (category -> {item: price}) OR a flat dict (item -> price).
      function flattenFood(d) {
        const out = [];
        if (Array.isArray(d)) return d;
        const obj = d || {};
        for (const k in obj) {
          const v = obj[k];
          if (v && typeof v === "object" && !Array.isArray(v)) {
            // grouped: category -> {item_name: price}
            for (const name in v) {
              out.push({ item_name: name, category: k, price: v[name] });
            }
          } else if (Array.isArray(v)) {
            v.forEach((x) => out.push({ item_name: (x && (x.item_name || x.name)) || x, category: k, price: x && x.price }));
          } else {
            // flat dict: item_name -> price
            out.push({ item_name: k, category: null, price: v });
          }
        }
        return out;
      }
      async function load() {
        loading.value = true;
        try {
          let d = PSV.unwrap(await PSV.api("/api/fetch_food_menu"));
          if (d == null || (typeof d === "object" && !Array.isArray(d) && !Object.keys(d).length)) d = PSV.unwrap(await PSV.api("/api/fetch_food_prices"));
          list.value = flattenFood(d);
        } catch (e) { list.value = []; }
        finally { loading.value = false; }
      }
      onMounted(load);
      return { list, loading, fKs };
    },
  };

  // ── VIEW: Promotions ────────────────────────────────────
  const Promotions = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Promotions" back="/more" />
        <van-skeleton title :row="3" v-if="loading" style="margin:12px" />
        <van-empty v-else-if="!list.length" description="No active promos" />
        <van-cell-group v-else inset style="margin-top:8px">
          <van-cell v-for="p in list" :key="p.id" :title="p.promo_name || p.name" :label="(p.discount_type||'') + ' ' + (p.discount_value ?? '')">
            <template #value><van-tag type="success">Active</van-tag></template>
          </van-cell>
        </van-cell-group>
      </div>
    `,
    setup() {
      const list = ref([]);
      const loading = ref(true);
      async function load() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/promotions/active")); list.value = (d && (d.promotions || d)) || []; }
        catch (e) { list.value = []; }
        finally { loading.value = false; }
      }
      onMounted(load);
      return { list, loading };
    },
  };

  // ── VIEW: Salary ────────────────────────────────────────
  const Salary = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Salary" back="/more" />
        <van-skeleton title :row="3" v-if="loading" style="margin:12px" />
        <template v-else>
          <div v-if="ownerOnly" class="psv-owner-card">
            <div class="psv-owner-card__ico">🔒</div>
            <div class="psv-owner-card__t">Owner only</div>
            <div class="psv-owner-card__s">Requesting or approving a salary advance needs the owner (admin) account. Ask an admin to action it from the dashboard.</div>
            <div v-if="serverMsg" class="psv-owner-card__m">{{ serverMsg }}</div>
          </div>
          <template v-else>
            <van-notice-bar wrapable text="Read-only — advances are settled by an admin" left-icon="info-o" />
            <van-cell-group inset title="This month" style="margin-top:8px">
              <van-cell v-for="a in advances" :key="a.advance_date + a.staff_name" :title="a.staff_name" :label="a.advance_date + ' · ' + (a.repayment_status || '')" :value="fKs(a.amount) + ' Ks'" />
              <van-cell v-if="!advances.length" title="No advances this month" />
            </van-cell-group>
          </template>
        </template>
      </div>
    `,
    setup() {
      const loading = ref(true);
      const ownerOnly = ref(false);
      const serverMsg = ref("");
      const advances = ref([]);
      async function load() {
        loading.value = true;
        try {
          const d = PSV.unwrap(await PSV.api("/api/dashboard/salary/advances"));
          advances.value = (d && (d.advances || d)) || [];
          ownerOnly.value = false;
        } catch (e) {
          const msg = String((e && e.message) || "");
          // 403 → owner-only; 401 → dashboard account required (same wall).
          if (isOwnerBlock(e) || /401|unauthor|user not found/i.test(msg)) { ownerOnly.value = true; serverMsg.value = msg; }
          else { ownerOnly.value = true; serverMsg.value = msg; }
        } finally { loading.value = false; }
      }
      onMounted(load);
      return { loading, ownerOnly, serverMsg, advances, fKs };
    },
  };

  // ── VIEW: Sessions — Move / Swap (WAVE 3) ──────────────
  // The server enforces the real guards (target must be Free for a move; both
  // bookings Active for a swap) and returns success:false with a message. We
  // flag busy destinations up front and surface the server refusal verbatim.
  const SessionsTools = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Sessions" back="/more" />
        <van-tabs v-model:active="tab" sticky>
          <van-tab title="Move">
            <van-pull-refresh v-model="refreshing" @refresh="load">
              <van-skeleton title :row="3" v-if="loading" style="margin:12px" />
              <van-empty v-else-if="!active.length" description="No live sessions" />
              <van-cell-group v-else inset style="margin-top:8px">
                <van-cell v-for="c in active" :key="cid(c)" :title="cid(c)" is-link @click="pickSource(c, 'move')">
                  <template #label>{{ c.current_member || c.customer_name || 'Guest' }}<span v-if="c.current_game"> · {{ c.current_game }}</span></template>
                </van-cell>
              </van-cell-group>
            </van-pull-refresh>
            <div class="psv-note">Pick a live session, then a destination console. A busy destination is flagged and refused by the server.</div>
          </van-tab>
          <van-tab title="Swap">
            <van-pull-refresh v-model="refreshing" @refresh="load">
              <van-skeleton title :row="3" v-if="loading" style="margin:12px" />
              <van-empty v-else-if="active.length < 2" description="Need at least two live sessions" />
              <van-cell-group v-else inset style="margin-top:8px">
                <van-cell v-for="c in active" :key="cid(c)" :title="cid(c)" is-link @click="pickSource(c, 'swap')">
                  <template #label>{{ c.current_member || c.customer_name || 'Guest' }}<span v-if="c.current_game"> · {{ c.current_game }}</span></template>
                </van-cell>
              </van-cell-group>
            </van-pull-refresh>
            <div class="psv-note">Pick two live sessions to swap their consoles.</div>
          </van-tab>
        </van-tabs>

        <van-popup v-model:show="pick" position="bottom" round style="max-height:75vh">
          <div class="psv-sheet">
            <div class="psv-sheet__handle"></div>
            <div class="psv-sheet__body">
              <div class="psv-sheet-title">{{ mode === 'move' ? '🔀 Move to console' : '🔁 Swap with session' }}</div>
              <div class="psv-note" v-if="src">From {{ cid(src) }}<span v-if="src.current_member || src.customer_name"> · {{ src.current_member || src.customer_name }}</span></div>
              <van-empty v-if="!targets.length" :description="mode === 'move' ? 'No other consoles' : 'No other live sessions'" />
              <div v-for="t in targets" :key="cid(t)" class="psv-list-row" @click="choose(t)">
                <div class="psv-list-row__main">
                  <div class="psv-list-row__title">{{ cid(t) }}</div>
                  <div class="psv-list-row__sub">{{ t.status || '-' }}<template v-if="t.current_member || t.customer_name"> · {{ t.current_member || t.customer_name }}</template></div>
                </div>
                <span v-if="mode === 'move' && !isFree(t)" class="psv-pill psv-pill--warn">busy</span>
                <van-icon name="arrow" />
              </div>
            </div>
          </div>
        </van-popup>

        <van-dialog v-model:show="conflict.show" title="Console busy" show-cancel-button confirm-button-text="Continue" @confirm="confirmGo">
          <div style="padding:16px 16px 4px">{{ conflict.msg }}</div>
        </van-dialog>
      </div>
    `,
    setup() {
      const tab = ref(0);
      const list = ref([]);
      const loading = ref(true);
      const refreshing = ref(false);
      const pick = ref(false);
      const mode = ref("move");
      const src = ref(null);
      const targets = ref([]);
      const conflict = reactive({ show: false, msg: "", pending: null });
      function cid(c) { return c && (c.console_id || c.id); }
      function isFree(c) { return String((c && c.status) || "").toLowerCase() === "free"; }
      const active = computed(() => list.value.filter((c) => String(c.status || "").toLowerCase() === "active"));
      async function load() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_console_status")); list.value = (d && d.consoles) || []; }
        catch (e) { showToast("Load failed: " + e.message, "fail"); list.value = []; }
        finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(load);
      function pickSource(c, m) {
        haptic("light"); mode.value = m; src.value = c;
        targets.value = m === "move"
          ? list.value.filter((x) => cid(x) !== cid(c))
          : active.value.filter((x) => cid(x) !== cid(c));
        pick.value = true;
      }
      function choose(t) {
        if (!src.value) return;
        if (mode.value === "swap") { doSwap(t); return; }
        if (!isFree(t)) {
          conflict.msg = "Console " + cid(t) + " is " + (t.status || "busy") + ". The move will be refused by the server.";
          conflict.pending = () => doMove(t);
          conflict.show = true; haptic("medium"); return;
        }
        doMove(t);
      }
      function confirmGo() { const f = conflict.pending; conflict.pending = null; if (f) f(); }
      async function doMove(t) {
        pick.value = false; haptic("medium");
        try {
          asOk(await PSV.api("/api/sessions/move", { method: "POST", headers: HJSON, body: JSON.stringify({ booking_id: src.value.booking_id, to_console_id: cid(t) }) }));
          showToast("Moved to " + cid(t) + " ✓", "success"); load();
        } catch (e) { showToast(String(e.message || "Move failed"), "fail"); }
      }
      async function doSwap(t) {
        pick.value = false; haptic("medium");
        try {
          asOk(await PSV.api("/api/sessions/swap", { method: "POST", headers: HJSON, body: JSON.stringify({ booking_id_1: src.value.booking_id, booking_id_2: t.booking_id }) }));
          showToast("Sessions swapped ✓", "success"); load();
        } catch (e) { showToast(String(e.message || "Swap failed"), "fail"); }
      }
      return { tab, loading, refreshing, pick, mode, src, targets, active, conflict, cid, isFree, pickSource, choose, confirmGo, load };
    },
  };

  // ── VIEW: Stock-out — manual deduction (WAVE 3) ─────────
  const StockOut = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Stock-out" back="/more" />
        <div class="psv-pad">
          <div class="psv-field-card">
            <div class="psv-field-label">Item</div>
            <div class="psv-field psv-field--tap" @click="openPicker">
              <span v-if="item.id" class="psv-field__value">{{ item.item_name }} <span class="psv-field__sub">· {{ item.quantity }} in stock</span></span>
              <span v-else class="psv-field__ph">Select an item…</span>
              <van-icon name="arrow" />
            </div>
          </div>
          <div class="psv-field-card">
            <div class="psv-field-label">Quantity</div>
            <input v-model.number="qty" class="psv-input" type="number" inputmode="numeric" placeholder="0" />
          </div>
          <div class="psv-field-card">
            <div class="psv-field-label">Reason / note</div>
            <input v-model="reason" class="psv-input" type="text" placeholder="e.g. damaged, staff meal" />
          </div>
          <div class="psv-card psv-receipt">
            <div class="psv-row"><span>Item</span><b>{{ item.item_name || '—' }}</b></div>
            <div class="psv-row"><span>Quantity</span><b>{{ Number(qty) || 0 }}</b></div>
            <div class="psv-row"><span>Unit price</span><b class="psv-tabnum">{{ fKs(item.unit_price) }} Ks</b></div>
            <div class="psv-row psv-receipt__total"><span>Value</span><b class="psv-tabnum">{{ fKs((Number(qty) || 0) * (Number(item.unit_price) || 0)) }} Ks</b></div>
          </div>
          <button class="psv-btn psv-btn--primary psv-btn--block" :disabled="busy || !item.id || !(Number(qty) > 0)" @click="submit">
            <span v-if="!busy">Record stock-out</span><span v-else>Working…</span>
          </button>
        </div>
        <van-popup v-model:show="picker" position="bottom" round style="max-height:70vh">
          <div class="psv-sheet">
            <div class="psv-sheet__handle"></div>
            <van-search v-model="q" placeholder="Search item" />
            <div class="psv-sheet__body">
              <div v-if="!filtered.length" class="psv-empty">No items</div>
              <div v-for="s in filtered" :key="s.id" class="psv-list-row" @click="pick(s)">
                <div class="psv-list-row__main"><div class="psv-list-row__title">{{ s.item_name }}</div><div class="psv-list-row__sub">{{ s.category || '-' }} · {{ s.quantity }} in stock</div></div>
                <van-icon name="arrow" />
              </div>
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup() {
      const picker = ref(false);
      const q = ref("");
      const stock = ref([]);
      const item = reactive({ id: "", item_name: "", quantity: 0, unit_price: 0 });
      const qty = ref(0);
      const reason = ref("");
      const busy = ref(false);
      const filtered = computed(() => {
        const s = q.value.trim().toLowerCase();
        if (!s) return stock.value;
        return stock.value.filter((x) => String(x.item_name || "").toLowerCase().includes(s));
      });
      async function openPicker() {
        haptic("light"); picker.value = true;
        if (!stock.value.length) {
          try { const d = PSV.unwrap(await PSV.api("/api/dashboard/inventory")); stock.value = Array.isArray(d) ? d : ((d && d.items) || []); }
          catch (e) { showToast("Load failed: " + e.message, "fail"); }
        }
      }
      function pick(s) { item.id = s.id; item.item_name = s.item_name; item.quantity = Number(s.quantity) || 0; item.unit_price = Number(s.unit_price) || 0; picker.value = false; haptic("light"); }
      async function submit() {
        const n = Number(qty.value) || 0;
        if (!item.id || n <= 0 || busy.value) return;
        busy.value = true; haptic("medium");
        try {
          const r = asOk(await PSV.api("/api/dashboard/stock-out", { method: "POST", headers: HJSON, body: JSON.stringify({ item_id: item.id, quantity: n, unit_price: item.unit_price, notes: reason.value, staff_name: PSV.getStaff().name }) }));
          const data = PSV.unwrap(r);
          const nq = data && data.new_quantity;
          showToast("Stock-out recorded ✓" + (nq != null ? (" · new qty " + nq) : ""), "success");
          if (nq != null) item.quantity = nq;
          qty.value = 0; reason.value = "";
        } catch (e) { showToast(String(e.message || "Stock-out failed"), "fail"); }
        finally { busy.value = false; }
      }
      return { picker, q, stock, item, qty, reason, busy, filtered, openPicker, pick, submit, fKs };
    },
  };

  // ── VIEW: Low-stock alerts (WAVE 3) ─────────────────────
  const StockAlerts = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Low-stock alerts" back="/more" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <van-skeleton title :row="3" v-if="loading" style="margin:12px" />
          <van-empty v-else-if="!list.length" description="No low-stock items" />
          <van-cell-group v-else inset style="margin-top:8px">
            <van-cell v-for="a in list" :key="a.id" :title="a.item_name" :label="'Qty ' + a.quantity + ' / reorder ' + a.reorder_level + ' · short ' + a.shortage">
              <template #value><van-tag :type="a.status === 'critical' ? 'danger' : 'warning'">{{ a.status }}</van-tag></template>
            </van-cell>
          </van-cell-group>
        </van-pull-refresh>
      </div>
    `,
    setup() {
      const list = ref([]); const loading = ref(true); const refreshing = ref(false);
      async function load() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/dashboard/inventory/alerts")); list.value = Array.isArray(d) ? d : ((d && d.items) || []); }
        catch (e) { showToast("Load failed: " + e.message, "fail"); list.value = []; }
        finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(load);
      return { list, loading, refreshing, load };
    },
  };

  // ── VIEW: Stock check — reconcile (WAVE 3) ──────────────
  const StockCheck = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Stock check" back="/more" />
        <van-skeleton title :row="4" v-if="loading" style="margin:12px" />
        <template v-else>
          <div class="psv-card psv-rows" style="margin-top:8px">
            <div class="psv-row"><span>Date</span><b>{{ date }}</b></div>
            <div class="psv-row"><span>Progress</span><b>{{ checked }} / {{ items.length }} ({{ pct }}%)</b></div>
          </div>
          <van-cell-group inset style="margin-top:10px">
            <van-cell v-for="it in items" :key="it.item_name" center>
              <template #title>
                <span>{{ it.item_name }}</span>
                <span class="psv-check__sys">sys {{ it.system_qty }}</span>
                <span v-if="variance(it) !== null" class="psv-check__var" :class="variance(it) === 0 ? 'is-ok' : 'is-bad'">Δ {{ variance(it) }}</span>
              </template>
              <template #right-icon>
                <input class="psv-check__in" type="number" inputmode="numeric" :value="entry(it)" placeholder="—" @input="setEntry(it, $event)" />
              </template>
            </van-cell>
          </van-cell-group>
          <div style="padding:14px 16px">
            <button class="psv-btn psv-btn--primary psv-btn--block" :disabled="busy || !dirtyCount" @click="submit">
              <span v-if="!busy">Save {{ dirtyCount }} count{{ dirtyCount === 1 ? '' : 's' }}</span><span v-else>Saving…</span>
            </button>
          </div>
        </template>
      </div>
    `,
    setup() {
      const items = ref([]);
      const loading = ref(true);
      const busy = ref(false);
      const date = ref("");
      const counts = reactive({});
      const entry = (it) => (counts[it.item_name] != null ? counts[it.item_name] : (it.actual_qty != null ? String(it.actual_qty) : ""));
      const checked = computed(() => items.value.filter((it) => entry(it) !== "").length);
      const pct = computed(() => items.value.length ? Math.round(checked.value / items.value.length * 100) : 0);
      const dirtyCount = computed(() => Object.keys(counts).filter((k) => counts[k] !== "" && counts[k] != null).length);
      function setEntry(it, e) { counts[it.item_name] = e.target.value; }
      function variance(it) { const e = entry(it); if (e === "" || e == null) return null; return Number(it.system_qty) - Number(e); }
      async function load() {
        loading.value = true;
        try { const d = PSV.unwrap(await PSV.api("/api/dashboard/stock-check")); items.value = (d && d.items) || []; date.value = (d && d.date) || PSV.todayMMT(); }
        catch (e) { showToast("Load failed: " + e.message, "fail"); items.value = []; }
        finally { loading.value = false; }
      }
      onMounted(load);
      async function submit() {
        const payload = [];
        for (const name in counts) { const v = counts[name]; if (v === "" || v == null) continue; payload.push({ item_name: name, actual_qty: Number(v) }); }
        if (!payload.length) { showToast("Enter at least one count", "warning"); return; }
        busy.value = true; haptic("medium");
        try {
          asOk(await PSV.api("/api/dashboard/stock-check", { method: "POST", headers: HJSON, body: JSON.stringify({ date: date.value, items: payload, checked_by: PSV.getStaff().name }) }));
          showToast("Saved " + payload.length + " count(s) ✓", "success");
          Object.keys(counts).forEach((k) => delete counts[k]);
          load();
        } catch (e) { showToast(String(e.message || "Save failed"), "fail"); }
        finally { busy.value = false; }
      }
      return { items, loading, busy, date, checked, pct, dirtyCount, entry, setEntry, variance, submit };
    },
  };

  // ── VIEW: Account balances — read-only (WAVE 3) ────────
  const Balances = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Account balances" back="/more" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <van-skeleton title :row="4" v-if="loading" style="margin:12px 16px" />
          <template v-else>
            <div v-if="blocked" class="psv-owner-card">
              <div class="psv-owner-card__ico">🔒</div>
              <div class="psv-owner-card__t">Owner-only</div>
              <div class="psv-owner-card__s">Owner-only — ask the owner to view these account balances.</div>
            </div>
            <van-empty v-else-if="err" image="error" :description="err" />
            <template v-else>
              <div class="psv-readonly">🔒 Read-only — balances shown for reference only</div>
              <div class="psv-acc-grid">
                <div v-for="a in accounts" :key="a.label" class="psv-acc-card">
                  <div class="psv-acc-card__top">
                    <span class="psv-acc-card__ico">{{ a.icon }}</span>
                    <span class="psv-acc-card__label">{{ a.label }}</span>
                    <span v-if="a.owner_only" class="psv-soon" style="margin-left:auto">Owner only</span>
                    <span v-else class="psv-acc-card__type">{{ a.type || 'Account' }}</span>
                  </div>
                  <div class="psv-acc-card__bal psv-tabnum">{{ fKs(a.balance) }} <span class="psv-acc-card__unit">Ks</span></div>
                  <div class="psv-acc-card__meta">{{ a.last ? ('Updated ' + fmtWhen(a.last)) : (a.owner_only ? 'Owner account' : 'Shop account') }}</div>
                </div>
              </div>
              <van-empty v-if="!accounts.length" image="search" description="No accounts to show" />
              <div class="psv-note" style="text-align:center">
                {{ isAdmin ? 'All accounts — owner-only accounts are marked.' : 'Shop accounts only. Owner-only accounts need the owner.' }}
              </div>
            </template>
          </template>
        </van-pull-refresh>
      </div>
    `,
    setup() {
      const accounts = ref([]); const loading = ref(true); const refreshing = ref(false);
      const blocked = ref(false); const err = ref(""); const role = ref("");
      const isAdmin = computed(() => String(role.value || "").toLowerCase() === "admin");
      function mapRows(rows, fallbackOwnerOnly) {
        return (rows || []).map((r) => {
          const label = r.label || r.name || r.account || "Account";
          return {
            key: label, label: label, icon: accIcon(label),
            type: r.type || (r.owner_only ? "Owner-only" : ""),
            balance: Number(r.balance) || 0,
            owner_only: !!(r.owner_only || r.ownerOnly || fallbackOwnerOnly),
            last: r.last_movement || r.last_updated || null,
          };
        });
      }
      async function load() {
        loading.value = true; err.value = ""; blocked.value = false;
        // Role-aware endpoint — the server decides which accounts a token may see
        // (staff → 4 shop accounts, admin → all 6 with owner_only flags).
        // We render exactly what comes back; no client-side role guessing.
        try {
          const d = PSV.unwrap(await PSV.api("/api/accounts/balances")) || {};
          role.value = d.role || role.value;
          let rows = d.accounts || [];
          if (!rows.length) rows = [].concat(d.operating || [], d.capital || []);
          accounts.value = mapRows(rows);
        } catch (e) {
          const m = String((e && e.message) || e || "");
          // 403 → the role is not allowed this view at all.
          if (isOwnerBlock(e) || /403|owner|admin|not permitted|401|unauthor|user not found/i.test(m)) {
            blocked.value = true; accounts.value = []; return;
          }
          try {
            const t = PSV.unwrap(await PSV.api("/api/accounts/transferable")) || {};
            role.value = t.role || role.value;
            accounts.value = mapRows(t.accounts || []);
          } catch (e2) {
            try {
              const f = PSV.unwrap(await PSV.api("/api/finance/account-balances")) || {};
              accounts.value = mapRows([].concat(f.operating || [], f.capital || []), true);
            } catch (e3) {
              accounts.value = [];
              err.value = String((e3 && e3.message) || m || "Could not load balances — pull to refresh");
            }
          }
        } finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(load);
      return { accounts, loading, refreshing, blocked, err, role, isAdmin, load, fKs, fmtWhen };
    },
  };

  // ══ WAVE 5 — payment-account helpers ═══════════════════════
  const ACC_ICONS = { "Cash": "💰", "WavePay": "📱", "KPay": "💳", "AYA Pay": "💳", "ACM's Acc": "🏦", "KBZ Bank": "🏦" };
  const accIcon = (name) => ACC_ICONS[String(name || "")] || "🏦";
  function fmtWhen(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso);
      return d.toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch (_e) { return String(iso); }
  }

  // ── VIEW: Account Balances — owner-only, ACM's Acc + KBZ Bank (WAVE 5) ──
  // Endpoint: GET /api/finance/account-balances  (403 for staff). Exactly two cards.
  const AccountBalances = {
    components: { PageHeader },
    template: `
      <div class="pv-page" style="min-height:72vh">
        <PageHeader title="Account Balances" back="/more" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <van-skeleton title :row="4" v-if="loading" style="margin:12px 16px" />
          <template v-else>
            <div v-if="blocked" class="psv-owner-card">
              <div class="psv-owner-card__ico">🔒</div>
              <div class="psv-owner-card__t">Owner-only</div>
              <div class="psv-owner-card__s">Owner-only — ask the owner to view these account balances.</div>
              <div class="psv-owner-card__m">ACM's Acc &amp; KBZ Bank are restricted to the owner / admin account.</div>
            </div>
            <van-empty v-else-if="err" image="error" :description="err" />
            <template v-else>
              <div class="psv-acc-grid">
                <div v-for="a in accounts" :key="a.label" class="psv-acc-card">
                  <div class="psv-acc-card__top">
                    <span class="psv-acc-card__ico">{{ a.icon }}</span>
                    <span class="psv-acc-card__label">{{ a.label }}</span>
                    <span class="psv-acc-card__type">{{ a.type || 'Account' }}</span>
                  </div>
                  <div class="psv-acc-card__bal psv-tabnum">{{ fKs(a.balance) }} <span class="psv-acc-card__unit">Ks</span></div>
                  <div class="psv-acc-card__meta">{{ a.last ? ('Updated ' + fmtWhen(a.last)) : 'Owner account' }}</div>
                </div>
              </div>
              <div class="psv-readonly">🔒 Owner-only — restricted account balances</div>
            </template>
          </template>
        </van-pull-refresh>
      </div>
    `,
    setup() {
      const accounts = ref([]); const loading = ref(true); const refreshing = ref(false);
      const blocked = ref(false); const err = ref("");
      async function load() {
        loading.value = true; err.value = "";
        try {
          // Canonical owner-only endpoint. Falls back to the /api/accounts/balances alias.
          let d = null;
          try { d = PSV.unwrap(await PSV.api("/api/finance/account-balances")); }
          catch (e1) {
            if (isOwnerBlock(e1)) throw e1;
            d = PSV.unwrap(await PSV.api("/api/accounts/balances"));
          }
          let rows = (d && d.accounts) || [];
          if (!rows.length) rows = [].concat((d && d.operating) || [], (d && d.capital) || []);
          // Contract: ONLY ACM's Acc + KBZ Bank — enforce even if the server ever widens.
          rows = rows.filter((r) => /ACM|KBZ/i.test(String((r && (r.label || r.name || r.account)) || "")));
          accounts.value = rows.map((r) => {
            const label = r.label || r.name || r.account || "Account";
            return { label: label, icon: accIcon(label), type: r.type || "", balance: Number(r.balance) || 0, last: r.last_movement || r.last_updated || null };
          });
          blocked.value = false;
        } catch (e) {
          accounts.value = [];
          const m = String((e && e.message) || e || "");
          if (isOwnerBlock(e) || /403|owner|admin|401|unauthor|not permitted|user not found/i.test(m)) blocked.value = true;
          else err.value = m || "Could not load balances — pull to refresh";
        } finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(load);
      return { accounts, loading, refreshing, blocked, err, load, fKs, fmtWhen };
    },
  };

  // ── VIEW: Transfer — move money between payment accounts (WAVE 5) ──
  // From/To are whatever /api/accounts/transferable returns (role-filtered server-side).
  // The client NEVER hardcodes the role rules — it renders the returned list verbatim.
  const TransferScreen = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Transfer" back="/more" />

        <van-skeleton title :row="4" v-if="loading" style="margin:12px 16px" />

        <template v-else-if="done">
          <div class="psv-xfer-done">
            <div class="psv-xfer-done__ico">✅</div>
            <div class="psv-xfer-done__t">Transfer complete</div>
            <div class="psv-xfer-done__ref">Ref {{ done.ref }}</div>
            <div class="psv-card psv-rows psv-xfer-done__bal">
              <div class="psv-row"><span>{{ done.from_account }}</span><b class="psv-tabnum">{{ fKs(done.from_balance) }} Ks</b></div>
              <div class="psv-row"><span>{{ done.to_account }}</span><b class="psv-tabnum">{{ fKs(done.to_balance) }} Ks</b></div>
            </div>
            <div class="psv-note" style="text-align:center">{{ fKs(done.amount) }} Ks moved from {{ done.from_account }} to {{ done.to_account }}.</div>
            <div class="psv-pad" style="padding-top:0">
              <button class="psv-btn psv-btn--primary psv-btn--block" @click="transferAgain">Transfer again</button>
              <button class="psv-btn psv-btn--ghost psv-btn--block" style="margin-top:10px" @click="goBack">Back to menu</button>
            </div>
          </div>
        </template>

        <template v-else-if="loadErr">
          <div class="psv-pad">
            <div class="psv-owner-card" style="margin:0">
              <div class="psv-owner-card__ico">🔄</div>
              <div class="psv-owner-card__t">Not available yet</div>
              <div class="psv-owner-card__s">{{ loadErr }}</div>
            </div>
            <button class="psv-btn psv-btn--ghost psv-btn--block" style="margin-top:14px" @click="load">Retry</button>
          </div>
        </template>

        <template v-else>
          <div class="psv-pad">
            <div v-if="errMsg" class="psv-err-banner">{{ errMsg }}</div>

            <div class="psv-field-card">
              <div class="psv-field-label">From account</div>
              <div class="psv-xfer-acct">
                <span class="psv-xfer-acct__ico">{{ iconOf(from) }}</span>
                <span class="psv-xfer-acct__name">{{ from || 'Select account' }}</span>
                <span class="psv-xfer-acct__bal psv-tabnum">{{ balOf(from) != null ? fKs(balOf(from)) + ' Ks' : '—' }}</span>
              </div>
              <div class="psv-xfer-actions">
                <button class="psv-xfer-mini" @click="pick('from')">Change</button>
                <button class="psv-xfer-swap" @click="swap" aria-label="Swap From and To">⇅ Swap</button>
              </div>
            </div>

            <div class="psv-field-card">
              <div class="psv-field-label">To account</div>
              <div class="psv-xfer-acct">
                <span class="psv-xfer-acct__ico">{{ iconOf(to) }}</span>
                <span class="psv-xfer-acct__name">{{ to || 'Select account' }}</span>
                <span class="psv-xfer-acct__bal psv-tabnum">{{ balOf(to) != null ? fKs(balOf(to)) + ' Ks' : '—' }}</span>
              </div>
              <div class="psv-xfer-actions">
                <button class="psv-xfer-mini" @click="pick('to')">Change</button>
              </div>
            </div>

            <div class="psv-field-card">
              <div class="psv-field-label">Amount (Ks)</div>
              <input class="psv-input psv-tabnum" type="tel" inputmode="numeric" v-model="amountStr" placeholder="0" />
              <div class="psv-xfer-quick" v-if="quick.length">
                <button v-for="q in quick" :key="q" class="psv-chip" @click="amountStr = String(q)">{{ fKs(q) }}</button>
              </div>
            </div>

            <div class="psv-field-card">
              <div class="psv-field-label">Note (optional)</div>
              <input class="psv-input psv-input--text" type="text" v-model="note" placeholder="e.g. cash float top-up" />
            </div>

            <div class="psv-card psv-rows" v-if="from && to && amount > 0">
              <div class="psv-section-title" style="margin:2px 2px 6px">After transfer</div>
              <div class="psv-row"><span>{{ from }}</span><b class="psv-tabnum">{{ fKs(fromAfter) }} Ks</b></div>
              <div class="psv-row"><span>{{ to }}</span><b class="psv-tabnum">{{ fKs(toAfter) }} Ks</b></div>
            </div>

            <button class="psv-btn psv-btn--primary psv-btn--block" :disabled="!canReview" @click="openConfirm">Review transfer</button>
            <div class="psv-note">You can move money between: {{ allowedNames }}. Final balances are confirmed by the server.</div>
          </div>
        </template>

        <van-popup v-model:show="pickShow" position="bottom" round>
          <van-picker :columns="columns" :title="pickWhich === 'from' ? 'From account' : 'To account'" @confirm="onPick" @cancel="pickShow = false" />
        </van-popup>

        <van-popup v-model:show="confirmShow" position="bottom" round :close-on-click-overlay="!busy">
          <div class="psv-sheet">
            <div class="psv-sheet__handle"></div>
            <div class="psv-sheet__body">
              <div class="psv-sheet-title">Confirm transfer</div>
              <div class="psv-xfer-confirm">
                <div class="psv-xfer-flow">
                  <span class="psv-xfer-flow__a">{{ from }}</span>
                  <span class="psv-xfer-flow__arrow">→</span>
                  <span class="psv-xfer-flow__b">{{ to }}</span>
                </div>
                <div class="psv-xfer-big psv-tabnum">{{ fKs(amount) }} <span>Ks</span></div>
                <div class="psv-xfer-newbal">
                  This moves <b>{{ fKs(amount) }} Ks</b> from <b>{{ from }}</b> to <b>{{ to }}</b>.
                  <template v-if="note"><br>Note: {{ note }}</template>
                  <br>New balances: <b>{{ from }}</b> {{ fKs(fromAfter) }} Ks · <b>{{ to }}</b> {{ fKs(toAfter) }} Ks
                </div>
              </div>
              <div v-if="errMsg" class="psv-err-banner" style="margin-top:10px">{{ errMsg }}</div>
              <button class="psv-btn psv-btn--primary psv-btn--block" :disabled="busy" @click="doTransfer">{{ busy ? 'Transferring…' : 'Confirm transfer' }}</button>
              <button class="psv-btn psv-btn--ghost psv-btn--block" style="margin-top:10px" :disabled="busy" @click="confirmShow = false">Cancel</button>
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup() {
      const loading = ref(true); const refreshing = ref(false);
      const accounts = ref([]); const role = ref("staff");
      const from = ref(""); const to = ref("");
      const amountStr = ref(""); const note = ref("");
      const pickShow = ref(false); const pickWhich = ref("from");
      const confirmShow = ref(false); const busy = ref(false);
      const errMsg = ref(""); const loadErr = ref(""); const done = ref(null);
      // One idempotency key per attempt: a double-tap cannot double-post. A NEW key is
      // minted on load, after success, and after any error (i.e. on every new attempt).
      const clientRef = ref(PSV.genClientRef());

      const amount = computed(() => { const n = Math.floor(Number(amountStr.value) || 0); return n > 0 ? n : 0; });
      const balances = computed(() => { const m = {}; accounts.value.forEach((a) => { m[a.label] = Number(a.balance) || 0; }); return m; });
      function balOf(name) { return (name && (name in balances.value)) ? balances.value[name] : null; }
      function iconOf(name) { return accIcon(name); }
      const fromAfter = computed(() => Math.max(0, (balOf(from.value) || 0) - amount.value));
      const toAfter = computed(() => (balOf(to.value) || 0) + amount.value);
      const columns = computed(() => accounts.value.map((a) => ({ text: a.label + (a.owner_only ? " (owner)" : ""), value: a.label })));
      const quick = computed(() => {
        const b = balOf(from.value) || 0;
        return [5000, 10000, 20000, 50000, 100000, 200000].filter((x) => x <= b).slice(-4);
      });
      const allowedNames = computed(() => accounts.value.map((a) => a.label).join(", ") || "—");
      // Review is allowed whenever the form is plausibly complete; the insufficient-balance
      // check happens in openConfirm() so the user gets a clear message (not a dead button).
      const canReview = computed(() => !!from.value && !!to.value && from.value !== to.value && amount.value > 0);

      async function load() {
        loading.value = true; loadErr.value = "";
        try {
          const raw = await PSV.api("/api/accounts/transferable");
          const d = PSV.unwrap(raw) || {};
          const list = Array.isArray(d) ? d : (d.accounts || []);
          accounts.value = list.map((a) => {
            const label = a.label || a.account || a.key || "Account";
            return { label: label, balance: Number(a.balance) || 0, owner_only: !!(a.owner_only || a.ownerOnly || /ACM|KBZ/i.test(label)) };
          });
          role.value = d.role || ((PSV.getStaff() || {}).role) || "staff";
          if (!from.value || !accounts.value.some((a) => a.label === from.value)) from.value = accounts.value[0] ? accounts.value[0].label : "";
          if (!to.value || !accounts.value.some((a) => a.label === to.value) || to.value === from.value) {
            const other = accounts.value.find((a) => a.label !== from.value);
            to.value = other ? other.label : "";
          }
          errMsg.value = "";
        } catch (e) {
          const m = String((e && e.message) || e || "");
          loadErr.value = /not found|404|not available|405/i.test(m) ? "This screen is not available yet — please try again shortly." : (m || "Could not load accounts");
        } finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(load);

      function pick(which) { pickWhich.value = which; pickShow.value = true; haptic("light"); }
      function onPick({ selectedOptions }) {
        if (!selectedOptions || !selectedOptions[0]) { pickShow.value = false; return; }
        const val = selectedOptions[0].value;
        if (pickWhich.value === "from") {
          if (val === to.value) to.value = from.value; // never allow from == to
          from.value = val;
        } else {
          if (val === from.value) from.value = to.value;
          to.value = val;
        }
        pickShow.value = false; errMsg.value = "";
      }
      function swap() { const t = from.value; from.value = to.value; to.value = t; errMsg.value = ""; haptic("light"); }

      function openConfirm() {
        errMsg.value = "";
        if (from.value === to.value) { errMsg.value = "From and To must be different accounts."; return; }
        if (amount.value <= 0) { errMsg.value = "Enter an amount greater than 0."; return; }
        if (amount.value > (balOf(from.value) || 0)) { errMsg.value = "Insufficient balance in " + from.value + " — available " + fKs(balOf(from.value) || 0) + " Ks."; return; }
        haptic("light"); confirmShow.value = true;
      }

      function friendlyTransferError(e) {
        const m = String((e && e.message) || e || "Transfer failed");
        if (/insufficient/i.test(m)) return m + " — reduce the amount or choose another account.";
        if (/owner-only|not permitted|not allowed|403|owner only/i.test(m)) return "Not allowed — that account is owner-only for your role.";
        if (/must be different|same account/i.test(m)) return "From and To must be different accounts.";
        if (/duplicate|client_ref|409/i.test(m)) return "This transfer was already submitted — check history before retrying.";
        if (/not found|404|not available|405/i.test(m)) return "Transfers are not available yet — please try again shortly.";
        return m;
      }

      async function doTransfer() {
        if (busy.value) return;
        busy.value = true; errMsg.value = "";
        const payload = {
          from_account: from.value, to_account: to.value, amount: amount.value,
          note: note.value || "", staff_name: (PSV.getStaff() || {}).name || "", client_ref: clientRef.value,
        };
        try {
          const raw = await PSV.api("/api/accounts/transfer", { method: "POST", headers: HJSON, body: JSON.stringify(payload) });
          const d = PSV.unwrap(raw) || {};
          const bl = d.balances || {};
          const fromBal = (bl[from.value] != null) ? bl[from.value] : (d.from_balance != null ? d.from_balance : fromAfter.value);
          const toBal = (bl[to.value] != null) ? bl[to.value] : (d.to_balance != null ? d.to_balance : toAfter.value);
          done.value = {
            ref: d.ref || d.reference || "-", amount: Number(d.amount || amount.value),
            from_account: d.from_account || from.value, to_account: d.to_account || to.value,
            from_balance: Number(fromBal) || 0, to_balance: Number(toBal) || 0,
          };
          confirmShow.value = false;
          try { PSV.invalidate("/api/accounts"); } catch (_e) {}
        } catch (e) {
          errMsg.value = friendlyTransferError(e);
          clientRef.value = PSV.genClientRef(); // failed attempt done → fresh key next try
        } finally { busy.value = false; }
      }

      function transferAgain() { done.value = null; amountStr.value = ""; note.value = ""; errMsg.value = ""; clientRef.value = PSV.genClientRef(); load(); haptic("light"); }
      function goBack() { router.push("/more"); }

      return {
        loading, refreshing, accounts, role, from, to, amountStr, note, pickShow, pickWhich, confirmShow, busy,
        errMsg, loadErr, done, amount, quick, columns, allowedNames, canReview, fromAfter, toAfter,
        balOf, iconOf, pick, onPick, swap, openConfirm, doTransfer, transferAgain, goBack, load, fKs, fmtWhen,
      };
    },
  };

  // ── VIEW: Transfer history (WAVE 6) ─────────────────────
  // Rows: date/time · from → to · amount · staff · ref — newest first.
  // Source: GET /api/accounts/transfers?limit=50 (admin: all, staff: own only).
  // The scope chip reflects the role the server scopes by, so a staff token never
  // implies they can see other staff's transfers.
  const TransferHistory = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Transfer history" back="/more" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <van-skeleton title :row="5" v-if="loading" style="margin:12px 16px" />
          <template v-else>
            <div v-if="unavailable" class="psv-owner-card">
              <div class="psv-owner-card__ico">🧾</div>
              <div class="psv-owner-card__t">Not available yet</div>
              <div class="psv-owner-card__s">{{ unavailable }}</div>
              <button class="psv-btn psv-btn--ghost psv-btn--block" style="margin-top:14px" @click="load">Retry</button>
            </div>
            <van-empty v-else-if="err" image="error" :description="err" />
            <template v-else>
              <div class="psv-scope" :class="isAdmin ? 'psv-scope--all' : 'psv-scope--mine'">
                <span class="psv-scope__ico">{{ isAdmin ? '🏦' : '👤' }}</span>
                <span class="psv-scope__t">{{ isAdmin ? 'All staff transfers' : 'Your transfers only' }}</span>
                <span class="psv-scope__n">{{ rows.length }}</span>
              </div>
              <van-empty v-if="!rows.length" image="search" description="No transfers yet" />
              <div v-else class="psv-th-list">
                <div v-for="t in rows" :key="t.id || t.ref" class="psv-th-row">
                  <div class="psv-th-row__top">
                    <span class="psv-th-row__flow">
                      <span class="psv-th-row__ico">{{ iconOf(t.from_account) }}</span>
                      <b>{{ t.from_account }}</b>
                      <span class="psv-th-row__arrow">→</span>
                      <span class="psv-th-row__ico">{{ iconOf(t.to_account) }}</span>
                      <b>{{ t.to_account }}</b>
                    </span>
                    <span class="psv-th-row__amt psv-tabnum">{{ fKs(t.amount) }} Ks</span>
                  </div>
                  <div class="psv-th-row__meta">
                    <span>{{ fmtWhen(t.created_at) }}</span>
                    <span v-if="t.staff_name">· {{ t.staff_name }}</span>
                    <span class="psv-th-row__ref">· {{ t.ref }}</span>
                  </div>
                </div>
              </div>
              <div class="psv-note" style="text-align:center">{{ isAdmin ? 'Showing every account transfer.' : 'Showing transfers you created.' }}</div>
            </template>
          </template>
        </van-pull-refresh>
      </div>
    `,
    setup() {
      const rows = ref([]); const loading = ref(true); const refreshing = ref(false);
      const err = ref(""); const unavailable = ref(""); const role = ref("");
      const isAdmin = computed(() => String(role.value || "").toLowerCase() === "admin");
      function normalize(list) {
        const arr = Array.isArray(list) ? list.slice() : [];
        // Newest first (defensive: never trust server ordering).
        arr.sort((a, b) => {
          const ta = Date.parse(a && a.created_at) || 0, tb = Date.parse(b && b.created_at) || 0;
          if (tb !== ta) return tb - ta;
          return (Number(b && b.id) || 0) - (Number(a && a.id) || 0);
        });
        return arr.map((t) => ({
          id: t.id, ref: t.ref || "-",
          from_account: t.from_account || "?", to_account: t.to_account || "?",
          amount: Number(t.amount) || 0,
          staff_name: t.staff_name || "", created_at: t.created_at || "",
        }));
      }
      async function load() {
        loading.value = true; err.value = ""; unavailable.value = "";
        try {
          const d = PSV.unwrap(await PSV.api("/api/accounts/transfers?limit=50")) || {};
          role.value = d.role || d.scope || role.value || String((PSV.getStaff() || {}).role || "");
          rows.value = normalize(d.transfers || d.rows || (Array.isArray(d) ? d : []));
        } catch (e) {
          const m = String((e && e.message) || e || "");
          rows.value = [];
          if (/not found|404|405|not available|not implemented/i.test(m)) unavailable.value = "Transfer history is not available yet — please try again shortly.";
          else if (isOwnerBlock(e) || /403|owner|admin|401|unauthor|not permitted/i.test(m)) unavailable.value = "Owner-only — ask the owner.";
          else err.value = m || "Could not load transfer history — pull to refresh";
        } finally { loading.value = false; refreshing.value = false; }
      }
      onMounted(load);
      return { rows, loading, refreshing, err, unavailable, role, isAdmin, load, fKs, fmtWhen, iconOf: (n) => accIcon(n) };
    },
  };

  // ── VIEW: Gift / Free card (WAVE 3) ─────────────────────
  const GiftCard = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Gift / Free card" back="/more" />
        <van-notice-bar wrapable text="Grants free minutes with no payment. Requires the owner PIN." left-icon="info-o" />
        <van-cell-group inset style="margin-top:8px">
          <van-field v-model="form.member_id" label="Member ID" placeholder="auto" />
          <van-field v-model="form.name" label="Name" placeholder="Name" />
          <van-field v-model="form.phone" label="Phone" type="tel" placeholder="09..." />
          <van-field v-model.number="form.initial_mins" type="number" label="Free minutes" placeholder="0" />
          <van-field v-model="form.pin" type="password" label="Owner PIN" placeholder="Enter owner PIN" autocomplete="off" />
        </van-cell-group>
        <div class="psv-note">A wrong PIN is rejected with 403 and nothing is created. The PIN is never logged or stored.</div>
        <div style="padding:14px 16px">
          <van-button type="primary" block :loading="busy" @click="submit">Issue gift card</van-button>
        </div>
      </div>
    `,
    setup() {
      const form = reactive({ member_id: "", name: "", phone: "", initial_mins: 0, pin: "" });
      const busy = ref(false);
      async function nextId() { try { const id = PSV.unwrap(await PSV.api("/api/next_member_id")); if (id) form.member_id = String(id); } catch (_e) {} }
      onMounted(nextId);
      async function submit() {
        busy.value = true; haptic("medium");
        try {
          // PIN lives only in this request body — never persisted or logged.
          asOk(await PSV.api("/api/members/register", { method: "POST", headers: HJSON, body: JSON.stringify({ member_id: form.member_id, name: form.name, phone: form.phone, staff: PSV.getStaff().name, initial_mins: form.initial_mins, mins_added: form.initial_mins, amount: 0, is_gift: true, pin: form.pin }) }));
          showToast("Gift card issued ✓ " + form.member_id, "success");
          form.name = ""; form.phone = ""; form.initial_mins = 0; nextId();
        } catch (e) {
          const m = String((e && e.message) || "");
          if (/pin|owner|403|not allowed/i.test(m)) showToast("Owner PIN required — wrong or missing PIN.", "fail");
          else showToast(m || "Gift card failed", "fail");
        }
        finally { form.pin = ""; busy.value = false; }
      }
      return { form, busy, submit };
    },
  };

  // ── VIEW: Rank / Bonus table (WAVE 3) ──────────────────
  const RankBonus = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Rank / Bonus" back="/more" />
        <van-skeleton title :row="4" v-if="loading" style="margin:12px" />
        <template v-else>
          <div class="psv-card psv-rows" style="margin-top:8px">
            <div class="psv-row"><span>Master from</span><b class="psv-tabnum">{{ fKs(thresholds.master_threshold) }} Ks</b></div>
            <div class="psv-row"><span>Immortal from</span><b class="psv-tabnum">{{ fKs(thresholds.immortal_threshold) }} Ks</b></div>
          </div>
          <div class="psv-table-wrap">
            <table class="psv-table">
              <thead><tr><th>Amount (Ks)</th><th>Warrior</th><th>Master</th><th>Immortal</th></tr></thead>
              <tbody>
                <tr v-for="r in rows" :key="r.threshold">
                  <td class="psv-tabnum">{{ fKs(r.threshold) }}</td>
                  <td class="psv-tabnum">{{ r.warrior_bonus }}</td>
                  <td class="psv-tabnum">{{ r.master_bonus }}</td>
                  <td class="psv-tabnum">{{ r.immortal_bonus }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="psv-note">Bonus minutes granted on top-up, by rank tier.</div>
        </template>
      </div>
    `,
    setup() {
      const loading = ref(true); const rows = ref([]);
      const thresholds = reactive({ master_threshold: 0, immortal_threshold: 0 });
      async function load() {
        loading.value = true;
        const [b, t] = await Promise.allSettled([PSV.api("/api/fetch_bonus_table"), PSV.api("/api/fetch_rank_thresholds")]);
        rows.value = b.status === "fulfilled" ? (PSV.unwrap(b.value) || []) : [];
        if (t.status === "fulfilled") { const d = PSV.unwrap(t.value) || {}; thresholds.master_threshold = d.master_threshold || 0; thresholds.immortal_threshold = d.immortal_threshold || 0; }
        loading.value = false;
      }
      onMounted(load);
      return { loading, rows, thresholds, fKs };
    },
  };

  // ── Registration + boot ─────────────────────────────────
  function registerApp() {
    router.register("/", Home);
    router.register("/sales", Sales);
    router.register("/members", Members);
    router.register("/members/register", MemberRegister);
    router.register("/bookings", Bookings);
    router.register("/timeline", TimelineGrid);
    router.register("/more", More);
    router.register("/print", PrintScreen);
    router.register("/me", MeScreen);
    router.register("/deposit", Deposit);
    router.register("/consoles", Consoles);
    router.register("/food-orders", FoodOrders);
    router.register("/inventory", Inventory);
    router.register("/reports", Reports);
    router.register("/attendance", Attendance);
    router.register("/opex", Opex);
    router.register("/staff", Staff);
    router.register("/games", Games);
    router.register("/food", Food);
    router.register("/promotions", Promotions);
    router.register("/salary", Salary);
    router.register("/notifications", NotificationsScreen);
    router.register("/sessions", SessionsTools);
    router.register("/stock-out", StockOut);
    router.register("/stock-alerts", StockAlerts);
    router.register("/stock-check", StockCheck);
    router.register("/balances", Balances);
    router.register("/account-balances", AccountBalances);
    router.register("/transfer", TransferScreen);
    router.register("/transfer-history", TransferHistory);
    router.register("/gift-card", GiftCard);
    router.register("/rank-bonus", RankBonus);
  }

  // ── VIEW: Standalone staff login (no Telegram context) ──
  const LoginScreen = {
    emits: ["logged-in"],
    template: `
      <div class="pv-login">
        <div class="pv-login__hero">
          <div class="pv-login__logo">PS <span class="vibe">VIBE</span></div>
          <div class="pv-login__tag">Play The Game. Share The VIBE!</div>
        </div>
        <div class="pv-login__card">
          <div class="pv-login__title">Staff Sign In</div>
          <div class="pv-login__sub">PS5 Gaming Lounge · Staff App</div>
          <van-form @submit="onSubmit">
            <div class="pv-login__fields">
              <van-field v-model="username" name="username" label="Username" placeholder="staff username" :rules="[{ required: true, message: 'Username required' }]" />
              <van-field v-model="password" type="password" name="password" label="Password" placeholder="" :rules="[{ required: true, message: 'Password required' }]" />
            </div>
            <div class="pv-login__row">
              <van-checkbox v-model="remember" shape="square">Remember me</van-checkbox>
            </div>
            <div v-if="errorMsg" class="pv-login__error">{{ errorMsg }}</div>
            <van-button class="pv-login__cta" type="primary" block native-type="submit" :loading="busy" loading-text="Signing in...">Login</van-button>
          </van-form>
          <div class="pv-login__hint">Sign in with the staff account created by the owner in the PS VIBE dashboard.</div>
        </div>
        <div class="pv-login__foot">PS VIBE Staff · v1.0.0</div>
      </div>
    `,
    setup(_, { emit }) {
      const username = ref("");
      const password = ref("");
      const remember = ref(true);
      const busy = ref(false);
      const errorMsg = ref("");
      async function onSubmit() {
        if (busy.value) return;
        busy.value = true;
        errorMsg.value = "";
        haptic("medium");
        try {
          await PSV.login(username.value.trim(), password.value, remember.value);
          showToast("Welcome " + (PSV.getStaff().name || "") + " ✓", "success");
          emit("logged-in");
        } catch (e) {
          const msg = (e && e.message) ? e.message : "Login failed";
          errorMsg.value = msg;
          showToast(msg, "fail");
        } finally { busy.value = false; }
      }
      return { username, password, remember, busy, errorMsg, onSubmit };
    },
  };

  // ══════════════════════════════════════════════════════
  //  APP-MODE (standalone) premium screens
  //  Styling lives in native-theme.css under html.psv-native.
  //  All API paths reuse the existing endpoints — UI only.
  // ══════════════════════════════════════════════════════
  const DEPOSIT_METHODS = ["Cash", "KPay", "WavePay", "AYA Pay"];

  // ── VIEW: Deposit (app mode top-up) ─────────────────────
  const Deposit = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Deposit" back="/" />
        <div class="psv-pad">
          <div class="psv-field-card">
            <div class="psv-field-label">Member</div>
            <div class="psv-field psv-field--tap" @click="openPicker">
              <span v-if="member.id" class="psv-field__value">{{ member.name }} <span class="psv-field__sub">· {{ member.id }}</span></span>
              <span v-else class="psv-field__ph">Search member…</span>
              <van-icon name="arrow" />
            </div>
          </div>

          <div class="psv-field-card">
            <div class="psv-field-label">Amount (Ks)</div>
            <input v-model.number="amount" class="psv-input" type="number" inputmode="numeric" placeholder="0" />
            <div class="psv-chips">
              <button v-for="q in quick" :key="q" type="button" class="psv-chip" @click="amount = q">{{ fKs(q) }}</button>
            </div>
          </div>

          <div class="psv-field-card">
            <div class="psv-field-label">Payment method</div>
            <div class="psv-chips">
              <button v-for="m in methods" :key="m" type="button" class="psv-chip" :class="{ 'psv-chip--on': method === m }" @click="method = m">{{ m }}</button>
            </div>
          </div>

          <div class="psv-card psv-receipt">
            <div class="psv-receipt__title">Receipt preview</div>
            <div class="psv-row"><span>Member</span><b>{{ member.name || '—' }}</b></div>
            <div class="psv-row"><span>Amount</span><b class="psv-tabnum">{{ fKs(amount) }} Ks</b></div>
            <div class="psv-row"><span>Method</span><b>{{ method }}</b></div>
            <div class="psv-row psv-receipt__total"><span>Credit</span><b class="psv-tabnum">{{ fKs(amount) }} Ks</b></div>
          </div>

          <button class="psv-btn psv-btn--primary psv-btn--block" :disabled="busy || !member.id || !(Number(amount) > 0)" @click="submit">
            <span v-if="!busy">Confirm Deposit</span><span v-else>Processing…</span>
          </button>
        </div>

        <!-- 🧾 Top-up receipt (WAVE 2) — same PSV.printVoucher / 576px pipeline -->
        <van-popup v-model:show="receiptOpen" position="bottom" round class="psv-sheet" :style="{ maxHeight: '90vh' }">
          <div class="psv-sheet__handle"></div>
          <div class="psv-sheet__body">
            <div class="psv-sheet-title">🧾 Top-up Receipt</div>
            <div class="psv-paper">
              <img v-if="receiptPng" :src="receiptPng" alt="top-up receipt" class="psv-paper__img" />
              <div v-else class="psv-skeleton" style="height:300px"></div>
            </div>
            <div class="psv-card psv-receipt">
              <div class="psv-row"><span>Member</span><b>{{ receipt.member }}</b></div>
              <div class="psv-row"><span>Amount</span><b class="psv-tabnum">{{ fKs(receipt.amount) }} Ks</b></div>
              <div class="psv-row"><span>Method</span><b>{{ receipt.method }}</b></div>
              <div class="psv-row" v-if="Number(receipt.bonus) > 0"><span>Bonus</span><b class="psv-tabnum">+{{ receipt.bonus }} mins</b></div>
              <div class="psv-row"><span>Credit</span><b class="psv-tabnum">{{ receipt.total_mins }} mins</b></div>
              <div class="psv-row"><span>New balance</span><b class="psv-tabnum">{{ receipt.new_balance }} mins</b></div>
              <div class="psv-row"><span>Staff</span><b>{{ receipt.staff }}</b></div>
              <div class="psv-row"><span>Time</span><b>{{ receipt.time }}</b></div>
            </div>
            <div class="psv-btn-row">
              <button class="psv-btn psv-btn--ghost" @click="receiptOpen = false">Close</button>
              <button class="psv-btn psv-btn--primary" :disabled="printingReceipt" @click="printReceipt">{{ printingReceipt ? 'Printing…' : '🖨 Print receipt' }}</button>
            </div>
          </div>
        </van-popup>

        <van-popup v-model:show="picker" position="bottom" round style="max-height:70vh">
          <div class="psv-sheet">
            <div class="psv-sheet__handle"></div>
            <van-search v-model="q" placeholder="Search name / phone / ID" />
            <div class="psv-sheet__body">
              <div v-if="!filtered.length" class="psv-empty">No members found</div>
              <div v-for="m in filtered" :key="m.id" class="psv-list-row" @click="pick(m)">
                <div class="psv-avatar">{{ initials(m.name || m.id) }}</div>
                <div class="psv-list-row__main"><div class="psv-list-row__title">{{ m.name || m.id }}</div><div class="psv-list-row__sub">ID {{ m.id }} · {{ m.phone || '-' }}</div></div>
                <van-icon name="arrow" />
              </div>
            </div>
          </div>
        </van-popup>
      </div>
    `,
    setup() {
      const picker = ref(false);
      const q = ref("");
      const members = ref([]);
      const member = reactive({ id: "", name: "" });
      const amount = ref(0);
      const method = ref("Cash");
      const busy = ref(false);
      const quick = [10000, 20000, 50000, 100000];
      const filtered = computed(() => {
        const s = q.value.trim().toLowerCase();
        if (!s) return members.value;
        return members.value.filter((m) => (((m.name || "") + " " + (m.id || "") + " " + (m.phone || "")).toLowerCase().includes(s)));
      });
      function initials(s) { return String(s || "?").trim().slice(0, 2).toUpperCase(); }
      async function openPicker() {
        haptic("light"); picker.value = true;
        if (!members.value.length) {
          try { const d = PSV.unwrap(await PSV.api("/api/fetch_members?limit=300")); members.value = (d && d.members) || []; }
          catch (e) { showToast("Load failed: " + e.message, "fail"); }
        }
      }
      function pick(m) { member.id = m.id; member.name = m.name || m.id; picker.value = false; haptic("light"); }
      // 🧾 WAVE 2 top-up receipt (reuses the PrintScreen / printVoucher 576px pipeline)
      const receiptOpen = ref(false);
      const receiptPng = ref("");
      const printingReceipt = ref(false);
      const receipt = reactive({ voucher_no: "", member: "", amount: 0, method: "Cash", bonus: 0, total_mins: 0, new_balance: 0, staff: "", time: "" });
      function currentTopupVoucher() {
        const items = [{ name: "Top-up Credit", qty: 1, price: Number(receipt.amount) || 0 }];
        if (Number(receipt.bonus) > 0) items.push({ name: "Rank Bonus", qty: 1, price: 0 });
        return {
          voucher_no: receipt.voucher_no || ("TOP-" + Date.now()),
          date: PSV.todayMMT(),
          time: receipt.time || new Date().toTimeString().slice(0, 5),
          staff: receipt.staff,
          member: receipt.member,
          items: items,
          total: Number(receipt.amount) || 0,
          payment: receipt.method,
          balance: "New balance: " + (Number(receipt.total_mins) || 0) + " mins",
          notes: "TOP-UP RECEIPT" + (Number(receipt.bonus) > 0 ? (" | Bonus: +" + receipt.bonus + " mins") : ""),
        };
      }
      async function openReceipt(v) {
        receipt.voucher_no = v.voucher_no || "";
        receipt.member = v.member || "";
        receipt.amount = Number(v.amount) || 0;
        receipt.method = v.method || "Cash";
        receipt.bonus = Number(v.bonus) || 0;
        receipt.total_mins = Number(v.total_mins) || 0;
        receipt.new_balance = Number(v.new_balance) || 0;
        receipt.staff = v.staff || "";
        receipt.time = v.time || new Date().toTimeString().slice(0, 5);
        receiptOpen.value = true;
        try { receiptPng.value = await PSV.renderVoucherPng(currentTopupVoucher()); } catch (_e) { receiptPng.value = ""; }
      }
      async function printReceipt() {
        printingReceipt.value = true;
        try { await PSV.printVoucher(currentTopupVoucher()); } finally { printingReceipt.value = false; }
      }
      async function submit() {
        const amt = Number(amount.value) || 0;
        if (!member.id || amt <= 0 || busy.value) return;
        busy.value = true; haptic("medium");
        const _mName = member.name || member.id; const _mId = member.id; const _mMethod = method.value;
        try {
          const res = PSV.unwrap(await PSV.api("/api/topup/log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member_id: _mId, amount: amt, payment_method: _mMethod, staff_name: PSV.getStaff().name, client_ref: PSV.genClientRef() }) }));
          if (res && res.duplicate) showToast("Already applied — " + (res.balance_mins ?? "?") + " mins", "warning");
          else showToast("Deposit done ✓ balance " + (res.balance_mins ?? "?"), "success");
          // 🧾 Auto-offer the top-up receipt
          const bonus = (res && res.bonus_mins != null) ? res.bonus_mins : 0;
          const totalMins = (res && res.mins_added != null) ? res.mins_added : ((res && res.base_mins != null ? res.base_mins : 0) + Number(bonus || 0));
          const newBal = (res && res.balance_mins != null) ? res.balance_mins : totalMins;
          openReceipt({ voucher_no: (res && res.voucher_no) || ("TOP-" + Date.now()), member: _mName + " (" + _mId + ")", amount: amt, method: _mMethod, bonus: bonus, total_mins: totalMins, new_balance: newBal, staff: PSV.getStaff().name });
          amount.value = 0; member.id = ""; member.name = "";
        } catch (e) { showToast("Error: " + e.message, "fail"); }
        finally { busy.value = false; }
      }
      return { picker, q, members, member, amount, method, busy, quick, filtered, methods: DEPOSIT_METHODS, initials, openPicker, pick, submit, fKs, receiptOpen, receiptPng, printingReceipt, receipt, printReceipt };
    },
  };

  // ── VIEW: Print / 80mm voucher preview (app mode) ───────
  const PrintScreen = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Print" back="/" />
        <div class="psv-pad">
          <div class="psv-card psv-printer">
            <div class="psv-printer__row">
              <div class="psv-printer__dot" :class="{ 'is-on': connected }"></div>
              <div class="psv-list-row__main">
                <div class="psv-list-row__title">{{ connected ? (printerName || 'Printer') : 'No printer connected' }}</div>
                <div class="psv-list-row__sub">{{ statusText }}</div>
              </div>
              <button class="psv-chip" type="button" @click="checkStatus">Check</button>
            </div>
          </div>

          <div class="psv-btn-row">
            <button class="psv-btn psv-btn--primary" type="button" @click="choosePrinter">{{ connecting ? 'Working…' : (connected ? '🔗 Change printer' : '🔗 Choose printer') }}</button>
          </div>

          <div class="psv-section-title">Paper width</div>
          <div class="psv-btn-row" style="gap:8px">
            <button class="psv-chip" :class="{ 'psv-chip--on': paperMm === 80 }" type="button" @click="setWidth(80)">80mm</button>
            <button class="psv-chip" :class="{ 'psv-chip--on': paperMm === 58 }" type="button" @click="setWidth(58)">58mm</button>
          </div>

          <van-popup v-model:show="pickerOpen" position="bottom" round :style="{ maxHeight: '70vh' }">
            <div class="psv-pad">
              <div class="psv-section-title">Paired Bluetooth printers</div>
              <div v-if="!printers.length" class="psv-list-row__sub" style="padding:8px 0">
                No paired printers found. Pair the printer in Android Settings → Bluetooth first, then tap Choose printer again.
              </div>
              <div v-for="p in printers" :key="p.address" class="psv-list-row" @click="connectPrinter(p)">
                <div class="psv-list-row__main">
                  <div class="psv-list-row__title">{{ p.name }}</div>
                  <div class="psv-list-row__sub">{{ p.address }} · {{ p.type || 'bt' }}</div>
                </div>
                <van-icon name="arrow" />
              </div>
              <button class="psv-btn psv-btn--ghost psv-btn--block" style="margin-top:10px" @click="pickerOpen=false">Close</button>
            </div>
          </van-popup>

          <div class="psv-section-title">{{ paperMm }}mm voucher preview</div>
          <div class="psv-paper">
            <img v-if="png" :src="png" alt="voucher preview" class="psv-paper__img" />
            <div v-else class="psv-skeleton" style="height:360px"></div>
          </div>

          <div class="psv-btn-row">
            <button class="psv-btn psv-btn--ghost" @click="testPrint">Test print</button>
            <button class="psv-btn psv-btn--primary" :disabled="printing" @click="doPrint">{{ printing ? 'Printing…' : 'Print voucher' }}</button>
          </div>
        </div>
      </div>
    `,
    setup() {
      const png = ref("");
      const printing = ref(false);
      const connected = ref(false);
      const paperMm = ref(PSV.getPaperWidth ? PSV.getPaperWidth() : 80);
      const printerName = ref("");
      const statusText = ref("Tap Check to query the printer");
      // Printer pairing (2026-09-20 fix): the plugin REQUIRES an explicit connect()
      // before printImage/testPrint — without it every print silently fell back to
      // "saving image instead". These refs drive the Choose-printer picker.
      const printers = ref([]);
      const pickerOpen = ref(false);
      const connecting = ref(false);
      const SAVED_KEY = "psv_printer";
      function savedPrinter() { try { return JSON.parse(localStorage.getItem(SAVED_KEY) || "null"); } catch (_e) { return null; } }
      const sample = {
        voucher_no: PSV.genVoucherNo(),
        date: PSV.todayMMT(), time: "18:30",
        staff: PSV.getStaff().name || "Staff",
        member: "M-0001",
        items: [ { name: "Game time (60m)", qty: 1, price: 10000 }, { name: "Chicken noodle", qty: 2, price: 4000 } ],
        total: 18000, payment: "Cash", balance: "1,240 mins",
      };
      function currentVoucher() { return (window.PSVLastVoucher && window.PSVLastVoucher.raw) || sample; }
      async function render() {
        try { png.value = await PSV.renderVoucherPng(currentVoucher()); }
        catch (_e) { png.value = ""; }
      }
      function plugin() { const c = window.Capacitor; return c && c.Plugins && c.Plugins.PSVPrinter; }
      async function checkStatus() {
        const P = plugin();
        if (!P) { statusText.value = "Native printer unavailable (web preview mode)"; connected.value = false; return; }
        try { const s = await P.status(); connected.value = !!(s && s.connected); printerName.value = (s && (s.name || s.address)) || ""; statusText.value = connected.value ? "Connected" : "Disconnected"; }
        catch (_e) { statusText.value = "Status error"; }
      }
      async function testPrint() {
        const P = plugin();
        if (!P || !P.testPrint) { showToast("Test print needs the native app", "warning"); return; }
        try { await P.testPrint(); showToast("Test page sent ✓", "success"); } catch (_e) { showToast("Test failed", "fail"); }
      }
      async function doPrint() {
        printing.value = true; haptic("medium");
        try { await PSV.printVoucher(currentVoucher()); } finally { printing.value = false; }
      }
      // Paper width chip — persists to localStorage (psv_paper_mm) + pushes to the native plugin.
      async function setWidth(mm) {
        paperMm.value = (Number(mm) === 58) ? 58 : 80;
        try { if (PSV.setPaperWidth) await PSV.setPaperWidth(paperMm.value); } catch (_e) {}
        render();
        showToast(paperMm.value + "mm paper ✓", "success");
      }
      async function choosePrinter() {
        const P = plugin();
        if (!P || !P.listPrinters) { showToast("Printer needs the native app", "warning"); return; }
        connecting.value = true;
        try {
          const res = await P.listPrinters();
          printers.value = (res && res.printers) || [];
          pickerOpen.value = true;
        } catch (e) { showToast((e && e.message) || "Could not list printers", "fail"); }
        finally { connecting.value = false; }
      }
      async function connectPrinter(p) {
        const P = plugin();
        if (!P || !P.connect) { showToast("Connect needs the native app", "warning"); return; }
        connecting.value = true;
        statusText.value = "Connecting to " + (p.name || p.address) + "…";
        try {
          await P.connect({ address: p.address, mode: "auto" });
          try { localStorage.setItem(SAVED_KEY, JSON.stringify({ address: p.address, name: p.name || p.address })); } catch (_e) {}
          pickerOpen.value = false;
          showToast("Printer connected ✓", "success");
          await checkStatus();
        } catch (e) { statusText.value = "Connect failed"; showToast((e && e.message) || "Connect failed", "fail"); }
        finally { connecting.value = false; }
      }
      // Re-connect to the saved printer on entry so Print works without re-picking.
      async function autoConnect() {
        const P = plugin();
        const s = savedPrinter();
        if (!P || !P.connect || !s || !s.address) return;
        try {
          const st = P.status ? await P.status().catch(function () { return null; }) : null;
          if (!st || !st.connected) await P.connect({ address: s.address, mode: "auto" });
        } catch (_e) {}
      }
      onMounted(() => { render(); checkStatus(); autoConnect().then(checkStatus); });
      return { png, printing, connected, printerName, statusText, checkStatus, testPrint, doPrint, paperMm, setWidth,
        printers, pickerOpen, connecting, choosePrinter, connectPrinter };
    },
  };

  // ── VIEW: Me (app mode) ─────────────────────────────────
  const MeScreen = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Me" back="/" />
        <div class="psv-pad">
          <div class="psv-card psv-profile">
            <div class="psv-avatar psv-avatar--lg">{{ initials }}</div>
            <div class="psv-list-row__main">
              <div class="psv-profile__name">{{ staff.name || 'Staff' }}</div>
              <span class="psv-account-role">{{ staff.role || 'staff' }}</span>
            </div>
          </div>

          <div class="psv-section-title">Shift</div>
          <div class="psv-card psv-rows">
            <div class="psv-row"><span>Today</span><b class="psv-tabnum">{{ fKs(summary.minsToday) }} mins</b></div>
            <div class="psv-row"><span>Sales today</span><b class="psv-tabnum">{{ summary.salesToday }}</b></div>
            <div class="psv-row"><span>Attendance</span><b>{{ attendance.status }}</b></div>
            <div class="psv-row" v-if="attendance.check_in || attendance.check_out"><span>In / Out</span><b>{{ attendance.check_in || '-' }} / {{ attendance.check_out || '-' }}</b></div>
          </div>
          <div class="psv-btn-row">
            <button class="psv-btn psv-btn--ghost" :disabled="attBusy || attendance.open" @click="checkIn">Check in</button>
            <button class="psv-btn psv-btn--primary" :disabled="attBusy || !attendance.open" @click="checkOut">Check out</button>
          </div>

          <div class="psv-section-title">Settings</div>
          <div class="psv-card psv-list">
            <div class="psv-list-row" @click="goNotifications">
              <div class="psv-list-row__main"><div class="psv-list-row__title">Notifications</div><div class="psv-list-row__sub">View session & booking alerts</div></div>
              <van-icon name="arrow" />
            </div>
            <div class="psv-list-row">
              <div class="psv-list-row__main"><div class="psv-list-row__title">Push alerts</div><div class="psv-list-row__sub">Device notifications</div></div>
              <van-switch v-model="notify" size="22" @change="onNotify" />
            </div>
            <div class="psv-list-row" @click="goPrint">
              <div class="psv-list-row__main"><div class="psv-list-row__title">Printer settings</div><div class="psv-list-row__sub">80mm Bluetooth printer</div></div>
              <van-icon name="arrow" />
            </div>
          </div>

          <div class="psv-section-title">About</div>
          <div class="psv-card psv-list">
            <div class="psv-row"><span>App version</span><b>{{ appVersion }}</b></div>
            <div class="psv-list-row" @click="checkUpdate">
              <div class="psv-list-row__main"><div class="psv-list-row__title">Check for updates</div><div class="psv-list-row__sub">{{ updateText }}</div></div>
              <van-icon name="arrow" />
            </div>
          </div>

          <button class="psv-btn psv-btn--danger psv-btn--block" @click="logout">Log out</button>
          <div class="psv-foot">PS VIBE Staff · Play The Game. Share The VIBE!</div>
        </div>
      </div>
    `,
    setup() {
      const staff = reactive({ name: PSV.getStaff().name || "", role: PSV.getStaff().role || "" });
      const initials = computed(() => String(staff.name || "?").trim().slice(0, 2).toUpperCase());
      const notify = ref(true);
      const appVersion = ref("1.0.0");
      const updateText = ref("Up to date");
      const summary = reactive({ minsToday: 0, salesToday: 0 });
      const attendance = reactive({ status: "Not checked in", check_in: "", check_out: "", open: false });
      const attBusy = ref(false);
      function goPrint() { haptic("light"); router.push("/print"); }
      function goNotifications() { haptic("light"); router.push("/notifications"); }
      // 🕐 Attendance: today's row + check-in / check-out (Wave 2 contract)
      async function loadAttendance() {
        try {
          const d = PSV.unwrap(await PSV.api("/api/attendance/today?staff_name=" + encodeURIComponent(staff.name || "")));
          const row = (d && d.today) || null;
          if (!row) { attendance.status = "Not checked in"; attendance.check_in = ""; attendance.check_out = ""; attendance.open = false; return; }
          const st = String(row.status || "").toLowerCase();
          attendance.check_in = row.check_in ? String(row.check_in).slice(11, 16) : "";
          attendance.check_out = row.check_out ? String(row.check_out).slice(11, 16) : "";
          attendance.open = ["checked_in", "present", "late"].includes(st) && !row.check_out;
          attendance.status = row.check_out ? "Checked out" : (st === "late" ? "Late" : (st === "checked_in" || st === "present" ? "Checked in" : (row.status || "-")));
        } catch (_e) { /* attendance is best-effort */ }
      }
      async function checkIn() {
        if (attBusy.value) return; attBusy.value = true; haptic("medium");
        try {
          await PSV.api("/api/save_attendance", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ staff_name: staff.name, status: "Present" }) });
          showToast("Checked in ✓", "success"); await loadAttendance();
        } catch (e) { showToast("Check-in failed: " + e.message, "fail"); }
        finally { attBusy.value = false; }
      }
      async function checkOut() {
        if (attBusy.value) return; attBusy.value = true; haptic("medium");
        try {
          await PSV.api("/api/save_attendance", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ staff_name: staff.name, status: "check-out" }) });
          showToast("Checked out ✓", "success"); await loadAttendance();
        } catch (e) { showToast("Check-out failed: " + e.message, "fail"); }
        finally { attBusy.value = false; }
      }
      function load() {
        try {
          const info = window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.PSVShell;
          if (info && info.getAppInfo) { info.getAppInfo().then((a) => { if (a && a.versionName) appVersion.value = a.versionName; }).catch(() => {}); }
        } catch (_e) {}
        PSV.api("/api/sales/list-today").then((r) => { const d = PSV.unwrap(r); if (Array.isArray(d)) { summary.salesToday = d.length; summary.minsToday = d.reduce((a, x) => a + (Number(x.mins) || 0), 0); } }).catch(() => {});
        loadAttendance();
      }
      async function checkUpdate() {
        try {
          const r = await fetch((PSV.apiOrigin ? PSV.apiOrigin() : "") + "/staff-app/version.json", { cache: "no-store" });
          if (r.ok) { const v = await r.json(); updateText.value = "Latest " + (v.version || "?"); }
          else updateText.value = "Up to date";
        } catch (_e) { updateText.value = "Up to date"; }
        haptic("light");
      }
      function onNotify() { haptic("light"); showToast(notify.value ? "Notifications on" : "Notifications off", "success"); }
      function logout() { haptic("medium"); PSV.logout(); showToast("Logged out", "success"); }
      onMounted(load);
      return { staff, initials, notify, appVersion, updateText, summary, attendance, attBusy, checkIn, checkOut, goPrint, goNotifications, checkUpdate, onNotify, logout, fKs };
    },
  };

  // ── VIEW: Notification Center (app + Telegram modes) ──
  const NotificationsScreen = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Notifications" back="/" />
        <van-pull-refresh v-model="refreshing" @refresh="load">
          <div v-if="loading" class="psv-notif-pad"><van-skeleton title :row="4" /></div>
          <template v-else>
            <div v-if="!items.length" class="psv-notif-empty">
              <div class="psv-notif-empty__ico">🔔</div>
              <div class="psv-notif-empty__txt">No notifications yet</div>
              <div class="psv-notif-empty__sub">Session and booking reminders will appear here.</div>
            </div>
            <template v-else>
              <div class="psv-notif-bar">
                <span class="psv-notif-bar__count">{{ unread }} unread</span>
                <van-button size="small" :disabled="!unread" @click="readAll">Mark all read</van-button>
              </div>
              <div v-for="grp in groups" :key="grp.label">
                <div class="psv-notif-group">{{ grp.label }}</div>
                <div class="psv-notif-list">
                  <div v-for="n in grp.items" :key="n.id" class="psv-notif-item" :class="{ 'is-unread': !n.read_at }" @click="openNotif(n)">
                    <div class="psv-notif-ico" :class="'psv-notif-ico--' + icoType(n.type)">{{ icoEmoji(n.type) }}</div>
                    <div class="psv-notif-main">
                      <div class="psv-notif-title">{{ n.title || 'Notification' }}</div>
                      <div v-if="n.body" class="psv-notif-body">{{ n.body }}</div>
                      <div class="psv-notif-time">{{ rel(n.created_at) }}</div>
                    </div>
                    <span v-if="!n.read_at" class="psv-notif-dot"></span>
                  </div>
                </div>
              </div>
            </template>
          </template>
        </van-pull-refresh>
      </div>
    `,
    setup() {
      const items = ref([]);
      const unread = ref(0);
      const loading = ref(true);
      const refreshing = ref(false);

      function icoType(t) {
        t = String(t || "").toLowerCase();
        if (t === "session_reminder") return "session";
        if (t === "booking_reminder") return "booking";
        return "generic";
      }
      function icoEmoji(t) {
        const k = icoType(t);
        return k === "session" ? "\u23f1\ufe0f" : (k === "booking" ? "\ud83d\udcc5" : "\ud83d\udd14");
      }
      function rel(ts) {
        if (!ts) return "";
        const d = new Date(String(ts).replace(" ", "T"));
        if (isNaN(d.getTime())) return String(ts);
        const s = Math.floor((Date.now() - d.getTime()) / 1000);
        if (s < 60) return "just now";
        if (s < 3600) return Math.floor(s / 60) + "m ago";
        if (s < 86400) return Math.floor(s / 3600) + "h ago";
        if (s < 172800) return "yesterday";
        return Math.floor(s / 86400) + "d ago";
      }
      const groups = computed(() => {
        const today = PSV.todayMMT();
        const yd = new Date(Date.now() + (6 * 60 + 30) * 60000 - 86400000).toISOString().slice(0, 10);
        const g = { Today: [], Yesterday: [], Earlier: [] };
        for (const n of items.value) {
          const d = new Date(String(n.created_at || "").replace(" ", "T"));
          let key = "Earlier";
          if (!isNaN(d.getTime())) {
            const mmt = new Date(d.getTime() + (6 * 60 + 30) * 60000).toISOString().slice(0, 10);
            if (mmt === today) key = "Today";
            else if (mmt === yd) key = "Yesterday";
          }
          g[key].push(n);
        }
        return ["Today", "Yesterday", "Earlier"].filter((k) => g[k].length).map((k) => ({ label: k, items: g[k] }));
      });
      async function load() {
        loading.value = true;
        try {
          const d = PSV.unwrap(await PSV.api("/api/staff/notifications?limit=50"));
          items.value = (d && d.items) || [];
          unread.value = Number((d && d.unread) || 0) || 0;
        } catch (_e) { items.value = []; unread.value = 0; }
        finally { loading.value = false; refreshing.value = false; }
      }
      async function markRead(n) {
        if (!n || n.read_at) return;
        n.read_at = new Date().toISOString();
        unread.value = Math.max(0, unread.value - 1);
        try { await PSV.api("/api/staff/notifications/" + n.id + "/read", { method: "POST" }); } catch (_e) {}
      }
      function openNotif(n) {
        haptic("light");
        markRead(n);
        const bid = n && (n.booking_id || n.bookingId);
        if (bid) { showToast("Booking #" + bid, "text"); openBooking(bid); return; }
        const t = String((n && n.type) || "");
        if (t.indexOf("session") >= 0) { router.push("/consoles"); return; }
        router.push("/bookings");
      }
      async function readAll() {
        haptic("light");
        try { await PSV.api("/api/staff/notifications/read-all", { method: "POST" }); } catch (_e) {}
        items.value.forEach((n) => { if (!n.read_at) n.read_at = new Date().toISOString(); });
        unread.value = 0;
        showToast("All marked read", "success");
      }
      onMounted(load);
      return { items, unread, loading, refreshing, groups, load, openNotif, readAll, rel, icoType, icoEmoji };
    },
  };

  // ── Global bottom-chrome sync (2026-09-20, Osmo: "Scroll ဆွဲလည်း အောက်ဆုံးထိ အပြည့်မပေါ်ဘူး") ──
  // The bottom tab bar's real height changes with font scaling, safe-area insets
  // and gesture navigation, so a hardcoded padding can leave the last row hidden
  // behind it. Measure the bar instead and publish it as --psv-tabbar-h; also
  // measure the Android system-navigation overlap (Android WebView reports
  // env(safe-area-inset-bottom) as 0) as --psv-sysnav-h.
  function psvSyncChrome() {
    try {
      const tb = document.querySelector(".van-tabbar");
      if (tb) {
        const h = Math.round(tb.getBoundingClientRect().height);
        if (h > 0) document.documentElement.style.setProperty("--psv-tabbar-h", h + "px");
      }
      let sys = 0;
      const vv = window.visualViewport;
      if (vv && window.innerHeight > vv.height) {
        const d = Math.round(window.innerHeight - vv.height - (vv.offsetTop || 0));
        if (d > 0 && d < 140) sys = d;
      }
      document.documentElement.style.setProperty("--psv-sysnav-h", sys + "px");
      // The native bridge pins #psv-update-banner at the bottom (position:fixed; bottom:0).
      // Reserve its height or it covers the last rows of a tall grid (owner 2026-09-20 11:53:
      // "Timeline က page တခုတည်းမပေါ်တော့ပြန်ဘူး").
      const bnr = document.getElementById("psv-update-banner");
      const bh = bnr ? Math.round(bnr.getBoundingClientRect().height) : 0;
      document.documentElement.style.setProperty("--psv-banner-h", (bh > 0 ? bh : 0) + "px");
    } catch (_e) {}
  }

  // Recompute the chrome whenever the native update banner appears/disappears so the
  // #app bottom padding and the timeline fit react immediately (2026-09-20).
  try {
    const _bannerObs = new MutationObserver(function () {
      psvSyncChrome();
      try { window.dispatchEvent(new Event("psv:chrome")); } catch (_e) {}
    });
    _bannerObs.observe(document.body, { childList: true });
  } catch (_e) {}

  const App = {
    components: { LoginScreen },
    template: `
      <div v-if="needLogin">
        <LoginScreen @logged-in="onLoggedIn" />
      </div>
      <div v-else>
        <component :is="activeView" />
        <van-tabbar v-if="!nativeMode" v-model="tab" active-color="#818cf8" inactive-color="#64748b" :safe-area-inset-bottom="true" class="app-tabbar">
          <van-tabbar-item name="/" icon="wap-home-o">Home</van-tabbar-item>
          <van-tabbar-item name="/sales" icon="balance-list-o">Sales</van-tabbar-item>
          <van-tabbar-item name="/members" icon="friends-o">Members</van-tabbar-item>
          <van-tabbar-item name="/bookings" icon="calendar-o">Bookings</van-tabbar-item>
          <van-tabbar-item name="/more" icon="apps-o">More</van-tabbar-item>
        </van-tabbar>
        <van-tabbar v-else v-model="tab" class="app-tabbar psv-tabbar" active-color="var(--psv-gold)" inactive-color="var(--psv-text-dim)" :safe-area-inset-bottom="true" :border="false">
          <van-tabbar-item name="/" icon="wap-home-o">Home</van-tabbar-item>
          <van-tabbar-item name="/sales" icon="shopping-cart-o">Sale</van-tabbar-item>
          <van-tabbar-item name="/bookings" icon="calendar-o">Booking</van-tabbar-item>
          <van-tabbar-item name="/print" icon="printer">Print</van-tabbar-item>
          <van-tabbar-item name="/more" icon="apps-o">Menu</van-tabbar-item>
        </van-tabbar>
      </div>

      <!-- WAVE 4 — friendly rate-limit state (429 / Cloudflare challenge).
           Never renders raw HTML; auto-retries GETs 2s→4s→8s, then offers Retry. -->
      <div v-if="rl.show" class="psv-rl" role="alert" aria-live="polite">
        <div class="psv-rl__card">
          <div class="psv-rl__spin" :class="{ 'is-off': rl.exhausted }"></div>
          <div class="psv-rl__title">{{ rl.exhausted ? 'Server is busy' : 'Server is busy — retrying…' }}</div>
          <div class="psv-rl__sub">{{ rl.sub }}</div>
          <button class="psv-btn psv-btn--primary psv-btn--block" :disabled="rl.retrying" @click="retry">{{ rl.retrying ? 'Retrying…' : 'Retry' }}</button>
        </div>
      </div>
    `,
    setup() {
      const activeView = computed(() => router.routes[router.current.value] || Home);
      const validTabs = NATIVE ? TABS_NATIVE : TABS_TG;
      const fallbackTab = NATIVE ? "/more" : "/more";
      const tab = computed({
        get: () => (validTabs.includes(router.current.value) ? router.current.value : fallbackTab),
        set: (v) => { if (v && v !== router.current.value) { haptic("light"); router.push(v); } },
      });
      const needLogin = computed(() => authGate.value);
      function onLoggedIn() { authGate.value = false; haptic("light"); }

      // ── Global rate-limit state (WAVE 4) ──────────────────
      // api.js emits psv:rate-limit / psv:rate-limit-clear around the backoff
      // and psv:retry when the user taps the Retry button.
      const rl = reactive({ show: false, exhausted: false, attempt: 0, max: 1, retrying: false, sub: "" });
      let _rlTimer = null;
      function _onRateLimit(e) {
        const d = (e && e.detail) || {};
        rl.show = true;
        rl.attempt = d.attempt || 1;
        rl.max = d.max || 1;
        rl.exhausted = !!d.exhausted;
        rl.retrying = false;
        rl.sub = rl.exhausted
          ? "The server is rate-limited. Please wait a moment, then tap Retry."
          : "Please wait — attempt " + rl.attempt + " of " + rl.max + ".";
        if (_rlTimer) { clearTimeout(_rlTimer); _rlTimer = null; }
        if (!rl.exhausted) {
          _rlTimer = setTimeout(function () { rl.show = false; }, (d.delayMs || 2000) + 3000);
        }
      }
      function _onRateLimitClear() {
        if (_rlTimer) { clearTimeout(_rlTimer); _rlTimer = null; }
        rl.show = false; rl.exhausted = false; rl.retrying = false;
      }
      function retry() {
        haptic("light");
        rl.retrying = true;
        try { PSV.retryNow(); } catch (_e) {}
        setTimeout(function () { rl.retrying = false; rl.show = false; }, 1200);
      }
      onMounted(() => {
        window.addEventListener("psv:rate-limit", _onRateLimit);
        window.addEventListener("psv:rate-limit-clear", _onRateLimitClear);
        // Keep the reserved bottom space in sync with the real tab bar.
        psvSyncChrome();
        requestAnimationFrame(psvSyncChrome);
        setTimeout(psvSyncChrome, 350);
        window.addEventListener("resize", psvSyncChrome);
        window.addEventListener("orientationchange", psvSyncChrome);
        if (window.visualViewport) window.visualViewport.addEventListener("resize", psvSyncChrome);
      });
      watch(activeView, () => { nextTick(psvSyncChrome); });
      return { activeView, tab, needLogin, onLoggedIn, nativeMode: NATIVE, rl, retry };
    },
  };

  function boot() {
    registerApp();

    // Auth gate decision BEFORE first render (avoids firing API calls behind the login screen).
    // The Telegram path is UNCHANGED — it is never gated.
    if (!(tg && tg.initData)) {
      if (!PSV.isAuthed() && !PSV.loadStoredAuth()) authGate.value = true;
      window.addEventListener("psv-auth-expired", () => { authGate.value = true; });
    }

    const app = createApp(App);
    app.use(vant);
    app.component("PageHeader", PageHeader);
    app.component("PSVPicker", PSVPicker);
    app.component("SaleForm", SaleForm);
    app.component("BookingForm", BookingForm);
    app.component("Home", Home);
    app.component("Sales", Sales);
    app.component("Members", Members);
    app.component("MemberRegister", MemberRegister);
    app.component("Bookings", Bookings);
    app.component("TimelineGrid", TimelineGrid);
    app.component("More", More);
    app.component("Deposit", Deposit);
    app.component("PrintScreen", PrintScreen);
    app.component("MeScreen", MeScreen);
    app.component("Consoles", Consoles);
    app.component("FoodOrders", FoodOrders);
    app.component("Inventory", Inventory);
    app.component("Reports", Reports);
    app.component("Attendance", Attendance);
    app.component("Opex", Opex);
    app.component("Staff", Staff);
    app.component("Games", Games);
    app.component("Food", Food);
    app.component("Promotions", Promotions);
    app.component("Salary", Salary);
    app.component("NotificationsScreen", NotificationsScreen);
    app.component("AccountBalances", AccountBalances);
    app.component("TransferScreen", TransferScreen);
    app.component("TransferHistory", TransferHistory);

    // WAVE 6 — Android back closes the top overlay first, then navigates.
    // Every Vant popup / action-sheet / dialog exposes a `show` prop; while one is
    // open we register a closer so router.back() dismisses it before stepping back.
    // (ActionSheet and Dialog render a Popup internally, so `van-popup` covers all.)
    try {
      const OVERLAY_COMPONENTS = { "van-popup": 1, "van-action-sheet": 1, "van-dialog": 1 };
      app.mixin({
        created() {
          const nm = this.$options && this.$options.name;
          if (!nm || !OVERLAY_COMPONENTS[nm]) return;
          if (!this.$props || !("show" in this.$props)) return;
          const self = this;
          const closer = function () {
            try { self.$emit("update:show", false); } catch (_e) { return false; }
            return true;
          };
          this.$watch(function () { return self.show; }, function (v) {
            if (v) router.pushOverlay(closer); else router.popOverlay(closer);
          }, { immediate: true, flush: "sync" });
        },
      });
    } catch (_e) { /* overlays still work; back just navigates */ }

    app.mount("#app");
    router.start();

    if (tg) { tg.ready(); tg.expand(); }

    // Inside Telegram → ensure the standalone(native) theme is OFF (head script may
    // have set it if the SDK had not loaded yet when it ran).
    if (tg && tg.initData) {
      try { document.documentElement.classList.remove("psv-native"); } catch (_e) {}
    }

    (async () => {
      if (!(tg && tg.initData)) return; // standalone handled above
      if (!PSV.isAuthed()) {
        const ok = await PSV.authenticate(true);
        if (!ok) showToast("Authorization failed", "fail");
      }
    })();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
