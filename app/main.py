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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    db.init_db()
    log.info(f"Datenbank: {DATABASE_PATH}")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
