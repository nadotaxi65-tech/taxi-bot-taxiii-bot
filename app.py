"""
Combined entry point: runs the Telegram bot (polling, in a background thread)
and the Flask admin panel (main thread) in ONE process.

Why this file exists
---------------------
Before this file, render.yaml deployed the bot and the admin panel as TWO
separate Render services. Each free Render service has its OWN disk — so
even though bot.py and admin_app.py both use "taxi.db", they were really two
different files on two different machines. The admin panel could never see
rides/users created by the bot, and vice versa.

Running both inside one process guarantees they use the exact same sqlite
connection path (database.py's DB_PATH) on the exact same disk, so the admin
panel and the bot are always in sync.

Usage: python3 app.py   (this is what render.yaml's single service now runs)
"""
import os
import threading
import logging

import database as db
from admin_app import app as flask_app
import bot as botmod

logger = logging.getLogger(__name__)


def _run_bot():
    try:
        botmod.run_bot_blocking(in_thread=True)
    except Exception:
        logger.exception("Bot thread crashed")


def main():
    db.init_db()  # creates/migrates taxi.db once, before either half starts

    bot_thread = threading.Thread(target=_run_bot, name="telegram-bot", daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", "10000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    flask_app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=False)


if __name__ == "__main__":
    main()
