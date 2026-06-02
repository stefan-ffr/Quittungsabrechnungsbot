import asyncio
import logging
from app.config import DATABASE_PATH
import app.db as db
from app.bot import build_application, set_commands

log = logging.getLogger(__name__)


async def run_bot():
    application = build_application()
    await application.initialize()
    await set_commands(application)
    await application.start()
    log.info("Telegram bot gestartet")
    await application.updater.start_polling(
        allowed_updates=["message", "callback_query"]
    )
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main():
    import os
    level_name = os.getenv("LOG_LEVEL", "DEBUG").upper()
    level = getattr(logging, level_name, logging.DEBUG)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    # Telegram-Polling-Spam abmildern
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    db.init_db()
    log.info(f"Bot startet — log-level={level_name}, DB={DATABASE_PATH}")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
