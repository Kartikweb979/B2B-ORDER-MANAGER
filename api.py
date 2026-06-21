"""
FastAPI service for order parsing (Gemini) and persistence (SQLite).

Run:
  uvicorn api:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from order_parser import format_parse_error, parse_order

load_dotenv()

DB_PATH = os.environ.get("ORDERS_DB", "orders.db")
LOG_FILE = os.environ.get("BOT_LOG", "bot.log")
LOG_VERBOSE = os.environ.get("BOT_LOG_VERBOSE", "").lower() in ("1", "true", "yes")

logger = logging.getLogger("order_api")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

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

    RedactSecretsFilter.register(os.environ.get("GEMINI_API_KEY"))

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


def _log_parse_error(exc: Exception) -> None:
    """Log Gemini / JSON failures with specific messages."""
    if isinstance(exc, json.JSONDecodeError):
        log_error("Invalid JSON while parsing order", exc)
    elif isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() or "timeout" in str(exc).lower():
        log_error("Gemini API timeout while parsing order", exc)
    else:
        log_error("Failed to parse order", exc)


# ---------------------------------------------------------------------------
# Database (extracted from telegram_bot.py)
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the Orders table if it doesn't already exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS Orders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ParsedAt         TEXT    NOT NULL,
                TelegramUser     TEXT,
                TelegramUsername TEXT,
                RawMessage       TEXT,
                ClientName       TEXT,
                ProductType      TEXT,
                Quantity         TEXT,
                PlyType          TEXT,
                Material         TEXT,
                DeliveryDate     TEXT
            )
        """)
        conn.commit()
    logger.info("Database ready: %s", DB_PATH)


def insert_order(
    order: dict,
    raw_message: str,
    *,
    source_user: str | None = None,
    source_username: str | None = None,
) -> int:
    """Persist a parsed order to SQLite. Returns the new row id."""
    record = {
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "telegram_user": source_user or "API",
        "telegram_username": source_username,
        "raw_message": raw_message,
        "client_name": order.get("ClientName"),
        "product_type": order.get("ProductType"),
        "quantity": str(order.get("Quantity", "")) if order.get("Quantity") is not None else None,
        "ply_type": order.get("PlyType"),
        "material": order.get("Material"),
        "delivery_date": order.get("DeliveryDate"),
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
            cursor = conn.execute(sql, record)
            conn.commit()
            order_id = cursor.lastrowid
    except sqlite3.OperationalError as exc:
        log_error("SQLite database lock while inserting order", exc)
        raise HTTPException(status_code=503, detail="Database is locked. Please retry.") from exc
    except sqlite3.Error as exc:
        log_error("Database error while inserting order", exc)
        raise HTTPException(status_code=500, detail="Failed to save order to database.") from exc

    logger.info(
        "Order inserted | id=%s | client=%s | product=%s | source=%s",
        order_id,
        record["client_name"],
        record["product_type"],
        record["telegram_user"],
    )
    return order_id


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class ProcessOrderRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Raw WhatsApp-style order message")
    source_user: str | None = Field(None, description="Optional caller label (e.g. Telegram display name)")
    source_username: str | None = Field(None, description="Optional caller username")


class ProcessOrderResponse(BaseModel):
    success: bool = True
    order_id: int
    order: dict[str, str]


def _log_incoming_request(body: ProcessOrderRequest) -> None:
    """Log every incoming /process-order request."""
    logger.info(
        "Incoming /process-order | source=%s (@%s) | text=%r",
        body.source_user or "API",
        body.source_username or "-",
        body.text.strip(),
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("Set GEMINI_API_KEY environment variable.")
    init_db()
    logger.info("API started — logging to %s", LOG_FILE)
    yield


app = FastAPI(
    title="Order Processing API",
    description="Parse B2B order messages with Gemini and store them in SQLite.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/process-order", response_model=ProcessOrderResponse)
def process_order(body: ProcessOrderRequest) -> ProcessOrderResponse:
    """Parse raw text via Gemini, save to the database, and return the result."""
    _log_incoming_request(body)
    text = body.text.strip()

    try:
        order = parse_order(text)
    except Exception as exc:
        _log_parse_error(exc)
        raise HTTPException(status_code=502, detail=format_parse_error(exc)) from exc

    order_id = insert_order(
        order,
        text,
        source_user=body.source_user,
        source_username=body.source_username,
    )

    logger.info("Order processed successfully | id=%s | client=%s", order_id, order.get("ClientName"))
    return ProcessOrderResponse(success=True, order_id=order_id, order=order)
