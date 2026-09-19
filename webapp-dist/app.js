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

  const { createApp, ref, reactive, computed, watch, onMounted, nextTick } = Vue;
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

  // ── Tiny Hash Router ────────────────────────────────────
  const router = {
    routes: {},
    current: ref("/"),
    register(path, component, title) { this.routes[path] = component; this.routes[path + "__title"] = title; },
    title(path) { return this.routes[path + "__title"] || "PS VIBE"; },
    push(path) {
      if (window.location.hash === "#" + path) this.current.value = path;
      else window.location.hash = "#" + path;
    },
    resolve() {
      let h = window.location.hash.replace(/^#/, "");
      if (!h || h === "/") return "/";
      // try exact match, else fall back to closest (e.g. /sales/new → /sales)
      if (this.routes[h]) return h;
      return "/";
    },
    start() {
      const onHash = () => { this.current.value = this.resolve(); };
      window.addEventListener("hashchange", onHash);
      this.current.value = this.resolve();
      // Telegram hardware back key → in-app navigation
      try {
        const tg = PSV.getTg();
        if (tg && tg.BackButton) {
          tg.BackButton.onClick(() => {
            const cur = this.current.value;
            if (cur && cur !== "/") { haptic("light"); this.push("/"); }
            else if (tg.close) { tg.close(); }
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

      const unread = ref(0);
      async function loadUnread() {
        try { const d = PSV.unwrap(await PSV.api("/api/staff/notifications/unread-count")); unread.value = Number((d && (d.unread != null ? d.unread : d.count)) || 0) || 0; }
        catch (_e) { unread.value = 0; }
      }
      function goNotifications() { haptic("light"); router.push("/notifications"); }

      async function load() {
        loading.value = true;
        const [members, consoles, sales] = await Promise.allSettled([
          PSV.api("/api/fetch_members?limit=1"),
          PSV.api("/api/fetch_console_status"),
          PSV.api("/api/sales/list-today").catch(() => null),
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
            const bd = PSV.unwrap(await PSV.api("/api/bookings?status="));
            const arr = Array.isArray(bd) ? bd : (bd && bd.bookings) || [];
            const today = PSV.todayMMT();
            summary.bookings = arr.filter((b) => !(b.date || b.booking_date) || String(b.date || b.booking_date).slice(0, 10) === today).length;
            summary.pendingDeposits = arr.filter((b) => ["paid", "pending"].includes(String(b.deposit_status || "").toLowerCase())).length;
          } catch (_e) {}
        }
        loading.value = false;
      }
      onMounted(() => { load(); loadUnread(); });
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
          let fi = s.notes || ""; const mm = /Food:\s*([^|]+)/.exec(fi); if (mm) fi = mm[1].trim();
          items.push({ name: "🍜 " + (fi || "Food"), qty: 1, price: ft });
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
        <div v-if="sessionWarn" class="psv-sess-warn">⚠️ {{ sessionWarn }} — သေချာစစ်ပါ</div>
        <div v-if="depositRedeem" class="psv-redeem-banner" :class="{ 'is-applied': depositApplied }">
          <span class="psv-redeem-banner__txt"><template v-if="depositApplied">✅ Deposit {{ fKs(depositRedeem.amount) }} Ks (BK#{{ depositRedeem.booking_id }}) — နုတ်ပြီးပါပြီ</template><template v-else>↩️ Verified deposit {{ fKs(depositRedeem.amount) }} Ks (BK#{{ depositRedeem.booking_id }}) ရှိသည်</template></span>
          <button v-if="!depositApplied" class="psv-abtn psv-abtn--gold" style="min-height:36px;padding:0 10px" @click="applyDeposit">💰 Deposit {{ fKs(depositRedeem.amount) }} နုတ်မယ်</button>
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
        <div style="margin:10px 16px 0">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 10px">
            <span style="font-weight:600">🏷️ Promotion / 🎟️ Coupon</span>
            <span v-if="calcStatus === 'ok' && totalDiscountKs > 0" class="psv-verified">🛡 server အတည်ပြု</span>
            <span v-else-if="calcStatus === 'loading'" style="color:var(--pv-muted);font-size:11px">တွက်ချက်နေသည်…</span>
          </div>
          <div class="psv-card psv-disc-card">
            <div v-if="promo.id" class="psv-pick-row">
              <div class="psv-pick-row__main">
                <div class="psv-pick-row__t">🎁 {{ promo.title }}</div>
                <div class="psv-pick-row__s">{{ promoText(promo) }}</div>
              </div>
              <button class="psv-chip" @click="clearPromo">ဖြုတ်</button>
            </div>
            <button v-else class="psv-btn psv-btn--ghost psv-btn--block" style="min-height:44px" @click="openPromo">🎁 Promotion ရွေးရန်</button>

            <div style="display:flex;gap:8px;margin-top:10px">
              <input v-model.trim="coupon.code" class="psv-input" style="flex:1" :disabled="coupon.applied" placeholder="Coupon code (CB…)" @keyup.enter="applyCoupon" />
              <button class="psv-chip" :disabled="coupon.applied || coupon.checking || !coupon.code" @click="applyCoupon">{{ coupon.applied ? '✓ applied' : (coupon.checking ? '…' : 'စစ်ရန်') }}</button>
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
        <van-cell-group inset style="margin-top:10px" title="ငွေစာရင်း / Amounts">
          <van-cell title="Game" :value="fKs(form.game_amt) + ' Ks'" />
          <van-cell title="Food" :value="fKs(form.food_total) + ' Ks'" />
          <van-cell title="Subtotal" :value="fKs(subtotalKs) + ' Ks'" />
          <van-cell v-for="(b, i) in discountLines" :key="'d' + i" :title="b.label">
            <template #value><span style="color:var(--pv-success)">{{ b.amount < 0 ? '-' + fKs(-b.amount) : fKs(b.amount) }}</span></template>
          </van-cell>
          <van-cell v-if="!discountLines.length && Number(form.discount) > 0" title="Discount" :value="'-' + fKs(form.discount) + ' Ks'" />
          <van-cell title="Discount စုစုပေါင်း" :value="'-' + fKs(totalDiscountKs) + ' Ks'" />
          <van-cell title="Total" style="font-weight:700">
            <template #value><span class="pv-amount" style="font-size:16px">{{ fKs(payableBeforeDeposit) }} Ks</span></template>
          </van-cell>
          <van-cell v-if="depositApplied" title="Deposit နုတ်">
            <template #value><span style="color:var(--pv-success);font-weight:600">-{{ fKs(depositRedeem.amount) }} Ks</span></template>
          </van-cell>
          <van-cell title="ကျသင့်ငွေ (Cash due)" style="font-weight:800">
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
              <van-cell v-if="Number(lastVoucher.deposit) > 0" title="Deposit နုတ်">
                <template #value><span style="color:var(--pv-success)">-{{ fKs(lastVoucher.deposit) }} Ks</span></template>
              </van-cell>
              <van-cell v-if="Number(lastVoucher.cashDue) != null && Number(lastVoucher.cashDue) !== Number(lastVoucher.total)" title="ကျသင့်ငွေ" style="font-weight:800">
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
            <div class="psv-sheet-title" style="padding:0 16px">🎁 Promotion ရွေးရန်</div>
            <div class="psv-bk-hint" style="padding:4px 16px 8px">ရွေးလိုက်ရင် ဆာဗာက discount ကို တွက်ပေးမယ်</div>
            <van-empty v-if="!promos.length" description="Active promotion မရှိပါ" />
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
            <div class="psv-sheet-title" style="padding:0 16px">🍜 FOC (အခမဲ့) item ရွေးရန်</div>
            <van-empty v-if="!foodLines.length" description="Food item မရှိပါ" />
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
      // Booking detail view. WAVE 2 adds the explicit "Deposit {amount} နုတ်မယ်"
      // confirm + a lookup for verified deposits on the picked member/console.
      const depositRedeem = ref(PSV.pendingDepositRedeem || null);
      const depositApplied = ref(false);
      function clearRedeem() { try { PSV.pendingDepositRedeem = null; } catch (_e) {} depositRedeem.value = null; depositApplied.value = false; }
      const depositAppliedAmt = computed(() => (depositApplied.value && depositRedeem.value) ? (Number(depositRedeem.value.amount) || 0) : 0);
      async function applyDeposit() {
        if (!depositRedeem.value) return;
        const amt = Number(depositRedeem.value.amount) || 0;
        const okc = await confirmBox("Deposit နုတ်မလား?", "BK#" + depositRedeem.value.booking_id + " verified deposit " + fKs(amt) + " Ks ကို ဒီအရောင်း ကျသင့်ငွေကနေ နုတ်မယ်။", "နုတ်မယ်", "မလုပ်");
        if (!okc) return;
        depositApplied.value = true; haptic("medium");
        showToast("Deposit " + fKs(amt) + " Ks နုတ်ပြီး", "success");
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
        const tgtMy = tgt === "game_only" ? "ဂိမ်းကြေး" : tgt === "food_only" ? "အစားအသောက်" : "စုစုပေါင်း";
        if (t === "bonus_mins") return "ကစားချိန် +" + (pct || "") + "% bonus mins";
        if (t === "bundle" || t === "free_item") return "Bundle / FOC — အစားအသောက် ၁ မျိုး အခမဲ့" + (p.notes ? " · " + p.notes : "");
        if (pct) return tgtMy + " " + pct + "% လျှော့" + (p.notes ? " · " + p.notes : "");
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
          if (r && r.success === false) throw new Error(r.error || "Coupon မမှန်ပါ");
          const d = PSV.unwrap(r); const c = (d && d.coupon) || {};
          if (!c.code) throw new Error("Coupon မတွေ့ပါ");
          coupon.code = c.code;
          coupon.minutes = Number(c.balance_minutes) || 0;
          coupon.discount = Math.round(coupon.minutes * (Number(form.base_rate) || 0) / 60);
          coupon.applied = true;
          coupon.message = "✓ သက်တမ်းရှိ — " + coupon.minutes + " mins (~" + fKs(coupon.discount) + " Ks). Save နှိပ်ချိန် redeem ဖြစ်မယ်။";
          haptic("light"); scheduleCalc();
        } catch (e) {
          coupon.applied = false; coupon.minutes = 0; coupon.discount = 0;
          coupon.message = "✗ " + (e && e.message ? e.message : "Coupon မမှန်ပါ");
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
        return "ဒီ " + (cid && String(hit.console_id || "").trim().toLowerCase() === cid ? "Console" : "Member") + " က အခု BK#" + hit.id + " session လုပ်နေတယ် (" + (hit.status || "Active") + ")";
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
          const okc = await confirmBox("⚠️ Session လုပ်နေတယ်", who + " က အခု BK#" + bk.id + " session လုပ်နေတယ် — သေချာလား? ထပ်ကောက်ရင် double-charge ဖြစ်နိုင်တယ်။", "ဆက်လုပ်", "မလုပ်", false);
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

          // ── 🎟️ Coupon redeem (idempotent per sale_ref = voucher) ──
          if (coupon.applied) {
            try {
              const cr = await PSV.api("/api/coupons/redeem", { method: "POST", headers: HJSON, body: JSON.stringify({ code: coupon.code, minutes: coupon.minutes, sale_ref: form.voucher_no, staff_name: PSV.getStaff().name }) });
              if (cr && cr.success === false) throw new Error(cr.error || "Coupon redeem မအောင်မြင်");
              const cd = PSV.unwrap(cr) || {};
              coupon.message = "✓ Redeem ပြီး — " + (cd.deducted_minutes != null ? cd.deducted_minutes : coupon.minutes) + " mins" + (cd.remaining_minutes != null ? " (ကျန် " + cd.remaining_minutes + ")" : "");
              showToast("Coupon " + coupon.code + " · -" + (cd.deducted_minutes != null ? cd.deducted_minutes : coupon.minutes) + " mins", "success");
            } catch (ce) {
              showToast("Coupon redeem မအောင်မြင်: " + ce.message, "fail");
              submitting.value = false; return;
            }
          }

          const payload = {
            voucher_no: form.voucher_no, member_id: member_id,
            console_id: form.console_id, mins: form.mins, base_rate: form.base_rate,
            multiplier: form.multiplier, game_amt: final_game_amt, food_items: form.food_items,
            food_total: form.food_total, discount: discTotal, net_total: netAfterDiscount,
            payment_method: final_pm, staff: PSV.getStaff().name,
            wallet_deduct: wallet_deduct,
            promotion_name: promo.title || "",
            coupon_code: coupon.applied ? coupon.code : "",
            booking_id: (depositApplied.value && depositRedeem.value) ? depositRedeem.value.booking_id : "",
            client_ref: PSV.genClientRef(),
          };
          const res = PSV.unwrap(await PSV.api("/api/sales/record", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }));
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
            const vPay = final_pm + (depAmt > 0 ? (" + Deposit " + fKs(depAmt) + " Ks") : "");
            lastVoucher.value = {
              voucher_no: vno,
              date: PSV.todayMMT(),
              time: new Date().toTimeString().slice(0, 5),
              staff: PSV.getStaff().name,
              member: isMemberSale ? (form.member_search || form.member_id) : "Guest",
              items: vItems,
              subtotal: vSubtotal,
              discount: discTotal,
              total: netAfterDiscount,
              deposit: depAmt,
              cashDue: payableNow,
              payment: vPay,
              balance: wallet_deduct > 0 ? ("Wallet −" + wallet_deduct + " mins") : "",
              notes: "Subtotal: " + fKs(vSubtotal) + " Ks" + (discTotal > 0 ? (" | Discount: -" + fKs(discTotal) + " Ks") : "") + (depAmt > 0 ? (" | Deposit: -" + fKs(depAmt) + " Ks") : "") + (promo.title ? (" | Promo: " + promo.title) : "") + (coupon.applied ? (" | Coupon: " + coupon.code) : ""),
            };
            voucherOpen.value = true;
            try { window.PSVLastVoucher = { raw: lastVoucher.value }; } catch (_e) {}
            showToast("Saved ✓ " + vno + (discTotal > 0 ? (" · Discount −" + fKs(discTotal)) : "") + (wallet_deduct > 0 ? (" · Wallet −" + wallet_deduct + "m") : ""), "success");
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
            emit("saved");
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
          <van-field v-model.number="form.amount" type="number" label="Amount (Ks)" placeholder="0" />
          <van-field v-model.number="form.kpay" type="number" label="KPay (Ks)" placeholder="0" />
          <van-field v-model.number="form.cash" type="number" label="Cash (Ks)" placeholder="0" />
          <van-field v-model="form.referral_code" label="Referral Code" placeholder="(optional)" />
        </van-cell-group>
        <div style="padding:14px 16px">
          <van-button type="primary" block :loading="submitting" @click="submit">Register</van-button>
        </div>
      </div>
    `,
    setup() {
      const form = reactive({ member_id: "", name: "", phone: "", initial_mins: 0, amount: 0, kpay: 0, cash: 0, referral_code: "" });
      const submitting = ref(false);
      async function nextId() { try { const id = PSV.unwrap(await PSV.api("/api/next_member_id")); if (id) form.member_id = String(id); } catch (_e) {} }
      onMounted(nextId);
      async function submit() {
        submitting.value = true; haptic("medium");
        try {
          const res = PSV.unwrap(await PSV.api("/api/members/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member_id: form.member_id, name: form.name, phone: form.phone, staff: PSV.getStaff().name, initial_mins: form.initial_mins, amount: form.amount, kpay: form.kpay, cash: form.cash, mins_added: form.initial_mins, referral_code: form.referral_code }) }));
          showToast("Saved ✓ " + (res && res.member_id ? res.member_id : ""), "success");
          form.name = ""; form.phone = ""; form.initial_mins = 0; form.amount = 0; form.kpay = 0; form.cash = 0; form.referral_code = "";
          nextId();
        } catch (e) { showToast("Error: " + e.message, "fail"); }
        finally { submitting.value = false; }
      }
      return { form, submitting, submit };
    },
  };

  // ── WAVE 1: booking ops + deposit helpers ───────────────
  const BK_REJECT_REASONS = [
    { text: "အချိန်မရ (slot မအား)", note: "Slot unavailable", value: "slot_unavailable" },
    { text: "ငွေမဆပ် (deposit မရ)", note: "Deposit not received", value: "payment_not_received" },
    { text: "အခြား", note: "Other", value: "other" },
  ];
  const BK_CANCEL_REASONS = [
    { text: "ဖောက်သည် ကိုယ်တိုင် ဖျက်", note: "Customer cancelled", value: "customer_cancelled" },
    { text: "Console မအား / ပြောင်း", note: "Console unavailable", value: "console_unavailable" },
    { text: "ဖောက်သည် မလာ (no-show)", note: "No-show", value: "no_show" },
    { text: "အခြား", note: "Other", value: "other" },
  ];
  const BK_STATUS = {
    pending:          { en: "Pending",    my: "စောင့်ဆိုင်",     tone: "warn" },
    pending_check_in: { en: "Confirmed",  my: "Check-in စောင့်", tone: "warn" },
    confirmed:        { en: "Confirmed",  my: "အတည်ပြု",       tone: "ok" },
    checked_in:       { en: "Checked-in", my: "ရောက်ရှိ",       tone: "ok" },
    active:           { en: "Active",     my: "ကစားနေ",         tone: "gold" },
    done:             { en: "Completed",  my: "ပြီးဆုံး",        tone: "dim" },
    completed:        { en: "Completed",  my: "ပြီးဆုံး",        tone: "dim" },
    cancelled:        { en: "Cancelled",  my: "ဖျက်ပြီး",        tone: "danger" },
    rejected:         { en: "Rejected",   my: "ငြင်းပယ်",        tone: "danger" },
  };
  function bkKey(s) { return String(s == null ? "" : s).toLowerCase().trim(); }
  function bkStatus(s) { return BK_STATUS[bkKey(s)] || { en: (s || "-"), my: "", tone: "dim" }; }
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
            cancelButtonText: cancelText || "မလုပ်",
            confirmButtonColor: danger ? "#f87171" : "#f0b429",
            messageAlign: "left",
          }).then(function () { resolve(true); }).catch(function () { resolve(false); });
          return;
        }
      } catch (_e) {}
      try { resolve(window.confirm(title + " — " + message)); } catch (_e2) { resolve(false); }
    });
  }

  // ── VIEW: Bookings (WAVE 1 — full staff ops + deposits) ─
  const Bookings = {
    components: { PageHeader },
    template: `
      <div class="pv-page psv-bk">
        <PageHeader title="Booking စာရင်း" />
        <div class="psv-bk-chips">
          <button v-for="c in chips" :key="c.id" class="psv-chip" :class="{ 'psv-chip--on': chip === c.id }" @click="setChip(c.id)">{{ c.icon }} {{ c.my }}</button>
        </div>
        <div class="psv-bk-hint">{{ chipHint }}</div>

        <van-pull-refresh v-model="refreshing" @refresh="load">
          <div v-if="loading" class="psv-pad">
            <div class="psv-skeleton psv-sk-card" v-for="n in 3" :key="n"></div>
          </div>
          <div v-else-if="!rows.length" class="psv-empty">
            <div style="font-size:34px">📭</div>
            <div style="margin-top:8px">{{ emptyText }}</div>
            <button class="psv-btn psv-btn--ghost" style="margin-top:14px" @click="goNew">➕ Booking အသစ်</button>
          </div>
          <div v-else class="psv-bk-list">
            <div v-for="b in rows" :key="b.id" class="psv-bk-card" :class="'is-' + bkStatus(b.status).tone">
              <div class="psv-bk-card__top" @click="openDetail(b)">
                <div class="psv-bk-card__who">
                  <div class="psv-bk-card__name">{{ b.customerName || b.member_id || 'Guest' }}</div>
                  <div class="psv-bk-card__sub">BK#{{ b.id }} · {{ b.console_id || b.consoleType || '-' }}</div>
                </div>
                <span class="psv-pill" :class="'psv-pill--' + bkStatus(b.status).tone">{{ bkStatus(b.status).my }}<i>{{ bkStatus(b.status).en }}</i></span>
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
                  <span class="psv-abtn__ico">{{ a.icon }}</span>{{ a.my }}
                </button>
                <button class="psv-abtn psv-abtn--ghost" :disabled="busy === b.id" @click.stop="openDetail(b)">⋯ အသေးစိတ်</button>
              </div>
            </div>
          </div>
        </van-pull-refresh>

        <!-- Booking detail + full action sheet -->
        <van-popup v-model:show="detailOpen" position="bottom" round class="psv-sheet" :style="{ maxHeight: '90vh' }">
          <div class="psv-sheet__handle"></div>
          <div class="psv-sheet__body" v-if="detail">
            <div class="psv-bk-detail__head">
              <div>
                <div class="psv-bk-detail__name">{{ detail.customerName || detail.member_id || 'Guest' }}</div>
                <div class="psv-bk-detail__id">BK#{{ detail.id }}</div>
              </div>
              <span class="psv-pill" :class="'psv-pill--' + bkStatus(detail.status).tone">{{ bkStatus(detail.status).my }}<i>{{ bkStatus(detail.status).en }}</i></span>
            </div>
            <div class="psv-card psv-rows">
              <div class="psv-row"><span>Console</span><b>{{ detail.console_id || detail.consoleType || '-' }}</b></div>
              <div class="psv-row"><span>ရက် / အချိန်</span><b>{{ detail.date }} · {{ fmt12h(detail.timeSlot || detail.startTime) }}</b></div>
              <div class="psv-row"><span>ကြာချိန်</span><b>{{ detail.durationMins || '-' }} မိနစ်</b></div>
              <div class="psv-row" v-if="detail.gameName"><span>ဂိမ်း</span><b>{{ detail.gameName }}</b></div>
              <div class="psv-row" v-if="detail.phone"><span>ဖုန်း</span><b>{{ detail.phone }}</b></div>
              <div class="psv-row" v-if="detail.member_id"><span>Member</span><b>{{ detail.member_id }}</b></div>
              <div class="psv-row" v-if="detail.deposit_status && detail.deposit_status !== 'none'"><span>Deposit</span><b>{{ depositText(detail) }}</b></div>
            </div>
            <div v-if="detail.notes" class="psv-card">
              <div class="psv-field-label">မှတ်ချက်</div>
              <div style="font-size:13px;line-height:1.6">{{ detail.notes }}</div>
            </div>
            <div class="psv-section-title">လုပ်ဆောင်ချက်များ</div>
            <div class="psv-bk-detail__grid">
              <button v-for="a in detailActions(detail)" :key="a.id" class="psv-abtn psv-abtn--block" :class="'psv-abtn--' + a.tone" :disabled="busy === detail.id" @click="runAction(a, detail)">
                <span class="psv-abtn__ico">{{ a.icon }}</span>{{ a.my }}<i v-if="a.en">{{ a.en }}</i>
              </button>
              <div v-if="!detailActions(detail).length" class="psv-empty" style="padding:16px">လုပ်ဆောင်စရာ မရှိတော့ပါ</div>
            </div>
          </div>
        </van-popup>

        <!-- Reject / cancel reason picker -->
        <van-popup v-model:show="reasonOpen" position="bottom" round class="psv-sheet">
          <div class="psv-sheet__handle"></div>
          <div class="psv-sheet__body">
            <div class="psv-sheet-title">{{ reasonMode === 'reject' ? '❌ ငြင်းပယ်ရသည့် အကြောင်းရင်း' : '⛔ ဖျက်ရသည့် အကြောင်းရင်း' }}</div>
            <div class="psv-reason" v-for="r in reasonList" :key="r.value" :class="{ 'is-on': reasonPick === r.value }" @click="reasonPick = r.value">
              <div class="psv-reason__radio"></div>
              <div class="psv-reason__main"><div class="psv-reason__t">{{ r.text }}</div><div class="psv-reason__s">{{ r.note }}</div></div>
            </div>
            <van-field v-if="reasonPick === 'other'" v-model="reasonNote" rows="2" autosize type="textarea" placeholder="အကြောင်းရင်း ရေးပါ…" />
            <div class="psv-btn-row" style="margin-top:12px">
              <button class="psv-btn psv-btn--ghost psv-btn--block" @click="reasonOpen = false">မလုပ်တော့</button>
              <button class="psv-btn psv-btn--danger psv-btn--block" :disabled="!reasonPick" @click="confirmReason">အတည်ပြု</button>
            </div>
          </div>
        </van-popup>

        <!-- Deposit proof -->
        <van-popup v-model:show="proofOpen" position="bottom" round class="psv-sheet">
          <div class="psv-sheet__handle"></div>
          <div class="psv-sheet__body">
            <div class="psv-sheet-title">👁 Deposit Proof — BK#{{ proof.booking_id }}</div>
            <img v-if="proof.isImg" :src="proof.ref" class="psv-proof-img" />
            <div v-else class="psv-card"><div class="psv-field-label">Ref / အချက်အလက်</div><div style="font-size:14px;word-break:break-all">{{ proof.ref }}</div></div>
            <button class="psv-btn psv-btn--primary psv-btn--block" style="margin-top:12px" @click="proofOpen = false">ပိတ်</button>
          </div>
        </van-popup>

        <!-- New booking (kept from previous pass) -->
        <van-popup v-model:show="newOpen" position="bottom" round class="psv-sheet" :style="{ height: '92vh' }">
          <div class="psv-sheet__handle"></div>
          <div style="overflow:auto;height:calc(92vh - 20px)"><BookingForm @saved="onSaved" /></div>
        </van-popup>
      </div>
    `,
    setup() {
      const CHIPS = [
        { id: "today", icon: "📅", my: "ဒီနေ့", hint: "ဒီနေ့ Booking အားလုံး" },
        { id: "open", icon: "🔓", my: "ပွင့်", hint: "Pending · Confirmed · Checked-in · Active" },
        { id: "pending", icon: "⏳", my: "စောင့်ဆိုင်", hint: "Approve / Reject လုပ်ရန် စောင့်နေသည်" },
        { id: "confirmed", icon: "✅", my: "အတည်ပြု", hint: "အတည်ပြုပြီး — ဖောက်သည် လာရင် Check-in လုပ်ပါ" },
        { id: "checked_in", icon: "🎟", my: "ရောက်ရှိ", hint: "ရောက်ရှိပြီး — Console မှာ Session စရန်" },
        { id: "active", icon: "▶️", my: "ကစားနေ", hint: "Session လည်ပတ်နေသည်" },
        { id: "deposit", icon: "💰", my: "Deposit", hint: "Deposit စစ်ဆေး / လဲလှယ်ရန်" },
      ];
      const chip = ref("open");
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
      const proof = reactive({ ref: "", isImg: false, booking_id: "" });

      const chipMeta = computed(() => CHIPS.find((c) => c.id === chip.value) || CHIPS[1]);
      const chipHint = computed(() => chipMeta.value.hint);
      const emptyText = computed(() => (chip.value === "deposit" ? "Deposit စစ်ဆေးရန် မရှိပါ" : "Booking မရှိပါ"));
      const reasonList = computed(() => (reasonMode.value === "reject" ? BK_REJECT_REASONS : BK_CANCEL_REASONS));

      function hasDepositPending(b) { return ["paid", "pending"].includes(bkKey(b.deposit_status)); }
      function isVerified(b) { return bkKey(b.deposit_status) === "verified"; }
      function depRef(b) { return b.deposit_ref || b.depositRef || b.deposit_slip || ""; }
      function depositText(b) {
        const s = bkKey(b.deposit_status);
        const amount = b.deposit_amount ? Number(b.deposit_amount).toLocaleString() : "";
        const method = (b.deposit_method || "").toUpperCase();
        const lbl = amount ? amount + (method ? " " + method : "") : "-";
        const map = { paid: "ရရှိ (စစ်ရန်) " + lbl, pending: "စောင့်ဆိုင် " + lbl, verified: "စစ်ပြီး " + lbl, redeemed: "သုံးပြီး " + lbl, forfeited: "ပြန်မအမ်း " + lbl, refunded: "ပြန်အမ်း " + lbl };
        return map[s] || ("Deposit " + lbl);
      }
      function cardActions(b) {
        const k = bkKey(b.status);
        const out = [];
        if (k === "pending") {
          out.push({ id: "approve", my: "အတည်ပြု", en: "Approve", icon: "✅", tone: "ok", kind: "approve" });
          out.push({ id: "reject", my: "ငြင်းပယ်", en: "Reject", icon: "❌", tone: "danger", kind: "reject" });
          if (hasDepositPending(b)) out.push({ id: "verify", my: "Deposit စစ်", en: "Verify", icon: "💰", tone: "gold", kind: "verify" });
          out.push({ id: "cancel", my: "ဖျက်", en: "Cancel", icon: "⛔", tone: "danger", kind: "cancel" });
        } else if (k === "confirmed" || k === "pending_check_in") {
          out.push({ id: "checkin", my: "Check-in", en: "Arrived", icon: "🎟", tone: "ok", kind: "checkin" });
          if (hasDepositPending(b)) out.push({ id: "verify", my: "Deposit စစ်", en: "Verify", icon: "💰", tone: "gold", kind: "verify" });
          out.push({ id: "cancel", my: "ဖျက်", en: "Cancel", icon: "⛔", tone: "danger", kind: "cancel" });
        } else if (k === "checked_in") {
          out.push({ id: "consoles", my: "Session စ", en: "Start", icon: "▶️", tone: "gold", kind: "consoles" });
          out.push({ id: "cancel", my: "ဖျက်", en: "Cancel", icon: "⛔", tone: "danger", kind: "cancel" });
        }
        return out;
      }
      function detailActions(b) {
        const out = cardActions(b);
        if (isVerified(b)) out.push({ id: "redeem", my: "Sale မှာ နုတ်", en: "Redeem at sale", icon: "↩️", tone: "gold", kind: "redeem" });
        if (depRef(b)) out.push({ id: "proof", my: "ငွေလွှဲ အထောက်အထား", en: "View proof", icon: "👁", tone: "ghost", kind: "proof" });
        if (bkKey(b.status) === "active") out.push({ id: "consoles", my: "Session ကြည့်", en: "View session", icon: "🖥", tone: "ghost", kind: "consoles" });
        return out;
      }

      async function fetchStatus(s) { const d = PSV.unwrap(await PSV.api("/api/bookings?status=" + encodeURIComponent(s))); return (d && d.bookings) || []; }
      async function fetchDate(dt) { const d = PSV.unwrap(await PSV.api("/api/bookings?date=" + encodeURIComponent(dt))); return (d && d.bookings) || []; }
      async function load() {
        loading.value = true;
        try {
          let list = [];
          if (chip.value === "today") list = await fetchDate(PSV.todayMMT());
          else if (chip.value === "open") {
            const arrs = await Promise.all([fetchStatus("pending"), fetchStatus("confirmed"), fetchStatus("checked_in"), fetchStatus("active")]);
            list = arrs.reduce((a, x) => a.concat(x), []);
          } else if (chip.value === "deposit") {
            const arrs = await Promise.all([fetchStatus("pending"), fetchStatus("confirmed")]);
            list = arrs.reduce((a, x) => a.concat(x), []).filter(hasDepositPending);
          } else {
            list = await fetchStatus(chip.value);
          }
          const seen = {};
          list = list.filter((b) => { if (seen[b.id]) return false; seen[b.id] = 1; return true; });
          list.sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")) || String(b.timeSlot || "").localeCompare(String(a.timeSlot || "")));
          rows.value = list;
        } catch (e) {
          rows.value = [];
          showToast("Load မရ: " + e.message, "fail");
        } finally { loading.value = false; refreshing.value = false; }
      }
      function setChip(id) { haptic("light"); chip.value = id; load(); }
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
        else showToast("BK#" + id + " မတွေ့ပါ", "fail");
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
        const okc = await confirmBox("အတည်ပြုမလား?", "BK#" + b.id + " (" + (b.customerName || "Guest") + ") ကို Confirmed လုပ်မယ်။ ဖောက်သည်ကို Telegram မှ အသိပေးမယ်။", "အတည်ပြု", "မလုပ်");
        if (!okc) return;
        busy.value = b.id;
        try {
          await PSV.api("/api/bookings/" + b.id + "/status", { method: "PATCH", headers: HJSON, body: JSON.stringify({ status: "confirmed", staffName: psvStaffName() }) });
          haptic("medium"); showToast("အတည်ပြုပြီး ✓", "success");
          await afterMutation(b.id, { status: "confirmed" });
        } catch (e) { showToast("မအောင်မြင်: " + e.message, "fail"); }
        finally { busy.value = null; }
      }
      async function doCheckin(b) {
        const okc = await confirmBox("Check-in လုပ်မလား?", "BK#" + b.id + " ဖောက်သည် ရောက်ရှိကြောင်း မှတ်မယ် (ကစားချိန် မစသေးပါ)။", "Check-in", "မလုပ်");
        if (!okc) return;
        busy.value = b.id;
        try {
          await PSV.api("/api/bookings/checkin", { method: "POST", headers: HJSON, body: JSON.stringify({ id: b.id, staff_name: psvStaffName() }) });
          haptic("medium"); showToast("Check-in ပြီး 🎟", "success");
          await afterMutation(b.id, { status: "checked_in" });
        } catch (e) { showToast("မအောင်မြင်: " + e.message, "fail"); }
        finally { busy.value = null; }
      }
      async function doVerify(b) {
        const amt = Number(b.deposit_amount) || 0;
        const okc = await confirmBox("Deposit စစ်ဆေးမလား?", "BK#" + b.id + " · " + amt.toLocaleString() + " Ks (" + (b.deposit_method || "").toUpperCase() + ")။ Verified လုပ်ပြီး Booking ကို Confirmed လုပ်မယ်။ ငွေဝင်စာရင်း (cash inject) ဖြစ်မယ်။", "Verified", "မလုပ်");
        if (!okc) return;
        busy.value = b.id;
        try {
          await PSV.api("/api/bookings/" + b.id + "/deposit/verify", { method: "PATCH", headers: HJSON, body: JSON.stringify({ verified_by: psvStaffName() }) });
          haptic("medium"); showToast("Deposit စစ်ပြီး 💰", "success");
          await afterMutation(b.id, { deposit_status: "verified", status: "confirmed" });
        } catch (e) { showToast("မအောင်မြင်: " + e.message, "fail"); }
        finally { busy.value = null; }
      }
      async function doRedeem(b) {
        try {
          const d = PSV.unwrap(await PSV.api("/api/bookings/" + b.id + "/deposit/pre-redeem", { method: "POST" }));
          const amt = Number((d && d.deposit_amount) || 0);
          if (!amt) { showToast("Verified deposit မရှိပါ", "warning"); return; }
          try { PSV.pendingDepositRedeem = { booking_id: b.id, amount: amt, method: (d && d.deposit_method) || "", ts: Date.now() }; } catch (_e) {}
          haptic("light");
          showToast("Deposit " + amt.toLocaleString() + " Ks — Sale မှာ ချိတ်ဆက်ထားပြီ", "success");
          detailOpen.value = false;
          router.push("/sales");
        } catch (e) { showToast("မအောင်မြင်: " + e.message, "fail"); }
      }
      function showProof(b) {
        const ref = depRef(b);
        if (!ref) { showToast("Proof ref မရှိသေးပါ — API မှ deposit_ref မပေးသေး", "warning"); return; }
        proof.ref = ref;
        proof.isImg = /^https?:\/\//i.test(ref) || /\.(png|jpe?g|webp|gif)$/i.test(ref);
        proof.booking_id = b.id;
        proofOpen.value = true;
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
          ? "BK#" + b.id + " ကို ငြင်းပယ်မယ် · ဖောက်သည်ကို အသိပေးမယ် · Paid deposit ရှိရင် ပြန်အမ်းရန် လိုအပ်နိုင်တယ်။"
          : "BK#" + b.id + " ကို ဖျက်မယ် · ဖောက်သည်ကို အသိပေးမယ် · Verified deposit ရှိရင် ပြန်မအမ်းဘဲ forfeit ဖြစ်မယ် (ဘောက်ချာ မရ)။";
        const okc = await confirmBox(isReject ? "ငြင်းပယ်မလား?" : "Booking ဖျက်မလား?", conseq + " အကြောင်းရင်း: " + label, isReject ? "ငြင်းပယ်" : "ဖျက်", "မလုပ်", true);
        if (!okc) return;
        busy.value = b.id;
        try {
          if (isReject) {
            await PSV.api("/api/bookings/" + b.id + "/status", { method: "PATCH", headers: HJSON, body: JSON.stringify({ status: "rejected", staffNote: label, staffName: psvStaffName() }) });
            showToast("ငြင်းပယ်ပြီး ❌", "success");
            await afterMutation(b.id, { status: "rejected" });
          } else {
            await PSV.api("/api/bookings/cancel", { method: "POST", headers: HJSON, body: JSON.stringify({ id: b.id, reason: label }) });
            showToast("ဖျက်ပြီး ⛔", "success");
            await afterMutation(b.id, { status: "cancelled" });
          }
        } catch (e) { showToast("မအောင်မြင်: " + e.message, "fail"); }
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
        if (a.kind === "consoles") { haptic("light"); detailOpen.value = false; router.push("/consoles"); return; }
      }

      onMounted(async () => {
        await load();
        const pid = pendingBooking.value;
        if (pid) { pendingBooking.value = null; await openById(pid); }
      });
      watch(pendingBooking, async (pid) => { if (pid) { pendingBooking.value = null; await openById(pid); } });

      return { chips: CHIPS, chip, chipHint, emptyText, rows, loading, refreshing, busy, detail, detailOpen, newOpen,
        reasonOpen, reasonMode, reasonPick, reasonNote, reasonList, proofOpen, proof,
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
        <van-popup v-model:show="memberPicker" position="bottom" round>
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
        <van-popup v-model:show="gamePicker" position="bottom" round>
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
      return { form, consoles, consolePicker, depPicker, gamePicker, timePicker, memberPicker, members, memberQuery, filteredMembers, submitting, depColumns, gameColumns, timeColumns, fmt12h, fmtDepositMethod, openConsole, pickConsole, openMember, pickMember, clearMember, pickDep, openGame, pickGame, pickTime, submit };
    },
  };

  // ── VIEW: More — category-organized staff menu (WAVE 1) ──
  // App mode: bilingual, category sections, roadmap items greyed "နောက် pass".
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
    { path: "/notifications", icon: "🔔", label: "Alerts" },
  ];
  const MENU_GROUPS = [
    { id: "sales", emoji: "🎮", my: "အရောင်း", en: "Sales", tone: "gold", items: [
      { my: "အရောင်းတင်ရန်", en: "New walk-in sale", icon: "🛒", path: "/sales" },
      { my: "Session ပြီးအရောင်း", en: "End-of-session sale", icon: "🎫", path: "/sales" },
      { my: "အစားအသောက် အရောင်း", en: "Food & drink sale", icon: "🍔", path: "/food-orders" },
      { my: "အစားအသောက် စာရင်း", en: "Food menu list", icon: "🍜", path: "/food" },
      { my: "Discount / Promo", en: "View promotions", icon: "🏷️", path: "/promotions" },
      { my: "Coupon", en: "Validate & redeem coupon", icon: "🎟️", path: "/sales" },
    ] },
    { id: "booking", emoji: "📅", my: "Booking", en: "Bookings", tone: "warn", items: [
      { my: "ဒီနေ့ Booking စာရင်း", en: "Today's bookings", icon: "📋", path: "/bookings" },
      { my: "အတည်ပြု / ငြင်းပယ်", en: "Approve or reject", icon: "✅", path: "/bookings" },
      { my: "Check-in", en: "Customer arrived", icon: "🎟️", path: "/bookings" },
      { my: "Booking ဖျက်", en: "Cancel a booking", icon: "⛔", path: "/bookings" },
      { my: "Deposit စစ်ရန်", en: "Verify deposit", icon: "💰", path: "/bookings" },
      { my: "Reminder", en: "Booking reminders", icon: "🔔", path: "/notifications" },
    ] },
    { id: "sessions", emoji: "🖥", my: "Session / Console", en: "Sessions & consoles", tone: "indigo", items: [
      { my: "အလုပ်လုပ်နေတဲ့ Session များ", en: "Live sessions", icon: "🖥", path: "/consoles" },
      { my: "အချိန်တိုး", en: "Extend session", icon: "⏱", path: "/consoles" },
      { my: "Session ပိတ်", en: "End session", icon: "⏹", path: "/consoles" },
      { my: "ဂိမ်းစာရင်း", en: "Game library", icon: "🎮", path: "/games" },
      { my: "Console ပြောင်း (Move)", en: "Move session to another console", icon: "🔀", tag: "နောက် pass" },
      { my: "Session လဲ (Swap)", en: "Swap two sessions", icon: "🔁", tag: "နောက် pass" },
    ] },
    { id: "member", emoji: "👥", my: "အဖွဲ့ဝင်", en: "Members", tone: "teal", items: [
      { my: "အဖွဲ့ဝင် ရှာရန်", en: "Find a member", icon: "🔍", path: "/members" },
      { my: "အဖွဲ့ဝင် အသစ်ဖွင့်", en: "Register new member", icon: "➕", path: "/members/register" },
      { my: "ငွေဖြည့် (Top-up)", en: "Wallet top-up", icon: "💳", path: "/deposit" },
      { my: "Gift / Free card", en: "PIN-gated free card", icon: "🎁", tag: "နောက် pass" },
      { my: "Rank / Bonus", en: "Rank & bonus table", icon: "🏅", tag: "နောက် pass" },
      { my: "ဖုန်းဆက်ရန်", en: "Call member", icon: "📞", tag: "နောက် pass" },
    ] },
    { id: "inventory", emoji: "📦", my: "Inventory", en: "Stock", tone: "gold", items: [
      { my: "Stock-in (ပစ္စည်းဝင်)", en: "Receive stock", icon: "📥", path: "/inventory" },
      { my: "Stock-out (ပစ္စည်းထုတ်)", en: "Manual stock-out", icon: "📤", tag: "နောက် pass" },
      { my: "ကုန်နီးပါး Alert", en: "Low-stock alerts", icon: "⚠️", tag: "နောက် pass" },
      { my: "Stock check", en: "Stock reconcile", icon: "🧮", tag: "နောက် pass" },
    ] },
    { id: "print", emoji: "🖨", my: "Print", en: "Printing", tone: "indigo", items: [
      { my: "ဘောက်ချာ ရိုက်ရန်", en: "Print voucher", icon: "🧾", path: "/print" },
      { my: "စမ်းသပ် Print", en: "Test print", icon: "🖨", path: "/print" },
      { my: "Top-up ပြေစာ", en: "Top-up receipt", icon: "📃", path: "/deposit" },
    ] },
    { id: "report", emoji: "📊", my: "Report", en: "Reports", tone: "teal", items: [
      { my: "ဒီနေ့ အနှစ်ချုပ်", en: "Today's summary", icon: "📊", path: "/reports" },
      { my: "အသုံးစရိတ် (OPEX)", en: "Operating expenses", icon: "💸", path: "/opex" },
      { my: "ငွေလက်ကျန်", en: "Account balance", icon: "🏦", tag: "နောက် pass" },
    ] },
    { id: "me", emoji: "🕒", my: "Me / Staff", en: "Me & staff", tone: "dim", items: [
      { my: "အလုပ်ချိန် (Attendance)", en: "Attendance in / out", icon: "🕐", path: "/attendance" },
      { my: "လစာ ကြိုယူ", en: "Salary advance", icon: "💰", path: "/salary" },
      { my: "ဝန်ထမ်းများ", en: "Staff list", icon: "👥", path: "/staff" },
      { my: "သတိပေးချက်", en: "Notifications", icon: "🔔", path: "/notifications" },
      { my: "ဆက်တင်", en: "Settings & profile", icon: "⚙️", path: "/me" },
      { my: "ထွက်ရန် (Logout)", en: "Sign out", icon: "🚪", action: "logout" },
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
            <span class="psv-menu-head__my">{{ g.my }}</span>
            <span class="psv-menu-head__en">{{ g.en }}</span>
          </div>
          <div class="psv-card psv-list psv-menu-list">
            <div v-for="it in g.items" :key="it.my" class="psv-menu-item" :class="{ 'is-soon': it.tag }" @click="tap(it)">
              <span class="psv-menu-item__ico">{{ it.icon }}</span>
              <div class="psv-menu-item__main">
                <div class="psv-menu-item__my">{{ it.my }}</div>
                <div class="psv-menu-item__en">{{ it.en }}</div>
              </div>
              <span v-if="it.tag" class="psv-soon">{{ it.tag }}</span>
              <van-icon v-else name="arrow" />
            </div>
          </div>
        </div>
        <button class="psv-btn psv-btn--danger psv-btn--block" @click="logout">🚪 ထွက်ရန် (Logout)</button>
        <div class="psv-foot">PS VIBE Staff · Play The Game. Share The VIBE!</div>
      </div>
    `,
    setup() {
      const staff = PSV.getStaff() || {};
      const initials = String(staff.name || "?").trim().slice(0, 2).toUpperCase();
      function tap(it) {
        haptic("light");
        if (it.tag) { showToast("⏳ " + it.my + " — နောက် pass မှာ လာမယ်", "text"); return; }
        if (it.action === "logout") { logout(); return; }
        if (it.path) router.push(it.path);
      }
      function logout() { haptic("medium"); PSV.logout(); showToast("ထွက်ပြီးပါပြီ", "success"); }
      return { groups: MENU_GROUPS, staff, initials, tap, logout };
    },
  };

  const More = {
    components: { PageHeader, MoreMenu },
    template: `
      <div class="pv-page psv-menu">
        <PageHeader :title="nativeMode ? 'မီနူး' : 'More'" />
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

        <van-dialog v-model:show="startShow" title="Start Session" show-cancel-button @confirm="submitStart" :before-close="noop">
          <div style="padding:14px 16px 6px">
            <van-cell v-if="startBind.booking" :title="'📌 Checked-in: ' + (startBind.booking.member_id || startBind.booking.customer_name || 'BK#' + startBind.booking.id)" :label="'BK#' + startBind.booking.id + (startBind.booking.game_name ? ' · ' + startBind.booking.game_name : '') + (startBind.booking.start_time ? ' · ' + fmtTime(startBind.booking.start_time) : '')" is-link @click="bindCheckin()" />
            <van-field :model-value="startForm.member_name || 'Select member…'" label="Member Name" readonly is-link @click="startMemberPicker = true" />
            <van-field v-model.number="startForm.duration_mins" type="number" label="Minutes" placeholder="60" />
            <van-field :model-value="startForm.game_name || 'Select game…'" label="Game" readonly is-link @click="startGamePicker = true" />
          </div>
        </van-dialog>

        <van-popup v-model:show="startMemberPicker" position="bottom" round>
          <van-picker :columns="startMemberCols" title="Select Member" @confirm="pickStartMember" @cancel="startMemberPicker=false" />
        </van-popup>
        <van-popup v-model:show="startGamePicker" position="bottom" round>
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
              <span style="color:var(--pv-muted);font-size:12px;flex:1">qty ကို ရွေးပြီး Save နှိပ်ပါ — အရင်မှာထားတာတွေ မထိဘဲ ထပ်ထည့်ပါမယ်</span>
              <span style="font-weight:700;color:var(--pv-primary)">{{ fKs(foodSel.reduce((s,x)=>s+(Number(x.qty)||0)*(Number(x.price)||0),0)) }} Ks</span>
            </div>
            <div style="padding:14px 16px;display:flex;gap:8px">
              <van-button block plain @click="foodShow=false">Cancel</van-button>
              <van-button block type="primary" :loading="foodSaving" :disabled="!foodSel.length" @click="saveSessionFood">{{ foodSel.length ? ('Add ' + foodSel.length + ' item(s)') : 'Select items to add' }}</van-button>
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
      const startMemberPicker = ref(false);
      const startGamePicker = ref(false);
      const startMemberCols = ref([]);
      const startGameCols = ref([]);
      const startBind = ref({ booking: null });
      const endDlg = reactive({ show: false, title: "", booking_id: null, console_id: "", member_name: "", mins: 0 });
      const extendDlg = reactive({ show: false, console_id: "", extra: 60 });
      const noop = () => true;

      function dotClass(s) { return ({ Active: "active", Free: "free", Reserved: "reserved", Busy: "busy", Maintenance: "maintenance" })[s] || "idle"; }
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
      onMounted(load);

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
        } else if (action.value === "end") {
          confirmEnd(c);
        }
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
              foodNote.value = "အရင်မှာထားပြီးသား " + items.length + " items ရှိ — ထပ်ထည့်ချင်တာတွေ ရွေးပြီး Save နှိပ်ပါ";
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
            startMemberCols.value = [{ text: "Guest (Walk-in)", value: "Guest" }].concat(ms.map((m) => ({ text: (m.name || m.id) + (m.id && m.name && m.id !== m.name ? "  (" + m.id + ")" : ""), value: m.id || m.name })));
          } catch (e) { showToast("Member load failed", "fail"); }
        }
        // game dropdown options
        if (!startGameCols.value.length) {
          try {
            const d = PSV.unwrap(await PSV.api("/api/fetch_games"));
            const gs = (d && d.games) || [];
            startGameCols.value = gs.filter((g) => g.game_title).map((g) => ({ text: g.game_title, value: g.game_title }));
          } catch (e) { /* optional */ }
        }
        // checked-in booking for this console (bind candidate)
        const c = activeConsole.value;
        if (!c) return;
        try {
          const d = PSV.unwrap(await PSV.api("/api/get-confirmed-booking?console_id=" + encodeURIComponent(c.console_id || c.id)));
          if (d && d.booking) startBind.value = { booking: d.booking };
        } catch (e) { /* none */ }
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

      function bindCheckin() {
        const b = startBind.value.booking;
        if (!b) { showToast("No checked-in booking to bind", "warning"); return; }
        startForm.member_id = b.member_id || b.customer_name || "";
        startForm.member_name = b.member_id || b.customer_name || ("BK#" + b.id);
        if (b.game_name) startForm.game_name = b.game_name;
        if (b.duration_mins && Number(b.duration_mins) > 0) startForm.duration_mins = Number(b.duration_mins);
        startForm.linked_booking_id = b.id;
        haptic("medium");
        showToast("Bound to BK#" + b.id + " ✓", "success");
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
        try {
          await PSV.api("/api/sessions/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              console_id: c.console_id || c.id,
              member_id: startForm.member_id || undefined,
              member_name: name,
              duration_mins: isNaN(dur) ? 0 : dur,
              game_name: (startForm.game_name || "").trim(),
              linked_booking_id: startForm.linked_booking_id || undefined,
              staff_name: PSV.getStaff().name,
            }),
          });
          showToast("Session started ✓", "success");
          startShow.value = false;
          load();
        } catch (e) { showToast("Start failed: " + e.message, "fail"); }
      }

      return { list, loading, refreshing, dotClass, isActive, fmtTime, load, sheetShow, sheetActions, openActions, onAction, startShow, startForm, startMemberPicker, startGamePicker, startMemberCols, startGameCols, startBind, endDlg, extendDlg, noop, submitStart, doEndSession, doExtend, loadStartOptions, pickStartMember, pickStartGame, bindCheckin, foodShow, foodConsole, foodNote, sessionFoodGroups, foodSel, sQty, sSetQty, foodSaving, saveSessionFood, openSessionFood, fKs };
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
            <div style="padding:0 16px 8px;color:var(--pv-muted);font-size:12px">qty 0 ထားရင် ဖျက်ပါမယ် — Save နှိပ်ပါ</div>
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
            <van-empty v-if="!editSel.length" description="No items — Consoles ကနေ ထပ်ထည့်ပါ" />
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
            <div style="padding:14px 16px">
              <van-button type="primary" block :loading="submitting" @click="submit">Stock In ({{ rows.length }} item{{ rows.length === 1 ? '' : 's' }})</van-button>
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
        else { row.isNew = false; row.item_name = val; }
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
      return { tab, stock, loading, refreshing, payPicker, rowItemPicker, rowItemPickerShow, submitting, payColumns, itemColumns, rows, form, addRow, loadStock, pickPay, pickRowItem, submit, fKs };
    },
  };

  // ── VIEW: Reports (today) ───────────────────────────────
  const Reports = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="Reports" back="/more" />
        <van-cell-group inset style="margin-top:8px" v-if="stats">
          <van-cell title="Today's Bookings" :value="String(stats.today_bookings ?? '-')" />
          <van-cell title="Active Players" :value="String(stats.active_players ?? '-')" />
          <van-cell title="Today's Revenue" :value="fKs(stats.today_revenue) + ' Ks'" />
          <van-cell title="Total Members" :value="String(stats.total_members ?? '-')" />
        </van-cell-group>
        <van-empty v-else description="Data unavailable (dashboard login required)" />
      </div>
    `,
    setup() {
      const stats = ref(null);
      async function load() {
        try {
          const d = PSV.unwrap(await PSV.api("/api/dashboard/stats"));
          stats.value = d || null;
        } catch (e) { stats.value = null; }
      }
      onMounted(load);
      return { stats, fKs };
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
      async function checkin(status) {
        haptic("medium");
        try {
          await PSV.api("/api/save_attendance", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ staff_name: staffName.value, status }) });
          showToast("Checked in ✓", "success");
        } catch (e) { showToast("Error: " + e.message, "fail"); }
      }
      async function loadAdvances() {
        try { const d = PSV.unwrap(await PSV.api("/api/fetch_salary_advances/" + month.value)); advances.value = (d && d.advances) || []; }
        catch (e) { advances.value = []; }
        advancesLoaded.value = true;
      }
      return { month, staffName, advances, advancesLoaded, checkin, loadAdvances, fKs };
    },
  };

  // ── VIEW: OPEX ──────────────────────────────────────────
  const Opex = {
    components: { PageHeader },
    template: `
      <div class="pv-page">
        <PageHeader title="OPEX" back="/more" />
        <van-notice-bar wrapable text="OPEX requires dashboard login" left-icon="info-o" />
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
        <van-notice-bar wrapable text="Salary/payroll requires dashboard login" left-icon="info-o" />
      </div>
    `,
    setup() { return {}; },
  };

  // ── Registration + boot ─────────────────────────────────
  function registerApp() {
    router.register("/", Home);
    router.register("/sales", Sales);
    router.register("/members", Members);
    router.register("/members/register", MemberRegister);
    router.register("/bookings", Bookings);
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
            <div class="psv-sheet-title">🧾 Top-up ပြေစာ</div>
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
              <button class="psv-btn psv-btn--ghost" @click="receiptOpen = false">ပိတ်</button>
              <button class="psv-btn psv-btn--primary" :disabled="printingReceipt" @click="printReceipt">{{ printingReceipt ? 'Printing…' : '🖨 ပြေစာ ရိုက်' }}</button>
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

          <div class="psv-section-title">80mm voucher preview</div>
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
      const printerName = ref("");
      const statusText = ref("Tap Check to query the printer");
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
      onMounted(() => { render(); checkStatus(); });
      return { png, printing, connected, printerName, statusText, checkStatus, testPrint, doPrint };
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
            <div class="psv-row"><span>Status</span><b>{{ attendance.status }}</b></div>
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
      const attendance = reactive({ status: "Not checked in" });
      function goPrint() { haptic("light"); router.push("/print"); }
      function goNotifications() { haptic("light"); router.push("/notifications"); }
      function load() {
        try {
          const info = window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.PSVShell;
          if (info && info.getAppInfo) { info.getAppInfo().then((a) => { if (a && a.versionName) appVersion.value = a.versionName; }).catch(() => {}); }
        } catch (_e) {}
        PSV.api("/api/sales/list-today").then((r) => { const d = PSV.unwrap(r); if (Array.isArray(d)) { summary.salesToday = d.length; summary.minsToday = d.reduce((a, x) => a + (Number(x.mins) || 0), 0); } }).catch(() => {});
      }
      async function checkUpdate() {
        try {
          const r = await fetch("/staff-app/version.json", { cache: "no-store" });
          if (r.ok) { const v = await r.json(); updateText.value = "Latest " + (v.version || "?"); }
          else updateText.value = "Up to date";
        } catch (_e) { updateText.value = "Up to date"; }
        haptic("light");
      }
      function onNotify() { haptic("light"); showToast(notify.value ? "Notifications on" : "Notifications off", "success"); }
      function logout() { haptic("medium"); PSV.logout(); showToast("Logged out", "success"); }
      onMounted(load);
      return { staff, initials, notify, appVersion, updateText, summary, attendance, goPrint, goNotifications, checkUpdate, onNotify, logout, fKs };
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
          <van-tabbar-item name="/more" icon="apps-o">မီနူး</van-tabbar-item>
        </van-tabbar>
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
      return { activeView, tab, needLogin, onLoggedIn, nativeMode: NATIVE };
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
    app.component("SaleForm", SaleForm);
    app.component("BookingForm", BookingForm);
    app.component("Home", Home);
    app.component("Sales", Sales);
    app.component("Members", Members);
    app.component("MemberRegister", MemberRegister);
    app.component("Bookings", Bookings);
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
