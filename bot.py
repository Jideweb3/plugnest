#!/usr/bin/env python3
"""Telegram layer for Jide's reseller store bot.

Uses only the Telegram Bot API over HTTPS (requests) with long
polling, so there are no extra packages to install. All store logic
lives in store.py.

Run:
    BOT_TOKEN=<token from BotFather> python3 bot.py

Payment/deposit is a stub for now (Jide: "sort out payment method
later"). Admins can credit test balances with /credit so the whole
buy flow can be rehearsed. Orders run in Insight TEST MODE until
config.json live_orders is set to true.
"""
import json
import os
import sys
import threading
import time

import requests

import store

CFG = store.load_config()
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
PAGE = 8
BOT_USERNAME = ""


def tg(method, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=70)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def send(chat_id, text, buttons=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **params)


def edit(chat_id, message_id, text, buttons=None):
    params = {"chat_id": chat_id, "message_id": message_id,
              "text": text, "parse_mode": "HTML"}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        return tg("editMessageText", **params)
    except Exception:
        return send(chat_id, text, buttons)


def btn(text, data):
    return {"text": text, "callback_data": data}


def main_buttons():
    rows = []
    webapp_url = os.environ.get("WEBAPP_URL", "").strip()
    if webapp_url:
        rows.append([{"text": "🛍 Open Store App",
                      "web_app": {"url": webapp_url}}])
    rows += [
        [btn("🏬 Shop", "shop:0")],
        [btn("💳 Deposit", "deposit"), btn("👤 My Profile", "profile")],
        [btn("🤝 Support", "support")],
        [btn("💲 Refer & Earn", "refer")],
    ]
    return rows


def welcome_text(user_name):
    name = user_name or "there"
    return (f"Welcome to {CFG['store_name']} 🔥\n\n"
            f"Hey {name} 👋\n\n"
            "We offer premium digital products at the best prices. "
            "Fast, secure, and fully automated delivery.\n\n"
            "🏬 <b>Shop</b> — Browse &amp; buy products\n"
            "💳 <b>Deposit</b> — Add funds to your wallet\n"
            "👤 <b>My Profile</b> — Balance, orders &amp; settings\n"
            "🤝 <b>Support</b> — Get help\n"
            "💲 <b>Refer &amp; Earn</b> — Invite friends &amp; earn rewards")


def shop_page(conn, page):
    products = store.list_products(conn, CFG)
    pages = max(1, (len(products) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for p in products[page * PAGE:(page + 1) * PAGE]:
        stock = "∞" if p["unlimited"] else str(p["stock"])
        label = (f"{p['emoji'] or '🛍'} {p['name']} — ${p['price_usdt']:.2f}"
                 f" (Stock: {stock})")
        if not p["available"]:
            label = f"❌ {p['name']} — Sold Out"
        rows.append([btn(label, f"prod:{p['id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"shop:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"shop:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("⬅️ Back", "home")])
    return f"🏬 <b>Shop</b> — page {page + 1}/{pages}\nTap a product to view it.", rows


def product_view(conn, product_id, page):
    p = store.get_product(conn, product_id, CFG)
    if not p:
        return "That product is gone.", [[btn("⬅️ Back to Shop", f"shop:{page}")]]
    stock = "∞" if p["unlimited"] else str(p["stock"])
    status = f"Stock: {stock}" if p["available"] else "❌ Sold Out"
    text = (f"{p['emoji'] or '🛍'} <b>{p['name']}</b>\n\n"
            f"{p['description_text'] or ''}\n\n"
            f"💲 Price: <b>${p['price_usdt']:.2f}</b>\n📦 {status}")
    rows = []
    if p["available"]:
        rows.append([btn(f"🛒 Buy — ${p['price_usdt']:.2f}", f"buy:{p['id']}")])
    rows.append([btn("⬅️ Back to Shop", f"shop:{page}")])
    return text, rows


def profile_text(conn, user):
    orders = store.order_history(conn, user["telegram_id"], limit=5)
    lines = [f"👤 <b>My Profile</b>\n",
             f"Name: {user['first_name'] or ''}",
             f"ID: <code>{user['telegram_id']}</code>",
             f"💲 Balance: <b>${user['balance_usdt']:.2f}</b>\n"]
    if orders:
        lines.append("<b>Recent orders:</b>")
        for o in orders:
            tag = " (test)" if o["test_mode"] else ""
            lines.append(f"• {o['product_name']} — ${o['price_usdt']:.2f}{tag}")
    else:
        lines.append("No orders yet. Open the Shop to make your first buy.")
    return "\n".join(lines)


def handle_start(conn, chat_id, user, ref_arg=""):
    referred_by = None
    if ref_arg.startswith("ref_"):
        try:
            referred_by = int(ref_arg[4:])
            if referred_by == user["id"]:
                referred_by = None
        except ValueError:
            referred_by = None
    store.get_or_create_user(conn, user["id"], user.get("username", ""),
                             user.get("first_name", ""), referred_by)
    send(chat_id, welcome_text(user.get("first_name", "")), main_buttons())


def handle_callback(conn, cq):
    chat_id = cq["message"]["chat"]["id"]
    msg_id = cq["message"]["message_id"]
    user = cq["from"]
    data = cq.get("data", "")
    store.get_or_create_user(conn, user["id"], user.get("username", ""),
                             user.get("first_name", ""))
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    me = conn.execute("SELECT * FROM users WHERE telegram_id=?",
                      (user["id"],)).fetchone()

    if data == "home":
        edit(chat_id, msg_id, welcome_text(user.get("first_name", "")),
             main_buttons())
    elif data.startswith("shop:"):
        text, rows = shop_page(conn, int(data.split(":")[1]))
        edit(chat_id, msg_id, text, rows)
    elif data.startswith("prod:"):
        _, pid, page = data.split(":")
        text, rows = product_view(conn, int(pid), int(page))
        edit(chat_id, msg_id, text, rows)
    elif data.startswith("buy:"):
        pid = int(data.split(":")[1])
        result = store.buy(conn, user["id"], pid, CFG)
        if result["ok"]:
            codes = "\n".join(f"<code>{c}</code>" for c in result["codes"]) \
                or "(delivery in your orders)"
            note = "\n\n🧪 Test order — no real money moved." \
                if result["test_mode"] else ""
            how = result["product"].get("how_to_use") or ""
            send(chat_id,
                 f"✅ <b>Delivered: {result['product']['name']}</b>\n\n"
                 f"{codes}\n\n{how}{note}")
        elif result["reason"] == "low_balance":
            send(chat_id,
                 f"Your balance is ${result['balance']:.2f} but this costs "
                 f"${result['needed']:.2f}.\nTop up with Deposit first.",
                 [[btn("💳 Deposit", "deposit")]])
        elif result["reason"] == "sold_out":
            send(chat_id, "That one just sold out. It will be back when "
                          "stock refills.")
        else:
            send(chat_id, "Something went wrong with that order. "
                          "Your balance was not charged.")
    elif data == "deposit":
        edit(chat_id, msg_id,
             "💳 <b>Deposit</b>\n\n"
             "Payment method is being set up — crypto deposit "
             "(fresh address per deposit, auto-confirm) is coming soon.\n\n"
             f"Your balance: <b>${me['balance_usdt']:.2f}</b>\n"
             f"Minimum deposit once live: ${CFG['min_deposit_usdt']}",
             [[btn("⬅️ Back", "home")]])
    elif data == "profile":
        edit(chat_id, msg_id, profile_text(conn, me),
             [[btn("💳 Deposit", "deposit"), btn("🏬 Shop", "shop:0")],
              [btn("⬅️ Back", "home")]])
    elif data == "support":
        handle = store.get_setting(conn, "support_handle") \
            or CFG['support_handle']
        edit(chat_id, msg_id,
             f"🤝 <b>Support</b>\n\nMessage us any time: "
             f"{handle}\nWe reply as fast as we can.",
             [[btn("⬅️ Back", "home")]])
    elif data == "refer":
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{user['id']}" \
            if BOT_USERNAME else "link appears once the bot is live"
        edit(chat_id, msg_id,
             "💲 <b>Refer &amp; Earn</b>\n\nInvite friends with your "
             f"link:\n{link}\n\nFriends who join through it are counted "
             "under you.",
             [[btn("⬅️ Back", "home")]])


def handle_message(conn, msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        handle_start(conn, chat_id, user, parts[1] if len(parts) > 1 else "")
        return
    store.get_or_create_user(conn, user["id"], user.get("username", ""),
                             user.get("first_name", ""))
    is_admin = user["id"] in CFG.get("admin_ids", [])
    if text == "/whoami":
        send(chat_id, f"Your Telegram ID: <code>{user['id']}</code>")
    elif text == "/menu":
        send(chat_id, welcome_text(user.get("first_name", "")), main_buttons())
    elif text == "/sync" and is_admin:
        total, buyable, restocked = store.sync_products(conn, CFG)
        send(chat_id, f"Synced: {total} products, {buyable} buyable, "
                      f"{len(restocked)} restocked.")
    elif text == "/stats" and is_admin:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        orders = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(profit_usdt),0) p FROM orders"
        ).fetchone()
        try:
            bal = store.insight_balance().get("balance_usdt")
        except Exception:
            bal = "?"
        send(chat_id, f"Users: {users}\nOrders: {orders['c']}\n"
                      f"Profit booked: ${orders['p']:.2f}\n"
                      f"Insight fund: ${bal}")
    elif text.startswith("/credit") and is_admin:
        try:
            _, uid, amount = text.split()
            store.credit(conn, int(uid), float(amount))
            send(chat_id, f"Credited ${float(amount):.2f} to {uid}.")
        except Exception:
            send(chat_id, "Use: /credit &lt;telegram_id&gt; &lt;amount&gt;")
    else:
        send(chat_id, "Tap a button below, or send /menu.",
             main_buttons())


def sync_loop(conn):
    while True:
        try:
            _, _, restocked = store.sync_products(conn, CFG)
            for p in restocked:
                for admin in CFG.get("admin_ids", []):
                    send(admin, f"🔔 Restocked: {p['name']} — "
                                f"{p.get('stock')} back in stock.")
        except Exception as e:
            print(f"[sync] {e}", file=sys.stderr)
        time.sleep(max(60, int(CFG.get("sync_minutes", 5)) * 60))


def main():
    if not TOKEN:
        print("BOT_TOKEN is not set. Get a token from @BotFather, then run:\n"
              "  BOT_TOKEN=<token> python3 bot.py")
        sys.exit(1)
    global BOT_USERNAME
    me = tg("getMe")
    BOT_USERNAME = me.get("username", "")
    try:
        tg("setMyDescription",
           description="Premium digital products at the best prices. "
                       "Fast, secure, and fully automated delivery.")
        tg("setMyShortDescription",
           short_description="Premium digital products. Fast, secure, "
                             "fully automated delivery.")
    except Exception as e:
        print(f"[branding] {e}", file=sys.stderr)
    tg("setMyCommands", commands=[
        {"command": "start", "description": "Open the store"},
        {"command": "menu", "description": "Main menu"},
        {"command": "whoami", "description": "Show my Telegram ID"},
    ])
    conn = store.db()
    total, buyable, _ = store.sync_products(conn, CFG)
    print(f"@{BOT_USERNAME} live. Synced {total} products ({buyable} buyable).")
    threading.Thread(target=sync_loop,
                     args=(store.db(check_same_thread=False),),
                     daemon=True).start()
    offset = 0
    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=50,
                         allowed_updates=["message", "callback_query"])
        except Exception as e:
            print(f"[poll] {e}", file=sys.stderr)
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    handle_callback(conn, upd["callback_query"])
                elif "message" in upd:
                    handle_message(conn, upd["message"])
            except Exception as e:
                print(f"[update] {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
