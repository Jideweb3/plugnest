# Jide Store — Telegram Reseller Bot + Mini App

A Telegram store that mirrors Insight Store's full board through the
Insight Reseller API, sells at your markup, and delivers instantly.
Chat bot and Mini App share one backend (store.py), one database and
one wallet per customer.

## What is built and tested

- **Shop** — full board synced from Insight with `include_unavailable`,
  so sold-out products show as Sold Out and flip live when refilled.
  Sync runs every `sync_minutes` and records restocks; admins get a
  Telegram ping when a product refills.
- **Buy flow** — customer balance is charged the selling price, the
  order is placed on Insight at cost, codes + how-to-use are delivered
  in chat and saved under My Profile / Mini App orders. Profit per
  order (selling − cost) is recorded.
- **Accounts** — created automatically from the Telegram ID on /start.
  No sign-up form. Refer & Earn passes `ref_<id>` on the start link.
- **Admin** — /stats (users, orders, profit, Insight fund), /sync,
  /credit <telegram_id> <amount> for rehearsing deposits.
- **Mini App** — dark storefront in webapp/index.html with search,
  product pages, profile/orders and buy, served by webapp_server.py.
  User identity is verified from Telegram WebApp initData.
- **Safety** — orders run in Insight TEST MODE (free, no stock used)
  until `live_orders` is set to true in config.json. Verified end to
  end: simulate.py and the Mini App API both completed test buys.

## What is NOT built yet

- **Customer deposit / payment** — Deposit is a stub saying it is
  being set up (decision parked by owner: fresh-address-per-deposit
  crypto, BEP20-style auto-confirm, to be added). Until then, admins
  credit balances manually with /credit.
- **Hosting** — bot and Mini App run anywhere with Python 3. The Mini
  App needs a public HTTPS URL before Telegram will open it.

## Files

- `store.py` — Insight client, sync + restock detection, pricing,
  users/wallet/orders in SQLite (data/store.db). No Telegram code.
- `bot.py` — chat bot (Telegram Bot API long polling, stdlib +
  requests only).
- `webapp_server.py` + `webapp/index.html` — Mini App server + page.
- `simulate.py` — full rehearsal without Telegram or real money.
- `config.json` — store name, markup, support handle, admin IDs,
  live_orders switch.

## Config (config.json)

- `markup_percent` — default 30 (placeholder until owner picks).
  Selling price = cost × (1 + markup%), floored at cost +
  `min_profit_usdt`.
- `admin_ids` — Telegram IDs with admin powers. Send /whoami to the
  bot to learn yours, then add it here.
- `support_handle` — shown under Support.
- `live_orders` — keep false until the Insight fund is topped up and
  prices are final.

## Insight key

Never stored in this project. On Muse's machine it comes from the
Secure Vault connector `custom.insightxpro-store`. Standalone: set
the `INSIGHT_API_KEY` environment variable.

## Run

```bash
# rehearsal (no Telegram, no money)
python3 simulate.py

# chat bot
BOT_TOKEN=<BotFather token> python3 bot.py

# mini app (local rehearsal)
DEV_MODE=1 PORT=8080 python3 webapp_server.py

# mini app (live, behind HTTPS)
BOT_TOKEN=<BotFather token> WEBAPP_URL=https://your-host/ python3 bot.py
BOT_TOKEN=<BotFather token> python3 webapp_server.py
```

## BotFather checklist

1. @BotFather → /newbot → copy the token (keep it secret).
2. Run bot.py with BOT_TOKEN.
3. Open the bot, /start, then /whoami → put your ID in admin_ids.
4. For the Mini App: host webapp_server.py on a public HTTPS
   address, set that URL as the bot's menu button in @BotFather
   (Bot Settings → Menu Button) and as WEBAPP_URL for bot.py.
