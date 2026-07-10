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

BOT_TOKEN: str = os.getenv("HAMYONIM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# Admin sifatida ishlaydigan Telegram user_id lar ro'yxati
ADMIN_IDS: set[int] = {
    int(uid) for uid in os.getenv("HAMYONIM_ADMIN_IDS", "123456789").split(",") if uid.strip().isdigit()
}

DB_PATH: str = os.getenv("HAMYONIM_DB_PATH", "hamyonim.db")
LOG_PATH: str = os.getenv("HAMYONIM_LOG_PATH", "hamyonim.log")

# Sahifalash (pagination) sozlamalari
TRANSACTIONS_PER_PAGE: int = 8
USERS_PER_BROADCAST_BATCH: int = 25
BROADCAST_DELAY_SECONDS: float = 0.05  # Telegram flood-limit dan qochish uchun

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
    "🏠 Uy-joy",
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
    """Bot uchun konsol va faylga yozadigan logger sozlaydi."""
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
    """Karta raqamidan probel, tire va boshqa belgilarni tozalaydi."""
    return re.sub(r"[^\d]", "", raw)


def detect_card_type(card_number: str) -> CardType:
    """
    Karta raqami bo'yicha turini aniqlaydi.
    Uzcard: 8600 bilan boshlanadi (asosan)
    Humo:   9860 bilan boshlanadi (asosan)
    """
    digits = clean_card_number(card_number)
    if digits.startswith("8600"):
        return CardType.UZCARD
    if digits.startswith("9860"):
        return CardType.HUMO
    return CardType.UNKNOWN


def is_valid_card_number(card_number: str) -> bool:
    """Karta raqami 16 ta raqamdan iborat ekanligini tekshiradi."""
    digits = clean_card_number(card_number)
    return len(digits) == 16 and digits.isdigit()


def mask_card_number(card_number: str) -> str:
    """
    Xavfsizlik maqsadida karta raqamini niqoblaydi.
    Faqat birinchi 4 va oxirgi 4 raqam ko'rinadi: 8600 **** **** 1234
    """
    digits = clean_card_number(card_number)
    if len(digits) != 16:
        return "**** **** **** ****"
    return f"{digits[:4]} **** **** {digits[12:]}"


def format_money(amount: float) -> str:
    """Summani chiroyli formatga o'tkazadi: 1234567.5 -> 1 234 567.50 so'm"""
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    integer_part = int(amount)
    decimal_part = round((amount - integer_part) * 100)
    grouped = f"{integer_part:,}".replace(",", " ")
    return f"{sign}{grouped}.{decimal_part:02d} so'm"


def format_datetime(dt_str: str) -> str:
    """ISO formatdagi vaqtni foydalanuvchiga qulay formatga o'tkazadi."""
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return dt_str


def escape_html(text: str) -> str:
    """HTML parse mode uchun maxsus belgilarni ekranlaydi."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ==============================================================================
# 4. MA'LUMOTLAR BAZASI QATLAMI (SQLite3, WAL rejimi)
# ==============================================================================

class Database:
    """
    SQLite3 ma'lumotlar bazasi bilan ishlash uchun barcha metodlarni
    o'z ichiga olgan sinf. WAL (Write-Ahead Logging) rejimi yoqilgan —
    bu bir vaqtning o'zida o'qish va yozish tezligini oshiradi.
    """

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
        """Barcha jadvallarni yaratadi (agar mavjud bo'lmasa)."""
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions (user_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions (created_at);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cards_user_id ON cards (user_id);"
            )
        logger.info("Ma'lumotlar bazasi muvaffaqiyatli ishga tushirildi: %s", self.db_path)

    # ---------------------------------------------------------------- USERS

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

    # --------------------------------------------------------- TRANSACTIONS

    def add_transaction(
        self,
        user_id: int,
        tx_type: str,
        amount: float,
        category: Optional[str],
        note: Optional[str],
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

    def get_transactions(
        self, user_id: int, limit: int = TRANSACTIONS_PER_PAGE, offset: int = 0
    ) -> list[sqlite3.Row]:
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
            cur = conn.execute(
                "SELECT COUNT(*) AS cnt FROM transactions WHERE user_id = ?;", (user_id,)
            )
            return cur.fetchone()["cnt"]

    def delete_transaction(self, tx_id: int, user_id: int) -> bool:
        with closing(self._get_connection()) as conn:
            cur = conn.execute(
                "SELECT * FROM transactions WHERE id = ? AND user_id = ?;", (tx_id, user_id)
            )
            row = cur.fetchone()
            if row is None:
                return False
            with conn:
                conn.execute("DELETE FROM transactions WHERE id = ?;", (tx_id,))
            delta = -row["amount"] if row["type"] == "income" else row["amount"]
            self.update_balance(user_id, delta)
            return True

    def get_stats_summary(self, user_id: int, days: int = 30) -> dict[str, float]:
        """Berilgan davr (kunlarda) uchun kirim/chiqim yig'indisini qaytaradi."""
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

    # --------------------------------------------------------------- CARDS

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
            cur = conn.execute(
                "SELECT * FROM cards WHERE user_id = ? ORDER BY created_at DESC;", (user_id,)
            )
            return cur.fetchall()

    def get_card(self, card_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            cur = conn.execute(
                "SELECT * FROM cards WHERE id = ? AND user_id = ?;", (card_id, user_id)
            )
            return cur.fetchone()

    def count_cards(self, user_id: int) -> int:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM cards WHERE user_id = ?;", (user_id,))
            return cur.fetchone()["cnt"]

    def delete_card(self, card_id: int, user_id: int) -> bool:
        with closing(self._get_connection()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM cards WHERE id = ? AND user_id = ?;", (card_id, user_id)
            )
            return cur.rowcount > 0

    # --------------------------------------------------------- ADMIN STATS

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
            blocked = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE is_blocked = 1;"
            ).fetchone()["cnt"]
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
    """Asosiy menyu klaviaturasi. Admin uchun qo'shimcha tugma qo'shiladi."""
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


def transactions_pagination_keyboard(
    current_page: int, total_pages: int, tx_ids: list[int]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for tx_id in tx_ids:
        builder.add(
            InlineKeyboardButton(text=f"🗑 #{tx_id} o'chirish", callback_data=f"tx_del:{tx_id}:{current_page}")
        )
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
    builder.row(InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"))
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
        f"✅ Kategoriya: <b>{category}</b>\n\n💵 Endi summani kiriting (so'mda), masalan: <code>150000</code>"
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
        f"✅ Kategoriya: <b>{category}</b>\n\n💵 Endi summani kiriting (so'mda), masalan: <code>45000</code>"
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
        await message.answer("⚠️ Noto'g'ri format. Iltimos, musbat sonli summa kiriting, masalan: <code>45000</code>")
        return

    await state.update_data(amount=amount)
    await state.set_state(AddExpenseStates.entering_note)
    await message.answer(
        "📝 Izoh qo'shmoqchimisiz? (ixtiyoriy)\nMatn kiriting yoki o'tkazib yuboring:",
        reply_markup=skip_note_keyboard("exp_note"),
    )


@user_router.callback_query(StateFilter(AddExpenseStates.entering_note), F.data.startswith("exp_note:"))
async def process_expense_note_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, action = callback.data.split(":", maxsplit=1)
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("🚫 Chiqim qo'shish bekor qilindi.")
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

    balance = db.get_balance(user_id)
    warning = "\n\n⚠️ <b>Diqqat:</b> balansingiz manfiy!" if balance < 0 else ""

    text = (
        "✅ <b>Chiqim muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📂 Kategoriya: {category}\n"
        f"💵 Summa: {format_money(amount)}\n"
    )
    if note:
        text += f"📝 Izoh: {escape_html(note)}\n"
    text += f"\n💰 Yangi balans: <b>{format_money(balance)}</b>{warning}"

    await message.answer(text, reply_markup=main_menu_keyboard(user_id))


# ------------------------------------------------------------------------
# 7.4. TRANZAKSIYALAR TARIXI (PAGINATION)
# ------------------------------------------------------------------------

def _build_transactions_page_text(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = db.count_transactions(user_id)
    total_pages = max((total + TRANSACTIONS_PER_PAGE - 1) // TRANSACTIONS_PER_PAGE, 1)
    page = max(0, min(page, total_pages - 1))

    rows = db.get_transactions(user_id, limit=TRANSACTIONS_PER_PAGE, offset=page * TRANSACTIONS_PER_PAGE)

    if not rows:
        text = "🧾 Sizda hali birorta ham tranzaksiya yo'q."
        return text, transactions_pagination_keyboard(0, 1, [])

    lines = [f"🧾 <b>Tranzaksiyalar tarixi</b> (sahifa {page + 1}/{total_pages})\n"]
    for row in rows:
        icon = "📥" if row["type"] == "income" else "📤"
        sign = "+" if row["type"] == "income" else "-"
        lines.append(
            f"{icon} #{row['id']} | {sign}{format_money(row['amount'])} | "
            f"{row['category'] or '—'} | {format_datetime(row['created_at'])}"
        )
        if row["note"]:
            lines.append(f"   📝 {escape_html(row['note'])}")

    text = "\n".join(lines)
    keyboard = transactions_pagination_keyboard(page, total_pages, [row["id"] for row in rows])
    return text, keyboard


@user_router.message(F.text == "🧾 Tranzaksiyalar tarixi")
async def show_transactions_history(message: Message) -> None:
    text, keyboard = _build_transactions_page_text(message.from_user.id, page=0)
    await message.answer(text, reply_markup=keyboard)


@user_router.callback_query(F.data.startswith("tx_page:"))
async def paginate_transactions(callback: CallbackQuery) -> None:
    _, page_str = callback.data.split(":", maxsplit=1)
    text, keyboard = _build_transactions_page_text(callback.from_user.id, page=int(page_str))
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramAPIError:
        pass
    await callback.answer()


@user_router.callback_query(F.data == "tx_noop")
async def tx_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@user_router.callback_query(F.data.startswith("tx_del:"))
async def delete_transaction_callback(callback: CallbackQuery) -> None:
    _, tx_id_str, page_str = callback.data.split(":", maxsplit=2)
    tx_id, page = int(tx_id_str), int(page_str)

    success = db.delete_transaction(tx_id, callback.from_user.id)
    if success:
        await callback.answer("✅ Tranzaksiya o'chirildi.")
    else:
        await callback.answer("⚠️ Tranzaksiya topilmadi.", show_alert=True)

    text, keyboard = _build_transactions_page_text(callback.from_user.id, page=page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramAPIError:
        pass


# ------------------------------------------------------------------------
# 7.5. STATISTIKA
# ------------------------------------------------------------------------

@user_router.message(F.text == "📊 Statistika")
async def show_statistics_menu(message: Message) -> None:
    await message.answer(
        "📊 <b>Statistika</b>\n\nQaysi davr uchun statistikani ko'rmoqchisiz?",
        reply_markup=stats_period_keyboard(),
    )


@user_router.callback_query(F.data.startswith("stats_period:"))
async def show_statistics(callback: CallbackQuery) -> None:
    _, days_str = callback.data.split(":", maxsplit=1)
    days = int(days_str)
    user_id = callback.from_user.id

    summary = db.get_stats_summary(user_id, days=days)
    income_breakdown = db.get_category_breakdown(user_id, "income", days=days)
    expense_breakdown = db.get_category_breakdown(user_id, "expense", days=days)

    net = summary["income"] - summary["expense"]
    net_emoji = "🟢" if net >= 0 else "🔴"

    lines = [
        f"📊 <b>Oxirgi {days} kunlik statistika</b>\n",
        f"📥 Jami kirim: <b>{format_money(summary['income'])}</b>",
        f"📤 Jami chiqim: <b>{format_money(summary['expense'])}</b>",
        f"{net_emoji} Sof natija: <b>{format_money(net)}</b>\n",
    ]

    if expense_breakdown:
        lines.append("📤 <b>Chiqimlar kategoriya bo'yicha:</b>")
        for row in expense_breakdown[:6]:
            lines.append(f"  • {row['category'] or '—'}: {format_money(row['total'])} ({row['cnt']} ta)")

    if income_breakdown:
        lines.append("\n📥 <b>Kirimlar kategoriya bo'yicha:</b>")
        for row in income_breakdown[:6]:
            lines.append(f"  • {row['category'] or '—'}: {format_money(row['total'])} ({row['cnt']} ta)")

    try:
        await callback.message.edit_text("\n".join(lines))
    except TelegramAPIError:
        await callback.message.answer("\n".join(lines))
    await callback.answer()


# ------------------------------------------------------------------------
# 7.6. KARTALARNI BOSHQARISH
# ------------------------------------------------------------------------

@user_router.message(F.text == "💳 Kartalarim")
async def show_cards(message: Message) -> None:
    cards = db.get_cards(message.from_user.id)
    if not cards:
        text = "💳 Sizda hali saqlangan karta yo'q.\n\nYangi karta qo'shish uchun tugmani bosing 👇"
    else:
        lines = ["💳 <b>Saqlangan kartalaringiz:</b>\n"]
        for card in cards:
            label = card["card_label"] or "Nomsiz"
            lines.append(f"• {card['card_type']} — {mask_card_number(card['card_number'])} ({label})")
        text = "\n".join(lines)

    await message.answer(text, reply_markup=cards_list_keyboard(cards))


@user_router.callback_query(F.data == "card_add")
async def start_add_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCardStates.entering_number)
    await callback.message.answer(
        "💳 <b>Yangi karta qo'shish</b>\n\n"
        "Karta raqamini kiriting (16 ta raqam):\n"
        "<i>Masalan: 8600 1234 5678 9012</i>\n\n"
        "🔒 Karta raqamingiz xavfsiz saqlanadi va faqat siz ko'ra olasiz.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@user_router.message(StateFilter(AddCardStates.entering_number))
async def process_card_number(message: Message, state: FSMContext) -> None:
    card_number = message.text.strip()

    # Xavfsizlik: foydalanuvchi yuborgan xabarni darhol o'chirishga harakat qilamiz,
    # chunki unda to'liq karta raqami mavjud.
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    if not is_valid_card_number(card_number):
        await message.answer(
            "⚠️ Noto'g'ri format. Karta raqami 16 ta raqamdan iborat bo'lishi kerak.\n"
            "Qaytadan kiriting yoki bekor qiling:",
            reply_markup=cancel_keyboard(),
        )
        return

    card_type = detect_card_type(card_number)
    await state.update_data(card_number=clean_card_number(card_number), card_type=card_type.value)
    await state.set_state(AddCardStates.entering_label)
    await message.answer(
        f"✅ Karta turi aniqlandi: <b>{card_type.value}</b>\n\n"
        "Kartaga nom bering (ixtiyoriy), masalan: <i>Asosiy karta</i>\n"
        "Yoki '-' belgisini yuboring, agar nom bermoqchi bo'lmasangiz.",
        reply_markup=cancel_keyboard(),
    )


@user_router.message(StateFilter(AddCardStates.entering_label))
async def process_card_label(message: Message, state: FSMContext) -> None:
    label = message.text.strip()
    if label == "-":
        label = None

    data = await state.get_data()
    card_id = db.add_card(message.from_user.id, data["card_number"], label)
    await state.clear()

    card = db.get_card(card_id, message.from_user.id)
    text = (
        "✅ <b>Karta muvaffaqiyatli qo'shildi!</b>\n\n"
        f"💳 Turi: {card['card_type']}\n"
        f"🔢 Raqami: {mask_card_number(card['card_number'])}\n"
        f"🏷 Nomi: {card['card_label'] or 'Nomsiz'}"
    )
    await message.answer(text, reply_markup=main_menu_keyboard(message.from_user.id))


@user_router.callback_query(F.data.startswith("card_del:"))
async def confirm_delete_card(callback: CallbackQuery) -> None:
    _, card_id_str = callback.data.split(":", maxsplit=1)
    card_id = int(card_id_str)
    card = db.get_card(card_id, callback.from_user.id)

    if card is None:
        await callback.answer("⚠️ Karta topilmadi.", show_alert=True)
        return

    await callback.message.answer(
        f"❓ <b>{card['card_type']} — {mask_card_number(card['card_number'])}</b> "
        "kartasini o'chirishni tasdiqlaysizmi?",
        reply_markup=confirm_card_delete_keyboard(card_id),
    )
    await callback.answer()


@user_router.callback_query(F.data.startswith("card_del_yes:"))
async def delete_card_confirmed(callback: CallbackQuery) -> None:
    _, card_id_str = callback.data.split(":", maxsplit=1)
    success = db.delete_card(int(card_id_str), callback.from_user.id)

    if success:
        await callback.message.edit_text("✅ Karta muvaffaqiyatli o'chirildi.")
    else:
        await callback.message.edit_text("⚠️ Karta topilmadi yoki allaqachon o'chirilgan.")
    await callback.answer()


@user_router.callback_query(F.data == "card_del_no")
async def delete_card_cancelled(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🚫 O'chirish bekor qilindi.")
    await callback.answer()


# ------------------------------------------------------------------------
# 7.7. SOZLAMALAR
# ------------------------------------------------------------------------

@user_router.message(F.text == "⚙️ Sozlamalar")
async def show_settings(message: Message) -> None:
    await message.answer("⚙️ <b>Sozlamalar</b>\n\nKerakli bo'limni tanlang:", reply_markup=settings_menu_keyboard())


@user_router.callback_query(F.data == "settings_edit_balance")
async def start_edit_balance(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditBalanceStates.entering_amount)
    current = db.get_balance(callback.from_user.id)
    await callback.message.answer(
        f"✏️ Joriy balansingiz: <b>{format_money(current)}</b>\n\n"
        "Yangi balans qiymatini kiriting (masalan: <code>500000</code>):",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@user_router.message(StateFilter(EditBalanceStates.entering_amount))
async def process_edit_balance(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "").replace(",", ".")
    try:
        new_balance = float(raw)
    except ValueError:
        await message.answer("⚠️ Noto'g'ri format. Faqat son kiriting, masalan: <code>500000</code>")
        return

    user_id = message.from_user.id
    current = db.get_balance(user_id)
    db.update_balance(user_id, new_balance - current)
    await state.clear()

    await message.answer(
        f"✅ Balans yangilandi: <b>{format_money(new_balance)}</b>",
        reply_markup=main_menu_keyboard(user_id),
    )


@user_router.callback_query(F.data == "settings_clear_history")
async def confirm_clear_history(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "❓ Barcha tranzaksiyalar tarixini butunlay o'chirishni tasdiqlaysizmi?\n"
        "⚠️ Bu amalni ortga qaytarib bo'lmaydi!",
        reply_markup=confirm_clear_history_keyboard(),
    )
    await callback.answer()


@user_router.callback_query(F.data == "clear_history_yes")
async def clear_history_confirmed(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("DELETE FROM transactions WHERE user_id = ?;", (user_id,))
        conn.execute("UPDATE users SET balance = 0 WHERE user_id = ?;", (user_id,))
    await callback.message.edit_text("✅ Barcha tranzaksiyalar tarixi tozalandi va balans nolga tushirildi.")
    await callback.answer()


@user_router.callback_query(F.data == "clear_history_no")
async def clear_history_cancelled(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🚫 Bekor qilindi. Ma'lumotlaringiz saqlanib qoldi.")
    await callback.answer()


# ==============================================================================
# 8. ADMIN PANEL
# ==============================================================================

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@admin_router.message(F.text == "🛠 Admin Panel")
async def show_admin_panel(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Admin Panel</b>\n\nKerakli bo'limni tanlang:", reply_markup=admin_panel_keyboard())


@admin_router.callback_query(F.data == "admin_stats")
async def show_admin_stats(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Sizda ruxsat yo'q.", show_alert=True)
        return

    stats = db.get_global_stats()
    active_today = db.count_active_users_today()

    text = (
        "📊 <b>Umumiy statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{stats['total_users']}</b>\n"
        f"🟢 Bugun faol bo'lganlar: <b>{active_today}</b>\n"
        f"🚫 Bloklanganlar: <b>{stats['blocked_users']}</b>\n\n"
        f"🧾 Jami tranzaksiyalar: <b>{stats['total_transactions']}</b>\n"
        f"💳 Jami kartalar: <b>{stats['total_cards']}</b>\n\n"
        f"📥 Barcha foydalanuvchilar kirimi: <b>{format_money(stats['total_income'])}</b>\n"
        f"📤 Barcha foydalanuvchilar chiqimi: <b>{format_money(stats['total_expense'])}</b>"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    except TelegramAPIError:
        await callback.message.answer(text, reply_markup=admin_panel_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Sizda ruxsat yo'q.", show_alert=True)
        return

    await state.set_state(BroadcastStates.entering_message)
    await callback.message.answer(
        "📢 <b>Xabar tarqatish</b>\n\n"
        "Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni kiriting:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@admin_router.message(StateFilter(BroadcastStates.entering_message))
async def process_broadcast_message(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.html_text or message.text)
    await state.set_state(BroadcastStates.confirming)

    total_users = db.count_users()
    await message.answer(
        f"📋 <b>Xabar ko'rinishi:</b>\n\n{message.html_text or message.text}\n\n"
        f"👥 Ushbu xabar <b>{total_users}</b> ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
        reply_markup=broadcast_confirm_keyboard(),
    )


@admin_router.callback_query(StateFilter(BroadcastStates.confirming), F.data == "broadcast_confirm")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()

    await callback.message.edit_text("📤 Xabar tarqatish boshlandi, iltimos kuting...")
    await callback.answer()

    user_ids = db.get_all_user_ids(only_active=True)
    sent, failed = 0, 0

    for idx, uid in enumerate(user_ids, start=1):
        try:
            await bot.send_message(uid, text)
            sent += 1
        except TelegramForbiddenError:
            db.set_blocked(uid, True)
            failed += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(uid, text)
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramAPIError as e:
            logger.warning("Broadcast xatoligi (user_id=%s): %s", uid, e)
            failed += 1

        if idx % USERS_PER_BROADCAST_BATCH == 0:
            await asyncio.sleep(BROADCAST_DELAY_SECONDS * USERS_PER_BROADCAST_BATCH)
        else:
            await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    await callback.message.answer(
        f"✅ <b>Xabar tarqatish yakunlandi!</b>\n\n"
        f"📤 Yuborildi: {sent}\n"
        f"❌ Muvaffaqiyatsiz: {failed}"
    )
    logger.info("Broadcast yakunlandi. Yuborildi: %s, Muvaffaqiyatsiz: %s", sent, failed)


@admin_router.callback_query(StateFilter(BroadcastStates.confirming), F.data == "broadcast_cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("🚫 Xabar tarqatish bekor qilindi.")
    await callback.answer()


@admin_router.message(Command("stats"))
async def cmd_admin_stats_shortcut(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    stats = db.get_global_stats()
    text = (
        f"👥 Foydalanuvchilar: {stats['total_users']} | "
        f"🧾 Tranzaksiyalar: {stats['total_transactions']} | "
        f"💳 Kartalar: {stats['total_cards']}"
    )
    await message.answer(text)


# ==============================================================================
# 9. MIDDLEWARE — LOGGING VA THROTTLING
# ==============================================================================

class LoggingMiddleware:
    """Har bir kelgan xabar/callback ni logga yozadi."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            logger.info(
                "Kiruvchi event: user_id=%s, username=%s, type=%s",
                user.id,
                user.username,
                type(event).__name__,
            )
        return await handler(event, data)


class ThrottlingMiddleware:
    """
    Foydalanuvchi juda tez-tez xabar yuborayotgan bo'lsa (flood),
    uni vaqtincha cheklaydi.
    """

    def __init__(self, rate_limit: float = 0.4) -> None:
        self.rate_limit = rate_limit
        self._last_call: dict[int, datetime] = {}

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            now = datetime.now()
            last = self._last_call.get(user.id)
            if last is not None and (now - last).total_seconds() < self.rate_limit:
                return  # Xabarni e'tiborsiz qoldiramiz (flood himoyasi)
            self._last_call[user.id] = now
        return await handler(event, data)


# ==============================================================================
# 10. FALLBACK HANDLER (Tushunilmagan xabarlar uchun)
# ==============================================================================

@user_router.message(StateFilter(None))
async def fallback_handler(message: Message) -> None:
    """Hech qaysi handlerga mos kelmagan xabarlar uchun."""
    if not db.user_exists(message.from_user.id):
        db.add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)

    await message.answer(
        "❓ Kechirasiz, buyruqni tushunmadim.\n"
        "Iltimos, quyidagi menyudan foydalaning yoki /help buyrug'ini yuboring.",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


# ==============================================================================
# 11. BOT BUYRUQLARI RO'YXATI (Telegram menyusi uchun)
# ==============================================================================

async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="balans", description="Joriy balansni ko'rish"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="cancel", description="Joriy amalni bekor qilish"),
    ]
    await bot.set_my_commands(commands)


# ==============================================================================
# 12. DASTURNI ISHGA TUSHIRISH (ENTRY POINT)
# ==============================================================================

async def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN sozlanmagan! HAMYONIM_BOT_TOKEN muhit o'zgaruvchisini o'rnating.")
        sys.exit(1)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Middleware larni ulash
    dp.message.middleware(ThrottlingMiddleware(rate_limit=0.3))
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())

    # Routerlarni ulash (admin_router avval, aniqroq filtrlar birinchi ishlaydi)
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await set_bot_commands(bot)

    logger.info("=" * 60)
    logger.info("HAMYONIM BOTI ISHGA TUSHMOQDA...")
    logger.info("Adminlar soni: %s", len(ADMIN_IDS))
    logger.info("=" * 60)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Bot to'xtatildi.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot foydalanuvchi tomonidan to'xtatildi.")
