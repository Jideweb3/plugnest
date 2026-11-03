#!/usr/bin/env python3
"""Mini App server for Jide's reseller store.

Serves the Telegram Mini App (webapp/index.html) and a small JSON API
backed by store.py — the same catalog, balances and buy flow as the
chat bot, so both stay in sync.

Run:
    BOT_TOKEN=<token> python3 webapp_server.py        # port 8080
    DEV_MODE=1 python3 webapp_server.py               # local testing

Telegram user check: when BOT_TOKEN is set, every API call must carry
valid WebApp initData (HMAC check per Telegram docs). With DEV_MODE=1
a ?dev=<telegram_id> / {"dev": id} value is accepted instead, for
local rehearsal only. Never run DEV_MODE on a public server.

To go live: host this on a public HTTPS address and give that URL to
BotFather as the Mini App / menu button URL, and set WEBAPP_URL for
bot.py so the chat shows an Open Store button.
"""
import hashlib
import hmac
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = store.load_config()
CONN = store.db(check_same_thread=False)
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DEV_MODE = os.environ.get("DEV_MODE") == "1"
PORT = int(os.environ.get("PORT", "8080"))


def verify_init_data(init_data):
    """Return the Telegram user dict from initData, or None."""
    if not TOKEN or not init_data:
        return None
    pairs = dict(urllib.parse.parse_qsl(init_data))
    given = pairs.pop("hash", "")
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    expect = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, given):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None


def whoami(payload):
    user = verify_init_data((payload or {}).get("initData", ""))
    if user and user.get("id"):
        return user
    if DEV_MODE:
        dev = str((payload or {}).get("dev", "")).strip()
        if dev.isdigit():
            return {"id": int(dev), "first_name": "Dev User", "username": "dev"}
    return None


def public_product(p):
    return {"id": p["id"], "name": p["name"], "emoji": p["emoji"],
            "description_text": p["description_text"],
            "how_to_use": p["how_to_use"], "price_usdt": p["price_usdt"],
            "stock": p["stock"], "available": bool(p["available"]),
            "unlimited": bool(p["unlimited"])}


def is_admin(user_id):
    return user_id in (CFG.get("admin_ids") or [])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _user_or_401(self, payload):
        user = whoami(payload)
        if not user:
            self._json({"ok": False, "reason": "unauthorized"}, 401)
            return None
        store.get_or_create_user(CONN, user["id"], user.get("username", ""),
                                 user.get("first_name", ""))
        return user

    # ------------------------------------------------------------ admin
    # Every /api/admin/* call needs a verified Telegram user whose ID is
    # in config admin_ids. The Mini App only shows the Admin tab then.

    def _admin(self, path, payload):
        if path == "/api/admin/overview":
            data = store.admin_overview(CONN)
            try:
                bal = store.insight_balance()
                data["insight"] = {
                    "balance_usdt": bal.get("balance_usdt", bal.get("balance"))}
            except Exception:
                data["insight"] = None
            return self._json({"ok": True, **data})
        if path == "/api/admin/users":
            return self._json({"ok": True,
                               "users": store.admin_users(CONN)})
        if path == "/api/admin/orders":
            return self._json({"ok": True,
                               "orders": store.admin_orders(CONN)})
        if path == "/api/admin/products":
            out = [{"id": p["id"], "name": p["name"],
                    "cost_usdt": p["cost_usdt"],
                    "price_usdt": p["price_usdt"],
                    "price_override": p.get("price_override"),
                    "stock": p["stock"],
                    "available": bool(p["available"])}
                   for p in store.list_products(CONN, CFG)]
            return self._json({"ok": True, "products": out})
        if path == "/api/admin/credit":
            try:
                tid = int(payload.get("telegram_id", 0))
                amount = float(payload.get("amount", 0))
            except (TypeError, ValueError):
                return self._json({"ok": False, "reason": "bad_input"}, 400)
            if amount == 0 or abs(amount) > 100000:
                return self._json({"ok": False, "reason": "bad_amount"}, 400)
            new = store.admin_adjust_balance(CONN, tid, amount)
            if new is None:
                return self._json({"ok": False, "reason": "no_user"}, 404)
            return self._json({"ok": True, "balance_usdt": new})
        if path == "/api/admin/price":
            try:
                pid = int(payload.get("product_id", 0))
            except (TypeError, ValueError):
                return self._json({"ok": False, "reason": "bad_input"}, 400)
            raw = payload.get("price")
            if raw in (None, ""):
                price = None
            else:
                try:
                    price = round(float(raw), 2)
                except (TypeError, ValueError):
                    return self._json({"ok": False, "reason": "bad_price"}, 400)
                if price <= 0:
                    return self._json({"ok": False, "reason": "bad_price"}, 400)
            if not store.admin_set_price(CONN, pid, price):
                return self._json({"ok": False, "reason": "no_product"}, 404)
            return self._json({"ok": True})
        if path == "/api/admin/settings":
            cfg = store.effective_config(CONN, CFG)
            return self._json({
                "ok": True, "markup_percent": cfg["markup_percent"],
                "min_profit_usdt": cfg["min_profit_usdt"],
                "support_handle": cfg["support_handle"],
                "live_orders": bool(CFG.get("live_orders"))})
        if path == "/api/admin/settings_save":
            try:
                markup = float(payload.get("markup_percent"))
                min_profit = float(payload.get("min_profit_usdt"))
            except (TypeError, ValueError):
                return self._json({"ok": False, "reason": "bad_input"}, 400)
            handle = str(payload.get("support_handle", "")).strip()
            if not (0 <= markup <= 1000) or min_profit < 0 or not handle:
                return self._json({"ok": False, "reason": "bad_input"}, 400)
            store.set_setting(CONN, "markup_percent", markup)
            store.set_setting(CONN, "min_profit_usdt", min_profit)
            store.set_setting(CONN, "support_handle", handle)
            return self._json({"ok": True})
        return self._json({"ok": False, "reason": "not_found"}, 404)

    def do_GET(self):
        if self.path.startswith("/api/products"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if DEV_MODE and qs.get("dev"):
                pass  # catalog is public inside the app either way
            products = [public_product(p)
                        for p in store.list_products(CONN, CFG)]
            return self._json({"products": products})
        path = os.path.join(HERE, "webapp", "index.html")
        if not os.path.exists(path):
            path = os.path.join(HERE, "index.html")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = self._payload()
        if self.path.startswith("/api/admin/"):
            user = self._user_or_401(payload)
            if not user:
                return
            if not is_admin(user["id"]):
                return self._json({"ok": False, "reason": "not_admin"}, 403)
            return self._admin(self.path, payload)
        if self.path == "/api/me":
            user = self._user_or_401(payload)
            if not user:
                return
            row = CONN.execute("SELECT * FROM users WHERE telegram_id=?",
                               (user["id"],)).fetchone()
            orders = [dict(o) for o in
                      store.order_history(CONN, user["id"], limit=10)]
            return self._json({
                "telegram_id": user["id"],
                "first_name": row["first_name"],
                "balance_usdt": row["balance_usdt"],
                "is_admin": is_admin(user["id"]),
                "store_name": CFG["store_name"], "orders": orders})
        if self.path == "/api/buy":
            user = self._user_or_401(payload)
            if not user:
                return
            result = store.buy(CONN, user["id"],
                               int(payload.get("product_id", 0)), CFG)
            out = {"ok": result["ok"], "reason": result.get("reason", "")}
            if result["ok"]:
                out.update({"codes": result["codes"],
                            "how_to_use":
                                result["product"].get("how_to_use", ""),
                            "test_mode": result["test_mode"],
                            "price": result["price"]})
            else:
                out.update({"balance": result.get("balance"),
                            "needed": result.get("needed")})
            return self._json(out)
        self._json({"ok": False, "reason": "not_found"}, 404)


if __name__ == "__main__":
    total, buyable, _ = store.sync_products(CONN, CFG)
    print(f"Mini App on http://0.0.0.0:{PORT} — {total} products "
          f"({buyable} buyable), DEV_MODE={DEV_MODE}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
