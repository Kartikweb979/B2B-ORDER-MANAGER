import asyncio
import csv
import json
import logging
import os
import tempfile
from dotenv import load_dotenv
load_dotenv()
import sqlite3

import requests
from requests.exceptions import HTTPError, RequestException

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DB_PATH = os.environ.get("ORDERS_DB", "orders.db")
LOG_FILE = os.environ.get("BOT_LOG", "bot.log")
API_URL = os.environ.get("ORDER_API_URL", "http://127.0.0.1:8000/process-order")
API_TIMEOUT = float(os.environ.get("ORDER_API_TIMEOUT", "120"))
LOG_VERBOSE = os.environ.get("BOT_LOG_VERBOSE", "").lower() in ("1", "true", "yes")

logger = logging.getLogger("telegram_bot")


class RedactSecretsFilter(logging.Filter):
    """Strip API tokens and keys from log records."""

    _secrets: list[str] = []

    @classmethod
    def register(cls, *values: str | None) -> None:
        for value in values:
            if value and value not in cls._secrets:
                cls._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


class CompactFormatter(logging.Formatter):
    """Single-line exception summaries unless BOT_LOG_VERBOSE is enabled."""

    def formatException(self, ei) -> str:
        if LOG_VERBOSE:
            return super().formatException(ei)
        exc_type, exc_value, _ = ei
        return f"{exc_type.__name__}: {exc_value}"


def log_error(message: str, exc: BaseException | None = None) -> None:
    """Log a failure as one clean line (full traceback only when BOT_LOG_VERBOSE=1)."""
    if exc is None:
        logger.error(message)
    elif LOG_VERBOSE:
        logger.error("%s", message, exc_info=exc)
    else:
        logger.error("%s | %s: %s", message, type(exc).__name__, exc)


def setup_logging() -> None:
    """Configure file + console logging (idempotent)."""
    if logger.handlers:
        return

    RedactSecretsFilter.register(TOKEN)

    logger.setLevel(logging.DEBUG)

    formatter = CompactFormatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redact = RedactSecretsFilter()

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redact)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redact)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def _log_incoming(update: Update, kind: str) -> None:
    """Log every incoming Telegram message or command."""
    user = update.effective_user
    text = update.message.text if update.message else None
    logger.info(
        "Incoming %s | user=%s (@%s) | %r",
        kind,
        user.id if user else "unknown",
        user.username or "-",
        text,
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the Orders table if it doesn't already exist."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS Orders (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ParsedAt      TEXT    NOT NULL,
                    TelegramUser  TEXT,
                    TelegramUsername TEXT,
                    RawMessage    TEXT,
                    ClientName    TEXT,
                    ProductType   TEXT,
                    Quantity      TEXT,
                    PlyType       TEXT,
                    Material      TEXT,
                    DeliveryDate  TEXT
                )
            """)
            conn.commit()
    except sqlite3.Error as exc:
        raise SystemExit(f"Failed to initialise database: {exc}") from exc


def fetch_all_orders() -> list[dict]:
    """Return all saved orders from the Orders table, oldest first."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT ClientName, ProductType, Quantity, PlyType, Material, DeliveryDate
            FROM Orders
            ORDER BY id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


_EXPORT_COLUMNS = (
    "id", "ParsedAt", "TelegramUser", "TelegramUsername", "RawMessage",
    "ClientName", "ProductType", "Quantity", "PlyType", "Material", "DeliveryDate",
)


def fetch_all_orders_for_export() -> list[dict]:
    """Return every Orders column for CSV export, oldest first."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        columns = ", ".join(_EXPORT_COLUMNS)
        cursor = conn.execute(f"""
            SELECT {columns}
            FROM Orders
            ORDER BY id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


def write_orders_csv(orders: list[dict], path: str) -> None:
    """Write orders to a CSV file at `path` (UTF-8 with BOM for Excel)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(orders)


def _clean_field(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "not provided":
        return None
    return text


def _format_product_summary(order: dict) -> str:
    parts = []
    for key in ("Quantity", "PlyType", "ProductType"):
        val = _clean_field(order.get(key))
        if val:
            parts.append(val)
    return " ".join(parts) if parts else "Not specified"


def format_orders_report(orders: list[dict]) -> str:
    """Build a numbered, human-readable summary of all orders."""
    lines = [f"Order Report ({len(orders)} total)", ""]
    for i, order in enumerate(orders, start=1):
        client = _clean_field(order.get("ClientName")) or "Unknown"
        product = _format_product_summary(order)
        delivery = _clean_field(order.get("DeliveryDate")) or "Not specified"
        lines.append(f"{i}. Client: {client} | Product: {product} | Delivery: {delivery}")
    return "\n".join(lines)


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split long text into Telegram-safe message chunks."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len:
            if current:
                chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


# ---------------------------------------------------------------------------
# Order API client
# ---------------------------------------------------------------------------

def _process_order_via_api(text: str, update: Update) -> dict:
    """POST the raw message to the FastAPI order-processing service."""
    user = update.effective_user
    payload = {
        "text": text,
        "source_user": user.full_name if user else None,
        "source_username": user.username if user else None,
    }
    logger.info(
        "POST %s | user=%s (@%s)",
        API_URL,
        user.id if user else "unknown",
        user.username or "-",
    )
    response = requests.post(API_URL, json=payload, timeout=API_TIMEOUT)
    logger.info("API response | status=%s", response.status_code)
    response.raise_for_status()
    return response.json()


def _api_error_detail(exc: HTTPError) -> str:
    if exc.response is None:
        return "Order processing failed."
    try:
        body = exc.response.json()
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            return "; ".join(str(item) for item in detail)
    except (json.JSONDecodeError, ValueError):
        pass
    return "Order processing failed."


def _format_order_confirmation(data: dict) -> str:
    order = data.get("order", {})
    lines = [
        f"Order saved successfully (ID: {data.get('order_id')})",
        "",
        f"Client: {order.get('ClientName', 'Not Provided')}",
        f"Product: {order.get('ProductType', 'Not Provided')}",
        f"Quantity: {order.get('Quantity', 'Not Provided')}",
        f"Ply: {order.get('PlyType', 'Not Provided')}",
        f"Material: {order.get('Material', 'Not Provided')}",
        f"Delivery: {order.get('DeliveryDate', 'Not Provided')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_incoming(update, "command:/start")
    await update.message.reply_text(
        "Send a WhatsApp-style order message and I will parse it into JSON.\n\n"
        "Commands:\n"
        "hisaab — view all saved orders\n"
        "export — download all orders as a CSV file\n\n"
        "Example:\n"
        "Bhaiya 2000 pizza boxes bhej do brown kraft paper mein 3-ply, kal tak"
    )


async def export_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_incoming(update, "command:/export")
    try:
        orders = await asyncio.to_thread(fetch_all_orders_for_export)
    except sqlite3.Error:
        await update.message.reply_text(
            "Could not read orders from the database. Please try again later."
        )
        return

    if not orders:
        await update.message.reply_text("No orders found to export.")
        return

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        await asyncio.to_thread(write_orders_csv, orders, tmp_path)
        with open(tmp_path, "rb") as doc:
            await update.message.reply_document(
                document=doc,
                filename="orders_report.csv",
                caption=f"Exported {len(orders)} order(s).",
            )
    except OSError:
        await update.message.reply_text(
            "Could not generate the export file. Please try again later."
        )
    except Exception:
        await update.message.reply_text(
            "Could not send the export file. Please try again later."
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def hisaab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_incoming(update, "command:/hisaab")
    try:
        orders = await asyncio.to_thread(fetch_all_orders)
    except sqlite3.Error:
        await update.message.reply_text(
            "Could not read orders from the database. Please try again later."
        )
        return

    if not orders:
        await update.message.reply_text("No orders found.")
        return

    summary = format_orders_report(orders)
    for chunk in _split_message(summary):
        await update.message.reply_text(chunk)


async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_incoming(update, "order")
    text = update.message.text
    await update.message.reply_text("Processing your order...")

    try:
        result = await asyncio.to_thread(_process_order_via_api, text, update)
    except HTTPError as exc:
        await update.message.reply_text(_api_error_detail(exc))
        return
    except RequestException as exc:
        log_error("FastAPI server unreachable", exc)
        await update.message.reply_text(
            "Could not reach the order processing service. "
            "Make sure the API is running at http://127.0.0.1:8000."
        )
        return

    if not result.get("success"):
        await update.message.reply_text("Order processing failed. Please try again.")
        return

    await update.message.reply_text(_format_order_confirmation(result))


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning("Telegram network error (will retry) | %s", context.error)
        return
    log_error("Unhandled bot error", context.error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()

    if not TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set")
        raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable.")

    init_db()   # ensures DB exists for hisaab/export reads

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    app = Application.builder().token(TOKEN).request(request).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hisaab", hisaab))
    app.add_handler(CommandHandler("export", export_orders))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_order))
    logger.info("Bot started — logging to %s", LOG_FILE)
    try:
        app.run_polling(drop_pending_updates=True)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log_error("Bot stopped due to a fatal error", exc)
        raise


if __name__ == "__main__":
    main()
    