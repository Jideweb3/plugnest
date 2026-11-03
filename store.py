#!/usr/bin/env python3
"""Core store logic for Jide's Telegram reseller bot.

Everything here works without Telegram: Insight sync, pricing, user
accounts, wallet balances, orders and restock detection. bot.py is a
thin Telegram layer on top of this file, and simulate.py exercises it
from the command line.

Insight auth, two modes:
1. Muse runtime (preferred): the key lives in the Secure Vault
   connector custom.insightxpro-store and is reached through the
   dynamic-credential surrogate. Nothing secret is stored here.
2. Standalone: set INSIGHT_API_KEY in the environment.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
DB_PATH = os.path.join(HERE, "data", "store.db")
CACHE_PATH = os.path.join(HERE, "data", "products_cache.json")
INSIGHT_BASE = "https://api.insightxpro.store"
INSIGHT_HOST = "api.insightxpro.store"
CONNECTOR = "custom.insightxpro-store"

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ------------------------------------------------------- runtime settings
# Values the owner can change from the admin dashboard. They live in the
# DB settings table and win over config.json wherever both exist.

def get_setting(conn, key, default=None):
    if conn is None:
        return default
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return row[0]


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)))
    conn.commit()


def effective_config(conn, cfg=None):
    """config.json merged with dashboard-edited settings (dashboard wins
    for markup_percent, min_profit_usdt and support_handle)."""
    base = dict(cfg or load_config())
    if conn is not None:
        for key in ("markup_percent", "min_profit_usdt", "support_handle"):
            v = get_setting(conn, key)
            if v is not None:
                base[key] = v
    return base


# ---------------------------------------------------------------- Insight

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")


def _insight_request(path, method="GET", body=None, test_mode=False):
    url = INSIGHT_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    if test_mode:
        req.add_header("X-Test-Mode", "true")
    env_key = os.environ.get("INSIGHT_API_KEY", "").strip()
    if env_key:
        req.add_header("X-API-Key", env_key)
    else:
        from dynamic_credentials import add_surrogate_to_request
        add_surrogate_to_request(req, CONNECTOR, allowed_hosts=[INSIGHT_HOST])
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Insight API {e.code}: {detail}") from e


def insight_products_all():
    """Full board including sold-out rows (available=false, stock=0)."""
    return _insight_request("/api/v1/products?include_unavailable=1")["products"]


def insight_balance():
    return _insight_request("/api/v1/balance")


def insight_place_order(product_id, quantity=1, idempotency_key=None, test_mode=True):
    """Place an order. Test mode: not charged, no stock used, no real
    order created, but the response is shaped exactly like a real one."""
    body = {
        "product_id": int(product_id),
        "quantity": int(quantity),
        "idempotency_key": idempotency_key or f"jstore-{uuid.uuid4()}",
    }
    return _insight_request("/api/v1/orders", method="POST", body=body,
                            test_mode=test_mode)


# ---------------------------------------------------------------- pricing

def selling_price(cost, cfg=None):
    """Your price = cost + markup. Config: markup_percent and a floor
    min_profit_usdt so tiny products still earn something."""
    cfg = cfg or load_config()
    price = float(cost) * (1 + float(cfg["markup_percent"]) / 100.0)
    price = max(price, float(cost) + float(cfg["min_profit_usdt"]))
    return round(price + 1e-9, 2)


# ---------------------------------------------------------------- database

def db(check_same_thread=True):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT,
        balance_usdt REAL NOT NULL DEFAULT 0,
        referred_by INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY, name TEXT, emoji TEXT,
        description_text TEXT, how_to_use TEXT,
        cost_usdt REAL, stock INTEGER, available INTEGER,
        unlimited INTEGER, updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        product_name TEXT, cost_usdt REAL, price_usdt REAL, profit_usdt REAL,
        codes TEXT, test_mode INTEGER, status TEXT,
        insight_order_id TEXT, created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS restocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER, product_name TEXT, new_stock INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    );
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]
    if "price_override" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN price_override REAL")
        conn.commit()
    return conn


def get_or_create_user(conn, telegram_id, username="", first_name="", referred_by=None):
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?",
                       (telegram_id,)).fetchone()
    if row:
        return row
    conn.execute(
        "INSERT INTO users (telegram_id, username, first_name, balance_usdt,"
        " referred_by, created_at) VALUES (?,?,?,0,?,?)",
        (telegram_id, username or "", first_name or "", referred_by,
         int(time.time())))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE telegram_id=?",
                        (telegram_id,)).fetchone()


def credit(conn, telegram_id, amount):
    conn.execute("UPDATE users SET balance_usdt = balance_usdt + ?"
                 " WHERE telegram_id=?", (float(amount), telegram_id))
    conn.commit()


def debit(conn, telegram_id, amount):
    cur = conn.execute(
        "UPDATE users SET balance_usdt = balance_usdt - ?"
        " WHERE telegram_id=? AND balance_usdt >= ?",
        (float(amount), telegram_id, float(amount)))
    conn.commit()
    return cur.rowcount == 1


# ---------------------------------------------------------------- sync

def sync_products(conn, cfg=None):
    """Pull the full board from Insight, update the local catalog, and
    record refills (stock 0 -> stock >0) so the bot can ping them.
    Returns (total, buyable, restocked_rows)."""
    cfg = cfg or load_config()
    fresh = insight_products_all()
    restocked = []
    now = int(time.time())
    for p in fresh:
        old = conn.execute("SELECT stock, available FROM products WHERE id=?",
                           (p["id"],)).fetchone()
        conn.execute(
            "INSERT INTO products (id, name, emoji, description_text,"
            " how_to_use, cost_usdt, stock, available, unlimited, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " emoji=excluded.emoji, description_text=excluded.description_text,"
            " how_to_use=excluded.how_to_use, cost_usdt=excluded.cost_usdt,"
            " stock=excluded.stock, available=excluded.available,"
            " unlimited=excluded.unlimited, updated_at=excluded.updated_at",
            (p["id"], p["name"], p.get("emoji", ""),
             p.get("description_text", ""), p.get("how_to_use", ""),
             p.get("base_price_usdt", p.get("price_usdt", 0)),
             p.get("stock", 0), 1 if p.get("available") else 0,
             1 if p.get("unlimited") else 0, now))
        if old and (old["stock"] or 0) == 0 and (p.get("stock") or 0) > 0:
            restocked.append(p)
            conn.execute(
                "INSERT INTO restocks (product_id, product_name, new_stock,"
                " created_at) VALUES (?,?,?,?)",
                (p["id"], p["name"], p.get("stock", 0), now))
    # Products that vanished from the full list are treated as sold out.
    fresh_ids = {p["id"] for p in fresh}
    for row in conn.execute("SELECT id FROM products").fetchall():
        if row["id"] not in fresh_ids:
            conn.execute("UPDATE products SET available=0, stock=0 WHERE id=?",
                         (row["id"],))
    conn.commit()
    with open(CACHE_PATH, "w") as f:
        json.dump({"synced_at": now, "products": fresh}, f)
    buyable = sum(1 for p in fresh if p.get("available"))
    return len(fresh), buyable, restocked


def _priced(d, cfg):
    """Apply the selling price: a per-product override from the admin
    dashboard wins, otherwise cost + markup from the effective config."""
    d["price_usdt"] = selling_price(d["cost_usdt"], cfg)
    if d.get("price_override") is not None:
        d["price_usdt"] = round(float(d["price_override"]), 2)
    d["profit_usdt"] = round(d["price_usdt"] - d["cost_usdt"], 2)
    return d


def list_products(conn, cfg=None):
    cfg = effective_config(conn, cfg)
    rows = conn.execute(
        "SELECT * FROM products ORDER BY available DESC, name").fetchall()
    return [_priced(dict(r), cfg) for r in rows]


def get_product(conn, product_id, cfg=None):
    cfg = effective_config(conn, cfg)
    row = conn.execute("SELECT * FROM products WHERE id=?",
                       (product_id,)).fetchone()
    if not row:
        return None
    return _priced(dict(row), cfg)


# ---------------------------------------------------------------- buying

def buy(conn, telegram_id, product_id, cfg=None):
    """Charge the customer's internal balance the selling price, place
    the order on Insight at cost, deliver the codes. Live orders stay
    OFF until config live_orders is true; until then every order runs
    in Insight test mode (free, no stock used) and is flagged as such.
    Returns a dict describing the outcome."""
    cfg = cfg or load_config()
    user = conn.execute("SELECT * FROM users WHERE telegram_id=?",
                        (telegram_id,)).fetchone()
    if not user:
        return {"ok": False, "reason": "no_account"}
    product = get_product(conn, product_id, cfg)
    if not product:
        return {"ok": False, "reason": "no_product"}
    if not product["available"] or (product["stock"] or 0) <= 0 \
            and not product["unlimited"]:
        return {"ok": False, "reason": "sold_out", "product": product}
    price = product["price_usdt"]
    if user["balance_usdt"] < price:
        return {"ok": False, "reason": "low_balance", "product": product,
                "balance": user["balance_usdt"], "needed": price}
    test_mode = not cfg.get("live_orders", False)
    result = insight_place_order(product_id, test_mode=test_mode)
    order = result.get("order", result)
    codes = order.get("codes") or order.get("delivery") or []
    if isinstance(codes, str):
        codes = [codes]
    if not debit(conn, telegram_id, price):
        return {"ok": False, "reason": "low_balance", "product": product}
    conn.execute(
        "INSERT INTO orders (telegram_id, product_id, product_name,"
        " cost_usdt, price_usdt, profit_usdt, codes, test_mode, status,"
        " insight_order_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (telegram_id, product_id, product["name"], product["cost_usdt"],
         price, product["profit_usdt"], json.dumps(codes),
         1 if test_mode else 0, "delivered",
         str(order.get("id", "")), int(time.time())))
    conn.commit()
    return {"ok": True, "product": product, "codes": codes,
            "price": price, "profit": product["profit_usdt"],
            "test_mode": test_mode, "order": order}


def order_history(conn, telegram_id, limit=10):
    return conn.execute(
        "SELECT * FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
        (telegram_id, limit)).fetchall()


# ---------------------------------------------------------------- admin

def admin_overview(conn):
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "users": one("SELECT COUNT(*) FROM users"),
        "balances_held": round(one(
            "SELECT COALESCE(SUM(balance_usdt),0) FROM users"), 2),
        "orders": one("SELECT COUNT(*) FROM orders"),
        "revenue": round(one(
            "SELECT COALESCE(SUM(price_usdt),0) FROM orders"
            " WHERE status='delivered'"), 2),
        "profit": round(one(
            "SELECT COALESCE(SUM(profit_usdt),0) FROM orders"
            " WHERE status='delivered'"), 2),
        "products_total": one("SELECT COUNT(*) FROM products"),
        "products_available": one(
            "SELECT COUNT(*) FROM products WHERE available=1"),
        "restocks": one("SELECT COUNT(*) FROM restocks"),
    }


def admin_users(conn, limit=200):
    rows = conn.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM orders o"
        " WHERE o.telegram_id=u.telegram_id) AS orders_count"
        " FROM users u ORDER BY u.created_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def admin_orders(conn, limit=50):
    rows = conn.execute(
        "SELECT o.*, u.first_name AS user_name, u.username AS user_username"
        " FROM orders o LEFT JOIN users u ON u.telegram_id=o.telegram_id"
        " ORDER BY o.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def admin_adjust_balance(conn, telegram_id, delta):
    """Add (or remove, when delta is negative) balance from the admin
    dashboard. Never lets a balance go below zero. Returns the new
    balance, or None when the user does not exist."""
    row = conn.execute("SELECT balance_usdt FROM users WHERE telegram_id=?",
                       (telegram_id,)).fetchone()
    if not row:
        return None
    new = max(0.0, round(row["balance_usdt"] + float(delta), 2))
    conn.execute("UPDATE users SET balance_usdt=? WHERE telegram_id=?",
                 (new, telegram_id))
    conn.commit()
    return new


def admin_set_price(conn, product_id, price):
    """Set a per-product selling-price override (price=None clears it
    back to the normal markup price)."""
    cur = conn.execute("UPDATE products SET price_override=? WHERE id=?",
                       (price, product_id))
    conn.commit()
    return cur.rowcount == 1
