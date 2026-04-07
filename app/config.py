import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

_raw_ids = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: list[int] = [int(x.strip()) for x in _raw_ids.split(",") if x.strip()]

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "/data/receipts.db")
UPLOAD_PATH: str = os.getenv("UPLOAD_PATH", "/data/uploads")
