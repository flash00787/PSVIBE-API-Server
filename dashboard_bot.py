#!/usr/bin/env python3
"""PS VIBE BI Dashboard — Telegram Bot

Responds to BI dashboard commands:
  /dashboard — Full dashboard summary
  /sales     — Today's sales breakdown
  /members   — Member activity stats
  /topups    — Top-up trends
  /consoles  — Console usage stats
  /analytics — Weekly/monthly trends
  /help      — Show available commands

Usage:
  Set env vars: TELEGRAM_BOT_TOKEN, API_BASE_URL (default http://localhost:8000)
  python dashboard_bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
)

# ── Config ──────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("DASHBOARD_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "")

MMT = timezone(timedelta(hours=6, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] dashboard_bot: %(message)s",
)
logger = logging.getLogger("dashboard_bot")


# ── API Helpers ─────────────────────────────────────────────────────

async def api_get(path: str, params: dict = None) -> dict:
    """Call the PS VIBE API and return data dict."""
    url = f"{API_BASE}{path}"
    if params is None:
        params = {}
    if API_KEY:
        params["api_key"] = API_KEY
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json().get("data", {})


# ── Formatting Helpers ──────────────────────────────────────────────

def fmt_ks(n) -> str:
    """Format Ks amounts nicely."""
    if n is None:
        return "0"
    return f"{int(n):,}"


def fmt_num(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def format_dashboard(data: dict) -> str:
    """Format full dashboard summary as Telegram message."""
    s = data.get("summary", {})
    lines = [
        "🎮 *PS VIBE — BI Dashboard*",
        f"🕐 _{data.get('generated_at', '')[:19]}_",
        "",
        "━━━ 📊 *TODAY* ━━━",
        f"💰 Sales: *{fmt_ks(s.get('today_sales_ks', 0))} Ks*",
        f"🧾 Vouchers: *{fmt_num(s.get('today_vouchers', 0))}*",
        f"🎯 Avg Ticket: *{fmt_ks(s.get('today_avg_ticket_ks', 0))} Ks*",
        f"👤 Active Members: *{fmt_num(s.get('active_members_today', 0))}*",
        f"🎮 Consoles: *{s.get('active_consoles', 0)}/{s.get('total_consoles', 0)} active*",
        "",
        "━━━ 📈 *THIS WEEK* ━━━",
        f"💳 Top-ups: *{fmt_ks(s.get('week_topup_ks', 0))} Ks* ({fmt_num(s.get('week_topup_count', 0))} transactions)",
        "",
        f"🏢 Total Members: *{fmt_num(s.get('total_members', 0))}*",
        f"⏱️ Base Rate: *{fmt_ks(s.get('base_rate_ks_hr', 0))} Ks/hr*",
        "",
        "_Use buttons below for details ↓_",
    ]
    return "\n".join(lines)


def format_sales(data: dict) -> str:
    """Format sales data."""
    lines = [
        f"📊 *Sales — {data.get('date', 'Today')}*",
        "",
        f"💰 Total: *{fmt_ks(data.get('total_sales_ks', 0))} Ks*",
        f"🧾 Vouchers: *{fmt_num(data.get('voucher_count', 0))}*",
        f"🎯 Avg Ticket: *{fmt_ks(data.get('average_ticket_ks', 0))} Ks*",
        "",
    ]
    by_payment = data.get("by_payment", {})
    if by_payment:
        lines.append("*By Payment Method:*")
        for method, info in sorted(by_payment.items()):
            lines.append(f"  {method}: {info['count']} × {fmt_ks(info['amount'])} Ks")
        lines.append("")

    top = data.get("top_sales", [])[:5]
    if top:
        lines.append("*Top 5 Sales:*")
        for i, s in enumerate(top, 1):
            lines.append(f"  {i}. {s.get('voucher', '?')} — {fmt_ks(s.get('amount', 0))} Ks ({s.get('member', '?')})")

    return "\n".join(lines)


def format_members(data: dict) -> str:
    """Format member activity."""
    lines = [
        "👥 *Member Activity*",
        "",
        f"👤 Total Members: *{fmt_num(data.get('total_members', 0))}*",
        f"🟢 Active Today: *{fmt_num(data.get('active_today', 0))}*",
        f"📅 Active (7d): *{fmt_num(data.get('active_last_7d', 0))}*",
        f"⏱️ Wallet Mins: *{fmt_num(data.get('total_wallet_mins', 0))}*",
        f"💵 Total Spend: *{fmt_ks(data.get('total_spend_ks', 0))} Ks*",
        f"📊 Avg/Member: *{fmt_ks(data.get('avg_spend_per_member', 0))} Ks*",
        "",
    ]
    tiers = data.get("tier_distribution", [])
    if tiers:
        lines.append("*Tier Breakdown:*")
        for t in tiers:
            bar = "█" * max(1, int(t.get("pct", 0) / 5))
            lines.append(f"  {t['tier']}: {t['count']} ({t['pct']}%) {bar}")
    return "\n".join(lines)


def format_topups(data: dict) -> str:
    """Format top-up trends."""
    lines = [
        f"💰 *Top-Up Trends ({data.get('period_days', 30)} days)*",
        "",
        f"📦 Total Top-ups: *{fmt_num(data.get('total_topups', 0))}*",
        f"💵 Total Amount: *{fmt_ks(data.get('total_amount_ks', 0))} Ks*",
        f"⏱️ Total Mins: *{fmt_num(data.get('total_mins', 0))}*",
        f"📊 Eff. Rate: *{data.get('all_time_effective_rate', 0)} Ks/min*",
        "",
    ]
    weekly = data.get("weekly_aggregates", [])
    if weekly:
        lines.append("*Weekly:*")
        for w in weekly[-4:]:
            lines.append(f"  {w['week']}: {fmt_ks(w['amount'])} Ks / {fmt_num(w['mins'])} mins ({w['rate']} Ks/min)")
        lines.append("")

    top = data.get("top_members", [])[:5]
    if top:
        lines.append("*Top Top-Uppers:*")
        for i, m in enumerate(top, 1):
            lines.append(f"  {i}. {m['member_id']}: {fmt_ks(m['amount'])} Ks ({m['count']}×)")
    return "\n".join(lines)


def format_consoles(data: dict) -> str:
    """Format console usage."""
    lines = [
        f"🎮 *Console Usage ({data.get('period_days', 30)} days)*",
        "",
        f"🖥️ Total Consoles: *{fmt_num(data.get('total_consoles', 0))}*",
        f"📋 Total Bookings: *{fmt_num(data.get('total_bookings', 0))}*",
        f"🟢 Active Now: *{fmt_num(data.get('active_now', 0))}*",
        f"📊 Util. Rate: *{data.get('avg_bookings_per_console_day', 0)}/day*",
        "",
    ]
    consoles = data.get("consoles", [])
    if consoles:
        lines.append("*Per Console:*")
        for c in consoles:
            status = "🟢" if c.get("active_bookings", 0) > 0 else "⚫"
            lines.append(
                f"  {status} *{c['console_id']}* ({c.get('type', '?')}): "
                f"{c['total_bookings']} bookings, {c['total_hours']}h, "
                f"{c['unique_members']} members"
            )
    return "\n".join(lines)


def format_analytics(data: dict) -> str:
    """Format weekly/monthly trends."""
    lines = [
        f"📈 *Trends ({data.get('period_weeks', 4)} weeks)*",
        "",
        f"📊 All-Time Eff. Rate: *{data.get('all_time_rate', 0)} Ks/min*",
        "",
    ]

    console_sum = data.get("console_summary", {})
    lines.append("*Console Activity:*")
    lines.append(f"  Bookings: {fmt_num(console_sum.get('total_bookings', 0))}")
    lines.append(f"  Active Now: {fmt_num(console_sum.get('active_now', 0))}")
    lines.append(f"  Util. Rate: {console_sum.get('utilization_rate', 0)}/day")
    lines.append("")

    weekly = data.get("topup_weekly", [])
    if weekly:
        lines.append("*Weekly Top-Up Trend:*")
        for w in weekly[-4:]:
            lines.append(f"  {w['week']}: {fmt_ks(w['amount'])} Ks | Rate: {w['rate']}")
    return "\n".join(lines)


# ── Bot Command Handlers ─────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show available commands."""
    text = (
        "🎮 *PS VIBE BI Dashboard Bot*\n\n"
        "*Commands:*\n"
        "/dashboard — Full BI summary\n"
        "/sales — Today's sales breakdown\n"
        "/members — Member activity stats\n"
        "/topups — Top-up trends (30d)\n"
        "/consoles — Console usage stats\n"
        "/analytics — Weekly/monthly trends\n"
        "/help — This help message\n\n"
        "_Data refreshes automatically from Google Sheets._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show full BI dashboard."""
    await update.message.reply_chat_action("typing")
    try:
        data = await api_get("/api/analytics/dashboard")
        text = format_dashboard(data)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Sales", callback_data="sales"),
                InlineKeyboardButton("👥 Members", callback_data="members"),
            ],
            [
                InlineKeyboardButton("💰 Top-ups", callback_data="topups"),
                InlineKeyboardButton("🎮 Consoles", callback_data="consoles"),
            ],
            [InlineKeyboardButton("📈 Trends", callback_data="analytics")],
        ])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error("Dashboard error: %s", e)
        await update.message.reply_text(f"⚠️ Error loading dashboard: {e}")


async def cmd_sales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show today's sales."""
    await update.message.reply_chat_action("typing")
    try:
        data = await api_get("/api/analytics/daily_sales")
        await update.message.reply_text(format_sales(data), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def cmd_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show member activity."""
    await update.message.reply_chat_action("typing")
    try:
        data = await api_get("/api/analytics/member_activity")
        await update.message.reply_text(format_members(data), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def cmd_topups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show top-up trends."""
    await update.message.reply_chat_action("typing")
    try:
        data = await api_get("/api/analytics/topups", {"days": 30})
        await update.message.reply_text(format_topups(data), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def cmd_consoles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show console usage."""
    await update.message.reply_chat_action("typing")
    try:
        data = await api_get("/api/analytics/console_usage", {"days": 30})
        await update.message.reply_text(format_consoles(data), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def cmd_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show weekly/monthly trends."""
    await update.message.reply_chat_action("typing")
    try:
        data = await api_get("/api/analytics/weekly_trends", {"weeks": 4})
        await update.message.reply_text(format_analytics(data), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


# ── Callback Query Handler ───────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses."""
    query = update.callback_query
    await query.answer()
    cmd = query.data

    try:
        if cmd == "sales":
            data = await api_get("/api/analytics/daily_sales")
            await query.edit_message_text(format_sales(data), parse_mode="Markdown")
        elif cmd == "members":
            data = await api_get("/api/analytics/member_activity")
            await query.edit_message_text(format_members(data), parse_mode="Markdown")
        elif cmd == "topups":
            data = await api_get("/api/analytics/topups", {"days": 30})
            await query.edit_message_text(format_topups(data), parse_mode="Markdown")
        elif cmd == "consoles":
            data = await api_get("/api/analytics/console_usage", {"days": 30})
            await query.edit_message_text(format_consoles(data), parse_mode="Markdown")
        elif cmd == "analytics":
            data = await api_get("/api/analytics/weekly_trends", {"weeks": 4})
            await query.edit_message_text(format_analytics(data), parse_mode="Markdown")
    except Exception as e:
        logger.error("Callback error: %s", e)
        await query.edit_message_text(f"⚠️ Error: {e}")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        logger.error("DASHBOARD_BOT_TOKEN or TELEGRAM_BOT_TOKEN must be set")
        sys.exit(1)

    logger.info("Starting PS VIBE Dashboard Bot")
    logger.info("API Base: %s", API_BASE)

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_dashboard))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CommandHandler("members", cmd_members))
    app.add_handler(CommandHandler("topups", cmd_topups))
    app.add_handler(CommandHandler("consoles", cmd_consoles))
    app.add_handler(CommandHandler("analytics", cmd_analytics))

    # Callback handler
    app.add_handler(CallbackQueryHandler(on_callback))

    logger.info("Bot polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
