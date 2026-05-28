import re

with open('/root/psvibe_api_server/app.py', 'r') as f:
    content = f.read()

# Edit 1: Add HTMLResponse and Request to imports
old_import = 'from fastapi import FastAPI, HTTPException, Query, Depends\nfrom fastapi.middleware.cors import CORSMiddleware'
new_import = 'from fastapi import FastAPI, HTTPException, Query, Depends, Request\nfrom fastapi.middleware.cors import CORSMiddleware\nfrom fastapi.responses import HTMLResponse'
content = content.replace(old_import, new_import)

# Edit 2: Insert analytics + dashboard routes before STARTUP section
analytics_routes = '''
# ═══════════════════════════════════════
#  BI DASHBOARD — Analytics Endpoints
# ═══════════════════════════════════════
@app.get("/api/analytics/daily_sales", tags=["Analytics"])
async def api_analytics_daily_sales(
    date: str = Query(None, description="Date in M/D/YYYY format, defaults to today"),
    auth=Depends(verify_api_key),
):
    """Return today's (or specified date's) sales KPIs from Sales_Daily."""
    try:
        from analytics import get_daily_sales
        return ok(get_daily_sales(date))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/topups", tags=["Analytics"])
async def api_analytics_topups(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    auth=Depends(verify_api_key),
):
    """Return top-up trends: daily/weekly aggregates, all-time effective rate, top members."""
    try:
        from analytics import get_topup_trends
        return ok(get_topup_trends(days))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/member_activity", tags=["Analytics"])
async def api_analytics_member_activity(auth=Depends(verify_api_key)):
    """Return member activity stats: total members, tier distribution, active today, wallet totals."""
    try:
        from analytics import get_member_activity
        return ok(get_member_activity())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/console_usage", tags=["Analytics"])
async def api_analytics_console_usage(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    auth=Depends(verify_api_key),
):
    """Return console usage stats: bookings per console, utilization rate, daily series."""
    try:
        from analytics import get_console_usage
        return ok(get_console_usage(days))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/dashboard", tags=["Analytics"])
async def api_analytics_dashboard(auth=Depends(verify_api_key)):
    """Return full BI dashboard summary with all KPIs."""
    try:
        from analytics import get_dashboard_summary
        return ok(get_dashboard_summary())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytics/weekly_trends", tags=["Analytics"])
async def api_analytics_weekly_trends(
    weeks: int = Query(4, ge=1, le=52, description="Number of weeks to analyze"),
    auth=Depends(verify_api_key),
):
    """Return weekly trends: sales, top-ups, and console usage aggregated by week."""
    try:
        from analytics import get_topup_trends, get_console_usage, get_daily_sales
        days = weeks * 7
        topups = get_topup_trends(days)
        consoles = get_console_usage(days)
        return ok({
            "period_weeks": weeks,
            "topup_weekly": topups.get("weekly_aggregates", []),
            "topup_daily": topups.get("daily_series", []),
            "console_daily": consoles.get("daily_series", []),
            "console_summary": {
                "total_bookings": consoles["total_bookings"],
                "utilization_rate": consoles.get("avg_bookings_per_console_day", 0),
                "active_now": consoles["active_now"],
            },
            "all_time_rate": topups.get("all_time_effective_rate", 0),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════
#  BI DASHBOARD — Web Dashboard (HTML)
# ═══════════════════════════════════════
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>PS VIBE — BI Dashboard</title>
<style>
:root {
  --bg: #0a0e17;
  --card: #111827;
  --border: #1f2937;
  --text: #e5e7eb;
  --dim: #9ca3af;
  --accent: #6366f1;
  --green: #10b981;
  --amber: #f59e0b;
  --red: #ef4444;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: var(--bg); color: var(--text); line-height:1.5; }
.header { background: linear-gradient(135deg, #1e1b4b, #111827); padding: 1.5rem 2rem;
          border-bottom: 1px solid var(--border); display:flex; justify-content:space-between;
          align-items:center; flex-wrap:wrap; gap:1rem; }
.header h1 { font-size: 1.75rem; font-weight: 700; }
.header h1 span { color: var(--accent); }
.header .ts { font-size: 0.85rem; color: var(--dim); }
.container { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }
.kpi-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap:1rem; margin-bottom: 1.5rem; }
.kpi-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
            padding: 1.25rem; }
.kpi-card .label { font-size:0.8rem; color:var(--dim); text-transform:uppercase;
                   letter-spacing:0.05em; margin-bottom:0.25rem; }
.kpi-card .value { font-size:1.75rem; font-weight:700; }
.kpi-card .value.accent { color: var(--accent); }
.kpi-card .value.green { color: var(--green); }
.kpi-card .value.amber { color: var(--amber); }
.row { display:grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap:1.5rem; }
.panel { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
         overflow:hidden; margin-bottom:1.5rem; }
.panel h3 { padding:1rem 1.25rem; border-bottom:1px solid var(--border); font-size:1rem;
            font-weight:600; }
.panel-body { padding:1.25rem; }
table { width:100%; border-collapse:collapse; font-size:0.9rem; }
th, td { padding:0.6rem 0.75rem; text-align:left; border-bottom:1px solid var(--border); }
th { color: var(--dim); font-weight:500; font-size:0.8rem; text-transform:uppercase; }
tr:hover { background: rgba(255,255,255,0.02); }
.bar { height:8px; border-radius:4px; background:var(--border); overflow:hidden; margin-top:0.25rem; }
.bar-fill { height:100%; border-radius:4px; transition: width 0.5s; }
.loading { text-align:center; padding:3rem; color:var(--dim); }
.error { background: rgba(239,68,68,0.1); color: var(--red); padding:1rem; border-radius:8px; }
.footer { text-align:center; padding:2rem; color:var(--dim); font-size:0.8rem;
          border-top:1px solid var(--border); margin-top:2rem; }
</style>
</head>
<body>
<div class="header">
  <h1>🎮 PS <span>VIBE</span> — BI Dashboard</h1>
  <div class="ts" id="ts">Loading...</div>
</div>
<div class="container" id="app"><div class="loading">Loading dashboard data...</div></div>
<div class="footer">PS VIBE Gaming Lounge &copy; 2026 · BI Dashboard v3.0</div>
<script>
const F = (n) => n != null ? Number(n).toLocaleString() : '-';
const FKS = (n) => n != null ? Number(n).toLocaleString() + ' Ks' : '-';
const P = (n, t) => n != null && t > 0 ? (n/t*100).toFixed(1) + '%' : '0%';

async function load() {
  try {
    const r = await fetch(DASHBOARD_API_URL);
    const d = (await r.json()).data;
    if (!d) throw new Error('No data');
    render(d);
    document.getElementById('ts').textContent = 'Generated: ' + d.generated_at;
  } catch(e) {
    document.getElementById('app').innerHTML =
      '<div class="error">⚠ Failed to load dashboard: ' + e.message + '</div>';
  }
}

function render(d) {
  const s = d.summary, sales = d.daily_sales, mem = d.member_activity,
        con = d.console_usage_today, tup = d.topup_trends_7d;
  let h = '';
  h += '<div class="kpi-grid">';
  h += kpi('Today Sales', FKS(s.today_sales_ks), 'accent');
  h += kpi('Vouchers', F(s.today_vouchers));
  h += kpi('Avg Ticket', FKS(s.today_avg_ticket_ks));
  h += kpi('Active Members', F(s.active_members_today), 'green');
  h += kpi('Active Consoles', s.active_consoles + '/' + s.total_consoles);
  h += kpi('Week Top-ups', FKS(s.week_topup_ks), 'amber');
  h += '</div>';

  h += '<div class="row">';
  h += '<div class="panel"><h3>📊 Today\'s Sales Breakdown</h3><div class="panel-body">';
  if (sales.by_payment && Object.keys(sales.by_payment).length) {
    h += '<table><tr><th>Payment</th><th>Count</th><th>Amount</th></tr>';
    for (const [k,v] of Object.entries(sales.by_payment)) {
      h += '<tr><td>'+k+'</td><td>'+F(v.count)+'</td><td>'+FKS(v.amount)+'</td></tr>';
    }
    h += '</table>';
  } else { h += '<p style="color:var(--dim)">No sales recorded today</p>'; }
  h += '</div></div>';

  h += '<div class="panel"><h3>👥 Member Activity</h3><div class="panel-body">';
  h += '<table>';
  h += '<tr><td>Total Members</td><td><strong>'+F(mem.total_members)+'</strong></td></tr>';
  h += '<tr><td>Active Today</td><td><strong style="color:var(--green)">'+F(mem.active_today)+'</strong></td></tr>';
  h += '<tr><td>Active (Last 7d)</td><td>'+F(mem.active_last_7d)+'</td></tr>';
  h += '<tr><td>Total Wallet Mins</td><td>'+F(mem.total_wallet_mins)+'</td></tr>';
  h += '<tr><td>Avg Spend/Member</td><td>'+FKS(mem.avg_spend_per_member)+'</td></tr>';
  h += '</table>';
  if (mem.tier_distribution && mem.tier_distribution.length) {
    h += '<h4 style="margin:1rem 0 0.5rem">Tier Distribution</h4><table><tr><th>Tier</th><th>Members</th><th>Share</th><th></th></tr>';
    for (const t of mem.tier_distribution) {
      h += '<tr><td>'+t.tier+'</td><td>'+F(t.count)+'</td><td>'+t.pct+'%</td>';
      h += '<td style="width:120px"><div class="bar"><div class="bar-fill" style="width:'+t.pct+'%;background:var(--accent)"></div></div></td></tr>';
    }
    h += '</table>';
  }
  h += '</div></div>';
  h += '</div>';

  h += '<div class="row">';
  h += '<div class="panel"><h3>🎮 Console Usage (Today)</h3><div class="panel-body">';
  if (con.consoles && con.consoles.length) {
    h += '<table><tr><th>Console</th><th>Type</th><th>Bookings</th><th>Hours</th><th>Active</th></tr>';
    for (const c of con.consoles) {
      h += '<tr><td><strong>'+c.console_id+'</strong></td><td>'+c.type+'</td><td>'+F(c.total_bookings)+'</td>';
      h += '<td>'+c.total_hours+'h</td>';
      h += '<td>'+(c.active_bookings>0?'<span style="color:var(--green)">● Active</span>':'<span style="color:var(--dim)">○ Free</span>')+'</td></tr>';
    }
    h += '</table>';
  } else { h += '<p style="color:var(--dim)">No console data available</p>'; }
  h += '</div></div>';

  h += '<div class="panel"><h3>💰 Top-Up Trends (7 Days)</h3><div class="panel-body">';
  h += '<table>';
  h += '<tr><td>Total Top-ups</td><td><strong>'+F(tup.total_topups)+'</strong></td></tr>';
  h += '<tr><td>Total Amount</td><td><strong style="color:var(--amber)">'+FKS(tup.total_amount_ks)+'</strong></td></tr>';
  h += '<tr><td>Total Mins</td><td>'+F(tup.total_mins)+'</td></tr>';
  h += '<tr><td>All-Time Eff. Rate</td><td>'+tup.all_time_effective_rate+' Ks/min</td></tr>';
  h += '</table>';
  if (tup.top_members && tup.top_members.length) {
    h += '<h4 style="margin:1rem 0 0.5rem">Top Top-Up Members</h4>';
    h += '<table><tr><th>#</th><th>Member</th><th>Amount</th><th>Mins</th><th>Rate</th></tr>';
    tup.top_members.slice(0,5).forEach((m,i) => {
      h += '<tr><td>'+(i+1)+'</td><td><strong>'+m.member_id+'</strong></td><td>'+FKS(m.amount)+'</td><td>'+F(m.mins)+'</td><td>'+m.rate+' Ks/min</td></tr>';
    });
    h += '</table>';
  }
  h += '</div></div>';
  h += '</div>';

  if (tup.daily_series && tup.daily_series.length) {
    h += '<div class="panel"><h3>📈 Daily Top-Up Trend</h3><div class="panel-body">';
    h += '<table><tr><th>Date</th><th>Count</th><th>Amount</th><th>Mins</th><th>Rate</th></tr>';
    tup.daily_series.slice(-7).reverse().forEach(d => {
      h += '<tr><td>'+d.date+'</td><td>'+F(d.count)+'</td><td>'+FKS(d.amount)+'</td><td>'+F(d.mins)+'</td><td>'+d.rate+'</td></tr>';
    });
    h += '</table></div></div>';
  }

  document.getElementById('app').innerHTML = h;
}

function kpi(label, value, cls) {
  return '<div class="kpi-card"><div class="label">'+label+'</div><div class="value'+(cls?' '+cls:'')+'">'+value+'</div></div>';
}

load();
</script>
</body>
</html>"""

@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def web_dashboard():
    """Serve the BI web dashboard with embedded API key."""
    api_url = f"/api/analytics/dashboard?api_key={API_KEY}" if API_KEY else "/api/analytics/dashboard"
    html = DASHBOARD_HTML.replace("DASHBOARD_API_URL", f"'{api_url}'")
    return HTMLResponse(html)

@app.get("/api/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def web_dashboard_api():
    """Alias: Serve the BI web dashboard."""
    api_url = f"/api/analytics/dashboard?api_key={API_KEY}" if API_KEY else "/api/analytics/dashboard"
    html = DASHBOARD_HTML.replace("DASHBOARD_API_URL", f"'{api_url}'")
    return HTMLResponse(html)

'''

startup_marker = '# ═══════════════════════════════════════\n#  STARTUP'
content = content.replace(startup_marker, analytics_routes + startup_marker)

with open('/root/psvibe_api_server/app.py', 'w') as f:
    f.write(content)

print("OK: app.py updated successfully")
print(f"Lines: {len(content.splitlines())}")
