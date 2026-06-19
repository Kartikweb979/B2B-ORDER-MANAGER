import asyncio
import csv
import json
import os
import tempfile
from dotenv import load_dotenv
load_dotenv()
import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from order_parser import format_parse_error, parse_order

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ORDERS_LOG = os.environ.get("ORDERS_LOG", "orders.jsonl")
DB_PATH = os.environ.get("ORDERS_DB", "orders.db")


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
        print(f"Database ready: {DB_PATH}")
    except sqlite3.Error as e:
        raise SystemExit(f"Failed to initialise database: {e}") from e


def insert_order(order: dict, update: Update) -> None:
    """Persist a parsed order to the SQLite Orders table."""
    user = update.effective_user
    raw_message = update.message.text if update.message else ""

    record = {
        "parsed_at":          datetime.now(timezone.utc).isoformat(),
        "telegram_user":      user.full_name if user else "Unknown",
        "telegram_username":  user.username  if user else None,
        "raw_message":        raw_message,
        "client_name":        order.get("ClientName"),
        "product_type":       order.get("ProductType"),
        "quantity":           str(order.get("Quantity", "")) if order.get("Quantity") is not None else None,
        "ply_type":           order.get("PlyType"),
        "material":           order.get("Material"),
        "delivery_date":      order.get("DeliveryDate"),
    }

    sql = """
        INSERT INTO Orders
            (ParsedAt, TelegramUser, TelegramUsername, RawMessage,
             ClientName, ProductType, Quantity, PlyType, Material, DeliveryDate)
        VALUES
            (:parsed_at, :telegram_user, :telegram_username, :raw_message,
             :client_name, :product_type, :quantity, :ply_type, :material, :delivery_date)
    """

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(sql, record)
            conn.commit()
    except sqlite3.Error as e:
        # Log but don't crash the bot — order is still returned to the user
        print(f"[DB ERROR] Failed to insert order: {e}")


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
# Legacy JSONL helper (kept so existing logs aren't broken)
# ---------------------------------------------------------------------------

def _save_order_jsonl(order: dict, update: Update) -> None:
    user = update.effective_user
    record = {
        "parsed_at":          datetime.now(timezone.utc).isoformat(),
        "telegram_user":      user.full_name if user else "Unknown",
        "telegram_username":  user.username  if user else None,
        "raw_message":        update.message.text if update.message else "",
        **order,
    }
    with open(ORDERS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send a WhatsApp-style order message and I will parse it into JSON.\n\n"
        "Commands:\n"
        "hisaab — view all saved orders\n"
        "export — download all orders as a CSV file\n\n"
        "Example:\n"
        "Bhaiya 2000 pizza boxes bhej do brown kraft paper mein 3-ply, kal tak"
    )


async def export_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        orders = await asyncio.to_thread(fetch_all_orders_for_export)
    except sqlite3.Error as e:
        print(f"[DB ERROR] Failed to export orders: {e}")
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
    except OSError as e:
        print(f"[EXPORT ERROR] Failed to create CSV: {e}")
        await update.message.reply_text(
            "Could not generate the export file. Please try again later."
        )
    except Exception as e:
        print(f"[EXPORT ERROR] Failed to send document: {e}")
        await update.message.reply_text(
            "Could not send the export file. Please try again later."
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as e:
                print(f"[EXPORT ERROR] Failed to remove temp file: {e}")


async def hisaab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        orders = await asyncio.to_thread(fetch_all_orders)
    except sqlite3.Error as e:
        print(f"[DB ERROR] Failed to read orders: {e}")
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
    text = update.message.text
    await update.message.reply_text("Parsing order...")

    try:
        order = await asyncio.to_thread(parse_order, text)
    except Exception as exc:
        await update.message.reply_text(format_parse_error(exc))
        return

    # Persist to both SQLite and the legacy JSONL log
    await asyncio.to_thread(insert_order, order, update)
    await asyncio.to_thread(_save_order_jsonl, order, update)

    await update.message.reply_text(json.dumps(order, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (TimedOut, NetworkError)):
        print(f"[NETWORK] {context.error} — will retry automatically")
        return
    print(f"[ERROR] {context.error}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable.")

    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("Set GEMINI_API_KEY environment variable.")

    init_db()   # <-- creates Orders table on first run

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    app = Application.builder().token(TOKEN).request(request).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hisaab", hisaab))
    app.add_handler(CommandHandler("export", export_orders))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_order))
    print("Bot running. Forward order messages here — they will be parsed automatically.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
    