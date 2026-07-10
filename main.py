"""
==============================================================================
  HAMYONIM — Shaxsiy Moliya va Bank Kartalarini Boshqarish Telegram Boti
==============================================================================

Tavsif:
    "Hamyonim" — foydalanuvchilarga o'z moliyaviy oqimlarini (kirim/chiqim),
    balansini va bank kartalarini (Uzcard / Humo) xavfsiz va qulay tarzda
    boshqarish imkonini beruvchi Telegram bot.

Texnik stek:
    - Python 3.11+
    - aiogram 3.x (to'liq asinxron arxitektura, Router asosida)
    - SQLite3 (WAL rejimi, indekslangan so'rovlar)
    - FSM (StatesGroup) — foydalanuvchi jarayonlarini boshqarish uchun
    - Logging — fayl va konsolga parallel yozuv

Muallif: Hamyonim Dev Team
Litsenziya: Ichki foydalanish uchun
==============================================================================
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ==============================================================================
# 1. KONFIGURATSIYA
# ==============================================================================

# Yangi yangilangan Telegram Bot Tokeningiz:
BOT_TOKEN: str = "8888847127:AAEbgW0Kk97WPRyqdZaslynlxrNrbE1vNa0"

# Admin Telegram ID (Boshqaruv paneli uchun)
ADMIN_IDS: set[int] = {123456789} 

DB_PATH: str = "hamyonim.db"
LOG_PATH: str = "hamyonim.log"

TRANSACTIONS_PER_PAGE: int = 8
USERS_PER_BROADCAST_BATCH: int = 25
BROADCAST_DELAY_SECONDS: float = 0.05

DEFAULT_CATEGORIES_INCOME: list[str] = [
    "💼 Ish haqi",
    "🎁 Sovg'a",
    "📈 Investitsiya",
    "🛒 Savdo",
    "➕ Boshqa kirim",
]

DEFAULT_CATEGORIES_EXPENSE: list[str] = [
    "🍽 Oziq-ovqat",
    "🚕 Transport",
    "🏠 Uy-job",
    "👕 Kiyim-kechak",
    "💊 Salomatlik",
    "🎓 Ta'lim",
    "🎮 Ko'ngilochar",
    "➖ Boshqa chiqim",
]


# ==============================================================================
# 2. LOGGING SOZLAMALARI
# ==============================================================================

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("hamyonim")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger

logger = setup_logging()


# ==============================================================================
# 3. YORDAMCHI (UTILITY) FUNKSIYALAR
# ==============================================================================

class CardType(str, Enum):
    UZCARD = "Uzcard"
    HUMO = "Humo"
    UNKNOWN = "Noma'lum"


def clean_card_number(raw: str) -> str:
    return re.sub(r"[^\d]", "", raw)


def detect_card_type(card_number: str) -> CardType:
    digits = clean_card_number(card_number)
    if digits.startswith("8600"):
        return CardType.UZCARD
    if digits.startswith("9860"):
        return CardType.HUMO
    return CardType.UNKNOWN


def is_valid_card_number(card_number: str) -> bool:
    digits = clean_card_number(card_number)
    return len(digits) == 16 and digits.isdigit()


def mask_card_number(card_number: str) -> str:
    digits = clean_card_number(card_number)
    if len(digits) != 16:
        return "**** **** **** ****"
    return f"{digits[:4]} **** **** {digits[12:]}"


def format_money(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    integer_part = int(amount)
    decimal_part = round((amount - integer_part) * 100)
    grouped = f"{integer_part:,}".replace(",", " ")
    return f"{sign}{grouped}.{decimal_part:02d} so'm"


def format_datetime(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return dt_str


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ==============================================================================
# 4. MA'LUMOTLAR BAZASI QATLAMI (SQLite3, WAL rejimi)
# ==============================================================================

class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with closing(self._get_connection()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT,
                    full_name   TEXT,
                    balance     REAL NOT NULL DEFAULT 0,
                    is_blocked  INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    type        TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                    amount      REAL NOT NULL,
                    category    TEXT,
                    note        TEXT,
                    created_at  TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cards (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    card_number  TEXT NOT NULL,
                    card_type    TEXT NOT NULL,
                    card_label   TEXT,
                    created_at   TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions (user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions (created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_user_id ON cards (user_id);")
        logger.info("Ma'lumotlar bazasi muvaffaqiyatli ishga tushirildi: %s", self.db_path)

    def add_user(self, user_id: int, username: Optional[str], full_name: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, balance, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    updated_at = excluded.updated_at;
                """,
                (user_id, username, full_name, now, now),
            )

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
            return cur.fetchone()

    def user_exists(self, user_id: int) -> bool:
        return self.get_user(user_id) is not None

    def get_balance(self, user_id: int) -> float:
        row = self.get_user(user_id)
        return row["balance"] if row else 0.0

    def update_balance(self, user_id: int, delta: float) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            conn.execute(
                """
                UPDATE users
                SET balance = balance + ?, updated_at = ?
                WHERE user_id = ?;
                """,
                (delta, now, user_id),
            )

    def set_balance(self, user_id: int, amount: float) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE user_id = ?;",
                (amount, now, user_id),
            )

    def clear_user_history(self, user_id: int) -> None:
        with closing(self._get_connection()) as conn, conn:
            conn.execute("DELETE FROM transactions WHERE user_id = ?;", (user_id,))
            conn.execute("UPDATE users SET balance = 0 WHERE user_id = ?;", (user_id,))

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        with closing(self._get_connection()) as conn, conn:
            conn.execute(
                "UPDATE users SET is_blocked = ? WHERE user_id = ?;",
                (1 if blocked else 0, user_id),
            )

    def get_all_user_ids(self, only_active: bool = True) -> list[int]:
        query = "SELECT user_id FROM users"
        if only_active:
            query += " WHERE is_blocked = 0"
        with closing(self._get_connection()) as conn:
            cur = conn.execute(query + ";")
            return [row["user_id"] for row in cur.fetchall()]

    def count_users(self) -> int:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM users;")
            return cur.fetchone()["cnt"]

    def count_active_users_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        with closing(self._get_connection()) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE updated_at LIKE ?;",
                (f"{today}%",),
            )
            return cur.fetchone()["cnt"]

    def add_transaction(
        self, user_id: int, tx_type: str, amount: float, category: Optional[str], note: Optional[str]
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO transactions (user_id, type, amount, category, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (user_id, tx_type, amount, category, note, now),
            )
            tx_id = cur.lastrowid

        delta = amount if tx_type == "income" else -amount
        self.update_balance(user_id, delta)
        return tx_id

    def get_transactions(self, user_id: int, limit: int = TRANSACTIONS_PER_PAGE, offset: int = 0) -> list[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            cur = conn.execute(
                """
                SELECT * FROM transactions
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?;
                """,
                (user_id, limit, offset),
            )
            return cur.fetchall()

    def count_transactions(self, user_id: int) -> int:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id = ?;", (user_id,))
            return cur.fetchone()["cnt"]

    def delete_transaction(self, tx_id: int, user_id: int) -> bool:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT * FROM transactions WHERE id = ? AND user_id = ?;", (tx_id, user_id))
            row = cur.fetchone()
            if row is None:
                return False
            with conn:
                conn.execute("DELETE FROM transactions WHERE id = ?;", (tx_id,))
            delta = -row["amount"] if row["type"] == "income" else row["amount"]
            self.update_balance(user_id, delta)
            return True

    def get_stats_summary(self, user_id: int, days: int = 30) -> dict[str, float]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn:
            cur = conn.execute(
                """
                SELECT type, COALESCE(SUM(amount), 0) AS total
                FROM transactions
                WHERE user_id = ? AND created_at >= ?
                GROUP BY type;
                """,
                (user_id, since),
            )
            result = {"income": 0.0, "expense": 0.0}
            for row in cur.fetchall():
                result[row["type"]] = row["total"]
            return result

    def get_category_breakdown(self, user_id: int, tx_type: str, days: int = 30) -> list[sqlite3.Row]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn:
            cur = conn.execute(
                """
                SELECT category, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt
                FROM transactions
                WHERE user_id = ? AND type = ? AND created_at >= ?
                GROUP BY category
                ORDER BY total DESC;
                """,
                (user_id, tx_type, since),
            )
            return cur.fetchall()

    def add_card(self, user_id: int, card_number: str, card_label: Optional[str]) -> int:
        card_type = detect_card_type(card_number).value
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO cards (user_id, card_number, card_type, card_label, created_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (user_id, clean_card_number(card_number), card_type, card_label, now),
            )
            return cur.lastrowid

    def get_cards(self, user_id: int) -> list[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT * FROM cards WHERE user_id = ? ORDER BY created_at DESC;", (user_id,))
            return cur.fetchall()

    def get_card(self, card_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT * FROM cards WHERE id = ? AND user_id = ?;", (card_id, user_id))
            return cur.fetchone()

    def count_cards(self, user_id: int) -> int:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM cards WHERE user_id = ?;", (user_id,))
            return cur.fetchone()["cnt"]

    def delete_card(self, card_id: int, user_id: int) -> bool:
        with closing(self._get_connection()) as conn, conn:
            cur = conn.execute("DELETE FROM cards WHERE id = ? AND user_id = ?;", (card_id, user_id))
            return cur.rowcount > 0

    def get_global_stats(self) -> dict[str, Any]:
        with closing(self._get_connection()) as conn:
            total_users = conn.execute("SELECT COUNT(*) AS cnt FROM users;").fetchone()["cnt"]
            total_tx = conn.execute("SELECT COUNT(*) AS cnt FROM transactions;").fetchone()["cnt"]
            total_cards = conn.execute("SELECT COUNT(*) AS cnt FROM cards;").fetchone()["cnt"]
            total_income = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE type='income';"
            ).fetchone()["s"]
            total_expense = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE type='expense';"
            ).fetchone()["s"]
            blocked = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_blocked = 1;").fetchone()["cnt"]
        return {
            "total_users": total_users,
            "blocked_users": blocked,
            "total_transactions": total_tx,
            "total_cards": total_cards,
            "total_income": total_income,
            "total_expense": total_expense,
        }

db = Database(DB_PATH)


# ==============================================================================
# 5. FSM HOLATLARI (STATES)
# ==============================================================================

class AddIncomeStates(StatesGroup):
    choosing_category = State()
    entering_amount = State()
    entering_note = State()


class AddExpenseStates(StatesGroup):
    choosing_category = State()
    entering_amount = State()
    entering_note = State()


class AddCardStates(StatesGroup):
    entering_number = State()
    entering_label = State()


class BroadcastStates(StatesGroup):
    entering_message = State()
    confirming = State()


class EditBalanceStates(StatesGroup):
    entering_amount = State()


# ==============================================================================
# 6. KLAVIATURALAR
# ==============================================================================

MAIN_MENU_BUTTONS = [
    "💰 Balans",
    "➕ Kirim qo'shish",
    "➖ Chiqim qo'shish",
    "📊 Statistika",
    "🧾 Tranzaksiyalar tarixi",
    "💳 Kartalarim",
    "⚙️ Sozlamalar",
]


def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for label in MAIN_MENU_BUTTONS:
        builder.add(KeyboardButton(text=label))
    builder.adjust(2, 2, 2, 1)

    if user_id in ADMIN_IDS:
        builder.row(KeyboardButton(text="🛠 Admin Panel"))

    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Menyudan tanlang...")


def cancel_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🚫 Bekor qilish"))
    return builder.as_markup(resize_keyboard=True)


def categories_inline_keyboard(categories: list[str], prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for idx, cat in enumerate(categories):
        builder.add(InlineKeyboardButton(text=cat, callback_data=f"{prefix}:{idx}"))
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="🚫 Bekor qilish", callback_data=f"{prefix}:cancel"))
    return builder.as_markup()


def skip_note_keyboard(prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data=f"{prefix}:skip_note"))
    builder.add(InlineKeyboardButton(text="🚫 Bekor qilish", callback_data=f"{prefix}:cancel"))
    builder.adjust(2)
    return builder.as_markup()


def transactions_pagination_keyboard(current_page: int, total_pages: int, tx_ids: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for tx_id in tx_ids:
        builder.add(InlineKeyboardButton(text=f"🗑 #{tx_id} o'chirish", callback_data=f"tx_del:{tx_id}:{current_page}"))
    builder.adjust(1)

    nav_row: list[InlineKeyboardButton] = []
    if current_page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"tx_page:{current_page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{current_page + 1}/{max(total_pages, 1)}", callback_data="tx_noop"))
    if current_page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"tx_page:{current_page + 1}"))
    builder.row(*nav_row)
    return builder.as_markup()


def cards_list_keyboard(cards: Iterable[sqlite3.Row]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for card in cards:
        label = card["card_label"] or card["card_type"]
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 {label} — {mask_card_number(card['card_number'])}",
                callback_data=f"card_del:{card['id']}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Yangi karta qo'shish", callback_data="card_add"))
    return builder.as_markup()


def confirm_card_delete_keyboard(card_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"card_del_yes:{card_id}"))
    builder.add(InlineKeyboardButton(text="❌ Yo'q", callback_data="card_del_no"))
    builder.adjust(2)
    return builder.as_markup()


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Balansni qo'lda tuzatish", callback_data="settings_edit_balance"))
    builder.row(InlineKeyboardButton(text="🗑 Barcha tarixni tozalash", callback_data="settings_clear_history"))
    return builder.as_markup()


def confirm_clear_history_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Ha, tozalash", callback_data="clear_history_yes"))
    builder.add(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="clear_history_no"))
    builder.adjust(2)
    return builder.as_markup()


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Umumiy Statistika", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="📢 Xabar tarqatish (Broadcast)", callback_data="admin_broadcast"))
    return builder.as_markup()


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Yuborish", callback_data="broadcast_confirm"))
    builder.add(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="broadcast_cancel"))
    builder.adjust(2)
    return builder.as_markup()


def stats_period_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="7 kun", callback_data="stats_period:7"))
    builder.add(InlineKeyboardButton(text="30 kun", callback_data="stats_period:30"))
    builder.add(InlineKeyboardButton(text="90 kun", callback_data="stats_period:90"))
    builder.adjust(3)
    return builder.as_markup()


# ==============================================================================
# 7. ROUTERLAR
# ==============================================================================

user_router = Router(name="user_router")
admin_router = Router(name="admin_router")

# ------------------------------------------------------------------------
# 7.1. UMUMIY / START
# ------------------------------------------------------------------------

@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    db.add_user(user.id, user.username, user.full_name)
    logger.info("Foydalanuvchi botni ishga tushirdi: %s (%s)", user.id, user.full_name)

    text = (
        f"👋 Assalomu alaykum, <b>{escape_html(user.full_name)}</b>!\n\n"
        "💼 <b>Hamyonim</b> — shaxsiy moliyangizni nazorat qilish uchun ishonchli yordamchingiz.\n\n"
        "Bu yerda siz:\n"
        "• 💰 Balansingizni kuzatishingiz\n"
        "• ➕ Kirim va ➖ chiqimlaringizni qayd etishingiz\n"
        "• 💳 Bank kartalaringizni xavfsiz saqlashingiz\n"
        "• 📊 Batafsil statistikani ko'rishingiz mumkin\n\n"
        "Quyidagi menyudan kerakli bo'limni tanlang 👇"
    )
    await message.answer(text, reply_markup=main_menu_keyboard(user.id))


@user_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>📖 Yordam bo'limi</b>\n\n"
        "/start — Botni qayta ishga tushirish\n"
        "/help — Ushbu yordam matnini ko'rsatish\n"
        "/balans — Joriy balansni tezkor ko'rish\n"
        "/cancel — Joriy amalni bekor qilish\n\n"
        "Asosiy menyu orqali kirim/chiqim qo'shishingiz, tranzaksiyalar tarixini "
        "ko'rishingiz va kartalaringizni boshqarishingiz mumkin."
    )
    await message.answer(text)


@user_router.message(Command("cancel"))
@user_router.message(F.text == "🚫 Bekor qilish")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("❕ Hozircha bekor qilinadigan amal yo'q.")
        return
    await state.clear()
    await message.answer("✅ Amal bekor qilindi.", reply_markup=main_menu_keyboard(message.from_user.id))


@user_router.message(Command("balans"))
@user_router.message(F.text == "💰 Balans")
async def show_balance(message: Message) -> None:
    user_id = message.from_user.id
    if not db.user_exists(user_id):
        db.add_user(user_id, message.from_user.username, message.from_user.full_name)

    balance = db.get_balance(user_id)
    cards_count = db.count_cards(user_id)
    tx_count = db.count_transactions(user_id)

    emoji = "🟢" if balance >= 0 else "🔴"
    text = (
        f"{emoji} <b>Joriy balansingiz:</b>\n"
        f"<b>{format_money(balance)}</b>\n\n"
        f"💳 Saqlangan kartalar: {cards_count} ta\n"
        f"🧾 Jami tranzaksiyalar: {tx_count} ta"
    )
    await message.answer(text)


# ------------------------------------------------------------------------
# 7.2. KIRIM QO'SHISH
# ------------------------------------------------------------------------

@user_router.message(F.text == "➕ Kirim qo'shish")
async def start_add_income(message: Message, state: FSMContext) -> None:
    await state.set_state(AddIncomeStates.choosing_category)
    await message.answer(
        "📥 <b>Kirim qo'shish</b>\n\nIltimos, kategoriyani tanlang:",
        reply_markup=categories_inline_keyboard(DEFAULT_CATEGORIES_INCOME, "inc_cat"),
    )


@user_router.callback_query(StateFilter(AddIncomeStates.choosing_category), F.data.startswith("inc_cat:"))
async def process_income_category(callback: CallbackQuery, state: FSMContext) -> None:
    _, value = callback.data.split(":", maxsplit=1)
    if value == "cancel":
        await state.clear()
        await callback.message.edit_text("🚫 Kirim qo'shish bekor qilindi.")
        await callback.answer()
        return

    category = DEFAULT_CATEGORIES_INCOME[int(value)]
    await state.update_data(category=category)
    await state.set_state(AddIncomeStates.entering_amount)
    await callback.message.edit_text(
        f"✅ Kategoriya: <b>{category}</b>\n\n💵 Endi summana kiriting (so'mda), masalan: <code>150000</code>"
    )
    await callback.answer()


@user_router.message(StateFilter(AddIncomeStates.entering_amount))
async def process_income_amount(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Noto'g'ri format. Iltimos, musbat sonli summa kiriting, masalan: <code>150000</code>")
        return

    await state.update_data(amount=amount)
    await state.set_state(AddIncomeStates.entering_note)
    await message.answer(
        "📝 Izoh qo'shmoqchimisiz? (ixtiyoriy)\nMatn kiriting yoki o'tkazib yuboring:",
        reply_markup=skip_note_keyboard("inc_note"),
    )


@user_router.callback_query(StateFilter(AddIncomeStates.entering_note), F.data.startswith("inc_note:"))
async def process_income_note_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, action = callback.data.split(":", maxsplit=1)
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("🚫 Kirim qo'shish bekor qilindi.")
        await callback.answer()
        return

    data = await state.get_data()
    await _finalize_income(callback.message, callback.from_user.id, data, note=None)
    await state.clear()
    await callback.answer()


@user_router.message(StateFilter(AddIncomeStates.entering_note))
async def process_income_note_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _finalize_income(message, message.from_user.id, data, note=message.text.strip())
    await state.clear()


async def _finalize_income(message: Message, user_id: int, data: dict, note: Optional[str]) -> None:
    amount = data["amount"]
    category = data["category"]
    db.add_transaction(user_id, "income", amount, category, note)

    text = (
        "✅ <b>Kirim muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📂 Kategoriya: {category}\n"
        f"💵 Summa: {format_money(amount)}\n"
    )
    if note:
        text += f"📝 Izoh: {escape_html(note)}\n"
    text += f"\n💰 Yangi balans: <b>{format_money(db.get_balance(user_id))}</b>"

    await message.answer(text, reply_markup=main_menu_keyboard(user_id))


# ------------------------------------------------------------------------
# 7.3. CHIQIM QO'SHISH
# ------------------------------------------------------------------------

@user_router.message(F.text == "➖ Chiqim qo'shish")
async def start_add_expense(message: Message, state: FSMContext) -> None:
    await state.set_state(AddExpenseStates.choosing_category)
    await message.answer(
        "📤 <b>Chiqim qo'shish</b>\n\nIltimos, kategoriyani tanlang:",
        reply_markup=categories_inline_keyboard(DEFAULT_CATEGORIES_EXPENSE, "exp_cat"),
    )


@user_router.callback_query(StateFilter(AddExpenseStates.choosing_category), F.data.startswith("exp_cat:"))
async def process_expense_category(callback: CallbackQuery, state: FSMContext) -> None:
    _, value = callback.data.split(":", maxsplit=1)
    if value == "cancel":
        await state.clear()
        await callback.message.edit_text("🚫 Chiqim qo'shish bekor qilindi.")
        await callback.answer()
        return

    category = DEFAULT_CATEGORIES_EXPENSE[int(value)]
    await state.update_data(category=category)
    await state.set_state(AddExpenseStates.entering_amount)
    await callback.message.edit_text(
        f"✅ Kategoriya: <b>{category}</b>\n\n💵 Endi chiqim summasini kiriting (so'mda):"
    )
    await callback.answer()


@user_router.message(StateFilter(AddExpenseStates.entering_amount))
async def process_expense_amount(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Iltimos, faqat musbat son kiriting:")
        return

    await state.update_data(amount=amount)
    await state.set_state(AddExpenseStates.entering_note)
    await message.answer(
        "📝 Izoh qo'shasizmi? (ixtiyoriy):",
        reply_markup=skip_note_keyboard("exp_note"),
    )


@user_router.callback_query(StateFilter(AddExpenseStates.entering_note), F.data.startswith("exp_note:"))
async def process_expense_note_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, action = callback.data.split(":", maxsplit=1)
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("🚫 Chiqim bekor qilindi.")
        await callback.answer()
        return

    data = await state.get_data()
    await _finalize_expense(callback.message, callback.from_user.id, data, note=None)
    await state.clear()
    await callback.answer()


@user_router.message(StateFilter(AddExpenseStates.entering_note))
async def process_expense_note_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _finalize_expense(message, message.from_user.id, data, note=message.text.strip())
    await state.clear()


async def _finalize_expense(message: Message, user_id: int, data: dict, note: Optional[str]) -> None:
    amount = data["amount"]
    category = data["category"]
    db.add_transaction(user_id, "expense", amount, category, note)

    text = (
        "✅ <b>Chiqim muvaffaqiyatli saqlandi!</b>\n\n"
        f"📂 Kategoriya: {category}\n"
        f"💵 Summa: {format_money(amount)}\n"
    )
    if note:
        text += f"📝 Izoh: {escape_html(note)}\n"
    text += f"\n💰 Yangi balans: <b>{format_money(db.get_balance(user_id))}</b>"

    await message.answer(text, reply_markup=main_menu_keyboard(user_id))


# ------------------------------------------------------------------------
# 7.4. STATISTIKA BO'LIMI
# ------------------------------------------------------------------------

@user_router.message(F.text == "📊 Statistika")
async def cmd_stats(message: Message) -> None:
    await message.answer("🗓 Hisobot davrini tanlang:", reply_markup=stats_period_keyboard())


@user_router.callback_query(F.data.startswith("stats_period:"))
async def process_stats_period(callback: CallbackQuery) -> None:
    _, days_str = callback.data.split(":")
    days = int(days_str)
    user_id = callback.from_user.id

    summary = db.get_stats_summary(user_id, days)
    inc_breakdown = db.get_category_breakdown(user_id, "income", days)
    exp_breakdown = db.get_category_breakdown(user_id, "expense", days)

    text = f"📊 <b>Oxirgi {days} kunlik hisobot:</b>\n\n"
    text += f"📥 Jami kirim: <code>{format_money(summary['income'])}</code>\n"
    text += f"📤 Jami chiqim: <code>{format_money(summary['expense'])}</code>\n"
    text += "───────────────────\n\n"

    if inc_breakdown:
        text += "💼 <b>Kirimlar kategoriyalar bo'yicha:</b>\n"
        for row in inc_breakdown:
            text += f"• {row['category']}: {format_money(row['total'])} ({row['cnt']} marta)\n"
        text += "\n"

    if exp_breakdown:
        text += "🍽 <b>Chiqimlar kategoriyalar bo'yicha:</b>\n"
        for row in exp_breakdown:
            text += f"• {row['category']}: {format_money(row['total'])} ({row['cnt']} marta)\n"
    
    if not inc_breakdown and not exp_breakdown:
        text += "📭 Ushbu davrda hech qanday tranzaksiya topilmadi."

    await callback.message.edit_text(text, reply_markup=stats_period_keyboard())
    await callback.answer()


# ------------------------------------------------------------------------
# 7.5. TRANZAKSIYALAR TARIXI
# ------------------------------------------------------------------------

@user_router.message(F.text == "🧾 Tranzaksiyalar tarixi")
async def cmd_tx_history(message: Message) -> None:
    await _show_transactions_page(message, message.from_user.id, page=0)


@user_router.callback_query(F.data.startswith("tx_page:"))
async def process_tx_pagination(callback: CallbackQuery) -> None:
    _, page_str = callback.data.split(":")
    await _show_transactions_page(callback.message, callback.from_user.id, page=int(page_str), edit=True)
    await callback.answer()


@user_router.callback_query(F.data.startswith("tx_del:"))
async def process_tx_delete(callback: CallbackQuery) -> None:
    _, tx_id_str, page_str = callback.data.split(":")
    tx_id = int(tx_id_str)
    page = int(page_str)
    user_id = callback.from_user.id

    if db.delete_transaction(tx_id, user_id):
        await callback.answer("🗑 Tranzaksiya o'chirildi va balans qayta hisoblandi!")
        await _show_transactions_page(callback.message, user_id, page=page, edit=True)
    else:
        await callback.answer("⚠️ Tranzaksiya topilmadi!", show_alert=True)


async def _show_transactions_page(message: Message, user_id: int, page: int, edit: bool = False) -> None:
    total_tx = db.count_transactions(user_id)
    total_pages = (total_tx + TRANSACTIONS_PER_PAGE - 1) // TRANSACTIONS_PER_PAGE
    offset = page * TRANSACTIONS_PER_PAGE
    tx_list = db.get_transactions(user_id, limit=TRANSACTIONS_PER_PAGE, offset=offset)

    if not tx_list:
        text = "📭 Tranzaksiyalar tarixi bo'sh."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    text = f"🧾 <b>Tranzaksiyalar tarixi (Sahifa {page + 1}/{max(total_pages, 1)}):</b>\n\n"
    tx_ids = []
    for tx in tx_list:
        tx_ids.append(tx["id"])
        sign = "➕" if tx["type"] == "income" else "➖"
        note_str = f" ({tx['note']})" if tx["note"] else ""
        text += f"<b>#{tx['id']}</b> | {sign} <code>{format_money(tx['amount'])}</code> | {tx['category']}{note_str}\n⏱ <i>{format_datetime(tx['created_at'])}</i>\n\n"

    markup = transactions_pagination_keyboard(page, total_pages, tx_ids)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


# ------------------------------------------------------------------------
# 7.6. KARTALAR MENEDJMENTI
# ------------------------------------------------------------------------

@user_router.message(F.text == "💳 Kartalarim")
async def cmd_cards(message: Message) -> None:
    cards = db.get_cards(message.from_user.id)
    text = "💳 <b>Sizning bank kartalaringiz:</b>\n\nO'chirish uchun kerakli kartani bosing yoki yangi karta qo'shing:"
    await message.answer(text, reply_markup=cards_list_keyboard(cards))


@user_router.callback_query(F.data == "card_add")
async def callback_add_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCardStates.entering_number)
    await callback.message.answer("🔢 Bank karta raqamini kiriting (16 xonali son):", reply_markup=cancel_keyboard())
    await callback.answer()


@user_router.message(StateFilter(AddCardStates.entering_number))
async def process_card_number(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not is_valid_card_number(raw):
        await message.answer("⚠️ Noto'g'ri karta raqami. Faqat 16 ta raqam kiriting:")
        return

    await state.update_data(card_number=raw)
    await state.set_state(AddCardStates.entering_label)
    await message.answer("✍️ Karta uchun nom bering (Masalan: <i>Asosiy, Milliy Karta</i>):", reply_markup=cancel_keyboard())


@user_router.message(StateFilter(AddCardStates.entering_label))
async def process_card_label(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    card_number = data["card_number"]
    label = message.text.strip()
    user_id = message.from_user.id

    db.add_card(user_id, card_number, label)
    await state.clear()
    await message.answer("✅ Karta muvaffaqiyatli saqlandi!", reply_markup=main_menu_keyboard(user_id))


@user_router.callback_query(F.data.startswith("card_del:"))
async def callback_delete_card_confirm(callback: CallbackQuery) -> None:
    _, card_id = callback.data.split(":")
    await callback.message.edit_text("❓ Haqiqatan ham ushbu kartani o'chirib tashlamoqchimisiz?", 
                                      reply_markup=confirm_card_delete_keyboard(int(card_id)))


@user_router.callback_query(F.data.startswith("card_del_yes:"))
async def callback_delete_card_yes(callback: CallbackQuery) -> None:
    _, card_id = callback.data.split(":")
    if db.delete_card(int(card_id), callback.from_user.id):
        await callback.answer("🗑 Karta muvaffaqiyatli o'chirildi!")
    else:
        await callback.answer("⚠️ Karta topilmadi!")
    cards = db.get_cards(callback.from_user.id)
    await callback.message.edit_text("💳 Kartalar ro'yxati yangilandi:", reply_markup=cards_list_keyboard(cards))


@user_router.callback_query(F.data == "card_del_no")
async def callback_delete_card_no(callback: CallbackQuery) -> None:
    cards = db.get_cards(callback.from_user.id)
    await callback.message.edit_text("💳 Kartalar ro'yxati:", reply_markup=cards_list_keyboard(cards))


# ------------------------------------------------------------------------
# 7.7. SOZLAMALAR BO'LIMI
# ------------------------------------------------------------------------

@user_router.message(F.text == "⚙️ Sozlamalar")
async def cmd_settings(message: Message) -> None:
    await message.answer("⚙️ <b>Sozlamalar paneli:</b>\n\nKerakli amalni tanlang:", reply_markup=settings_menu_keyboard())


@user_router.callback_query(F.data == "settings_edit_balance")
async def callback_settings_edit_balance(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditBalanceStates.entering_amount)
    await callback.message.answer("📝 Yangi joriy balans summasini kiriting (so'mda):", reply_markup=cancel_keyboard())
    await callback.answer()


@user_router.message(StateFilter(EditBalanceStates.entering_amount))
async def process_settings_new_balance(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "")
    try:
        amount = float(raw)
    except ValueError:
        await message.answer("⚠️ Faqat son kiriting:")
        return

    user_id = message.from_user.id
    db.set_balance(user_id, amount)
    await state.clear()
    await message.answer(f"✅ Balans muvaffaqiyatli yangilandi!\n💰 Yangi balans: <b>{format_money(amount)}</b>", reply_markup=main_menu_keyboard(user_id))


@user_router.callback_query(F.data == "settings_clear_history")
async def callback_clear_history_ask(callback: CallbackQuery) -> None:
    await callback.message.edit_text("⚠️ <b>DIQQAT!</b> Barcha tranzaksiyalar tarixi o'chib ketadi va balans 0 ga tushadi. Rozimisiz?", reply_markup=confirm_clear_history_keyboard())


@user_router.callback_query(F.data == "clear_history_yes")
async def callback_clear_history_yes(callback: CallbackQuery) -> None:
    db.clear_user_history(callback.from_user.id)
    await callback.message.edit_text("🗑 Barcha tarixingiz muvaffaqiyatli tozalandi!")


@user_router.callback_query(F.data == "clear_history_no")
async def callback_clear_history_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("⚙️ Amaliyot bekor qilindi.", reply_markup=settings_menu_keyboard())


# ==============================================================================
# 8. ADMIN ROUTER / ADMIN PANEL
# ==============================================================================

@admin_router.message(F.text == "🛠 Admin Panel", F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin_panel(message: Message) -> None:
    await message.answer("🛠 <b>Hamyonim — Admin boshqaruv paneli:</b>", reply_markup=admin_panel_keyboard())


@admin_router.callback_query(F.data == "admin_stats", F.from_user.id.in_(ADMIN_IDS))
async def callback_admin_stats(callback: CallbackQuery) -> None:
    stats = db.get_global_stats()
    active_today = db.count_active_users_today()
    text = (
        "📊 <b>Botning umumiy ko'rsatkichlari:</b>\n\n"
        f"👥 Jami foydalanuvchilar: {stats['total_users']} ta\n"
        f"🚫 Bloklanganlar: {stats['blocked_users']} ta\n"
        f"⚡️ Bugun aktiv bo'lganlar: {active_today} ta\n"
        f"💳 Saqlangan jami kartalar: {stats['total_cards']} ta\n\n"
        f"🧾 Jami operatsiyalar: {stats['total_transactions']} ta\n"
        f"📥 Tizimdagi jami kirim: {format_money(stats['total_income'])}\n"
        f"📤 Tizimdagi jami chiqim: {format_money(stats['total_expense'])}"
    )
    await callback.message.answer(text)
    await callback.answer()


@admin_router.callback_query(F.data == "admin_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def callback_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.entering_message)
    await callback.message.answer("📢 Barcha foydalanuvchilarga yuboriladigan reklama/xabar matnini kiriting:", reply_markup=cancel_keyboard())
    await callback.answer()


@admin_router.message(StateFilter(BroadcastStates.entering_message), F.from_user.id.in_(ADMIN_IDS))
async def process_broadcast_text(message: Message, state: FSMContext) -> None:
    await state.update_data(text=message.text)
    await state.set_state(BroadcastStates.confirming)
    await message.answer(f"❓ Quyidagi xabarni barcha foydalanuvchilarga tarqatishni tasdiqlaysizmi?\n\n{message.text}", reply_markup=broadcast_confirm_keyboard())


@admin_router.callback_query(StateFilter(BroadcastStates.confirming), F.data == "broadcast_confirm", F.from_user.id.in_(ADMIN_IDS))
async def callback_broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    msg_text = data["text"]
    await state.clear()
    
    await callback.message.edit_text("🚀 Xabar tarqatish boshlandi...")
    user_ids = db.get_all_user_ids(only_active=True)
    
    success, failed = 0, 0
    for uid in user_ids:
        try:
            await callback.bot.send_message(chat_id=uid, text=msg_text)
            success += 1
        except TelegramForbiddenError:
            db.set_blocked(uid, True)
            failed += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    await callback.message.answer(f"📢 <b>Xabar tarqatish yakunlandi:</b>\n\n✅ Muvaffaqiyatli yetkazildi: {success} ta\n❌ Yetkazilmadi (Bloklaganlar): {failed} ta")


@admin_router.callback_query(StateFilter(BroadcastStates.confirming), F.data == "broadcast_cancel", F.from_user.id.in_(ADMIN_IDS))
async def callback_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Xabar tarqatish bekor qilindi.")


# ==============================================================================
# 9. ASOSIY ISHGA TUSHIRISH (MAIN RUNNER)
# ==============================================================================

async def main() -> None:
    bot = Bot(token=BOT_TOKEN, properties=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # Routerlarni ulash
    dp.include_router(admin_router)
    dp.include_router(user_router)

    # Bot menyusiga buyruqlarni o'rnatish
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="help", description="Yordam sahifasi"),
        BotCommand(command="balans", description="Balansni ko'rish"),
        BotCommand(command="cancel", description="Amalni bekor qilish"),
    ]
    await bot.set_my_commands(commands)

    logger.info("Bot poling rejimida muvaffaqiyatli ishga tushdi...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot muvaffaqiyatli to'xtatildi.")
