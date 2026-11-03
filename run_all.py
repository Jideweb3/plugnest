#!/usr/bin/env python3
"""Combined runner for Render: chat bot + Mini App in ONE service.

Reads BOT_TOKEN and INSIGHT_API_KEY from environment variables
(set them in the Render dashboard - never commit them).
Runs bot polling in a background thread and the Mini App web
server in the main thread, so both share one database.
"""
import os
import threading

import store


def main():
    if os.environ.get("BOT_TOKEN"):
        import bot
        t = threading.Thread(target=bot.main, daemon=True,
                             name="plugnest-bot")
        t.start()
        print("[run_all] chat bot thread started")
    else:
        print("[run_all] BOT_TOKEN not set - web app only")

    _c = store.db()
    store.sync_products(_c)
    _c.close()

    import webapp_server
    port = int(os.environ.get("PORT", "8080"))
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("0.0.0.0", port), webapp_server.Handler)
    print(f"[run_all] Mini App live on port {port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
