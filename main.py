"""
==============================================================================
 HAMYONIM — Shaxsiy Moliya va Bank Kartalarini Boshqarish Telegram Boti
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

from aiohttp import web
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
# 1. KONFIGURATSIYA (TOKEN JOYLANDI)
# ==============================================================================

BOT_TOKEN: str = os.getenv("HAMYONIM_BOT_TOKEN", "8888847127:AAEbgW0Kk97WPRyqdZaslynlxrNrbE1vNa0")

# Admin sifatida ishlaydigan Telegram user_id lar ro'yxati
ADMIN_IDS: set[int] = {
    int(uid) for uid in os.getenv("HAMYONIM_ADMIN_IDS", "123456789").split(",") if uid.strip().isdigit()
}

DB_PATH: str = os.getenv("HAMYONIM_DB_PATH", "hamyonim.db")
LOG_PATH: str = os.getenv("HAMYONIM_LOG_PATH", "hamyonim.log")

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
# 3. YORDAMCHI FUNKSIYALAR
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
# 4. MA'LUMOTLAR BAZASI QATLAMI (SQLite3, WAL)
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    balance REAL NOT NULL DEFAULT 0,
                    is_blocked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                    amount REAL NOT NULL,
                    category TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    card_number TEXT NOT NULL,
                    card_type TEXT NOT NULL,
                    card_label TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions (user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions (created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_user_id ON cards (user_id);")
        logger.info("Ma'lumotlar bazasi tayyor: %s", self.db_path)

    def add_user(self, user_id: int, username: Optional[str], full_name: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            conn.execute("""
                INSERT INTO users (user_id, username, full_name, balance, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    updated_at = excluded.updated_at;
            """, (user_id, username, full_name, now, now))

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
            conn.execute("""
                UPDATE users
                SET balance = balance + ?, updated_at = ?
                WHERE user_id = ?;
            """, (delta, now, user_id))

    def set_balance(self, user_id: int, new_balance: float) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            conn.execute("""
                UPDATE users
                SET balance = ?, updated_at = ?
                WHERE user_id = ?;
            """, (new_balance, now, user_id))

    def clear_history(self, user_id: int) -> None:
        with closing(self._get_connection()) as conn, conn:
            conn.execute("DELETE FROM transactions WHERE user_id = ?;", (user_id,))
            conn.execute("UPDATE users SET balance = 0 WHERE user_id = ?;", (user_id,))

    def add_transaction(self, user_id: int, tx_type: str, amount: float, category: Optional[str], note: Optional[str]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            cur = conn.execute("""
                INSERT INTO transactions (user_id, type, amount, category, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (user_id, tx_type, amount, category, note, now))
            tx_id = cur.lastrowid

        delta = amount if tx_type == "income" else -amount
        self.update_balance(user_id, delta)
        return tx_id

    def get_transactions(self, user_id: int, limit: int = TRANSACTIONS_PER_PAGE, offset: int = 0) -> list[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("""
                SELECT * FROM transactions
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?;
            """, (user_id, limit, offset))
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
            cur = conn.execute("""
                SELECT type, COALESCE(SUM(amount), 0) AS total
                FROM transactions
                WHERE user_id = ? AND created_at >= ?
                GROUP BY type;
            """, (user_id, since))
            result = {"income": 0.0, "expense": 0.0}
            for row in cur.fetchall():
                result[row["type"]] = row["total"]
            return result

    def add_card(self, user_id: int, card_number: str, card_label: Optional[str]) -> int:
        card_type = detect_card_type(card_number).value
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._get_connection()) as conn, conn:
            cur = conn.execute("""
                INSERT INTO cards (user_id, card_number, card_type, card_label, created_at)
                VALUES (?, ?, ?, ?, ?);
            """, (user_id, clean_card_number(card_number), card_type, card_label, now))
            return cur.lastrowid

    def get_cards(self, user_id: int) -> list[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT * FROM cards WHERE user_id = ? ORDER BY created_at DESC;", (user_id,))
            return cur.fetchall()

    def count_cards(self, user_id: int) -> int:
        with closing(self._get_connection()) as conn:
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM cards WHERE user_id = ?;", (user_id,))
            return cur.fetchone()["cnt"]

    def delete_card(self, card_id: int, user_id: int) -> bool:
        with closing(self._get_connection()) as conn, conn:
            cur = conn.execute("DELETE FROM cards WHERE id = ? AND user_id = ?;", (card_id, user_id))
            return cur.rowcount > 0

db = Database(DB_PATH)

# ==============================================================================
# 5. FSM STATES & KEYBOARDS
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

class EditBalanceStates(StatesGroup):
    entering_amount = State()

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

def settings_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Balansni qo'lda tuzatish", callback_data="settings_edit_balance"))
    builder.row(InlineKeyboardButton(text="🗑 Barcha tarixni tozalash", callback_data="settings_clear_history"))
    return builder.as_markup()

# ==============================================================================
# 6. ROUTER VA HANDLERLAR
# ==============================================================================

router = Router(name="main_router")

@router.message(CommandStart())
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

@router.message(Command("help"))
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

@router.message(Command("cancel"))
@router.message(F.text == "🚫 Bekor qilish")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("❕ Hozircha bekor qilinadigan amal yo'q.")
        return
    await state.clear()
    await message.answer("✅ Amal bekor qilindi.", reply_markup=main_menu_keyboard(message.from_user.id))

@router.message(Command("balans"))
@router.message(F.text == "💰 Balans")
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

# --- KIRIM QO'SHISH ---
@router.message(F.text == "➕ Kirim qo'shish")
async def start_add_income(message: Message, state: FSMContext) -> None:
    await state.set_state(AddIncomeStates.choosing_category)
    await message.answer(
        "📥 <b>Kirim qo'shish</b>\n\nIltimos, kategoriyani tanlang:",
        reply_markup=categories_inline_keyboard(DEFAULT_CATEGORIES_INCOME, "inc_cat"),
    )

@router.callback_query(StateFilter(AddIncomeStates.choosing_category), F.data.startswith("inc_cat:"))
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

@router.message(StateFilter(AddIncomeStates.entering_amount))
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

@router.callback_query(StateFilter(AddIncomeStates.entering_note), F.data.startswith("inc_note:"))
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

@router.message(StateFilter(AddIncomeStates.entering_note))
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

# --- CHIQIM QO'SHISH ---
@router.message(F.text == "➖ Chiqim qo'shish")
async def start_add_expense(message: Message, state: FSMContext) -> None:
    await state.set_state(AddExpenseStates.choosing_category)
    await message.answer(
        "📤 <b>Chiqim qo'shish</b>\n\nIltimos, kategoriyani tanlang:",
        reply_markup=categories_inline_keyboard(DEFAULT_CATEGORIES_EXPENSE, "exp_cat"),
    )

@router.callback_query(StateFilter(AddExpenseStates.choosing_category), F.data.startswith("exp_cat:"))
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
        f"✅ Kategoriya: <b>{category}</b>\n\n💵 Endi summani kiriting (so'mda), masalan: <code>50000</code>"
    )
    await callback.answer()

@router.message(StateFilter(AddExpenseStates.entering_amount))
async def process_expense_amount(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Noto'g'ri format. Iltimos, musbat sonli summa kiriting, masalan: <code>50000</code>")
        return

    await state.update_data(amount=amount)
    await state.set_state(AddExpenseStates.entering_note)
    await message.answer(
        "📝 Izoh qo'shmoqchimisiz? (ixtiyoriy)\nMatn kiriting yoki o'tkazib yuboring:",
        reply_markup=skip_note_keyboard("exp_note"),
    )

@router.callback_query(StateFilter(AddExpenseStates.entering_note), F.data.startswith("exp_note:"))
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

@router.message(StateFilter(AddExpenseStates.entering_note))
async def process_expense_note_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _finalize_expense(message, message.from_user.id, data, note=message.text.strip())
    await state.clear()

async def _finalize_expense(message: Message, user_id: int, data: dict, note: Optional[str]) -> None:
    amount = data["amount"]
    category = data["category"]
    db.add_transaction(user_id, "expense", amount, category, note)

    text = (
        "✅ <b>Chiqim muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📂 Kategoriya: {category}\n"
        f"💸 Summa: {format_money(amount)}\n"
    )
    if note:
        text += f"📝 Izoh: {escape_html(note)}\n"
    text += f"\n💰 Yangi balans: <b>{format_money(db.get_balance(user_id))}</b>"

    await message.answer(text, reply_markup=main_menu_keyboard(user_id))

# --- KARTALAR VA SOZLAMALAR ---
@router.message(F.text == "💳 Kartalarim")
async def show_cards(message: Message) -> None:
    cards = db.get_cards(message.from_user.id)
    text = "💳 <b>Sizning bank kartalaringiz:</b>\n\n"
    if not cards:
        text += "Hozircha hech qanday karta qo'shilmagan."
    else:
        for c in cards:
            label = c["card_label"] or c["card_type"]
            text += f"• <b>{label}</b>: <code>{mask_card_number(c['card_number'])}</code>\n"

    await message.answer(text, reply_markup=cards_list_keyboard(cards))

@router.callback_query(F.data == "card_add")
async def start_add_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCardStates.entering_number)
    await callback.message.answer("💳 16 xonali karta raqamini kiriting (masalan: <code>8600 1234 5678 9012</code>):")
    await callback.answer()

@router.message(StateFilter(AddCardStates.entering_number))
async def process_card_number(message: Message, state: FSMContext) -> None:
    if not is_valid_card_number(message.text):
        await message.answer("⚠️ Karta raqami noto'g'ri! 16 ta raqamdan iborat bo'lishi kerak. Qayta kiriting:")
        return

    await state.update_data(card_number=clean_card_number(message.text))
    await state.set_state(AddCardStates.entering_label)
    await message.answer("🏷 Kartangiz uchun nom (masalan: <i>Asosiy Uzcard</i>) kiriting yoki o'tkazib yuborish uchun '.' yuboring:")

@router.message(StateFilter(AddCardStates.entering_label))
async def process_card_label(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    label = None if message.text.strip() == "." else message.text.strip()
    db.add_card(message.from_user.id, data["card_number"], label)
    await state.clear()
    await message.answer("✅ Karta muvaffaqiyatli saqlandi!", reply_markup=main_menu_keyboard(message.from_user.id))

@router.message(F.text == "⚙️ Sozlamalar")
async def show_settings(message: Message) -> None:
    await message.answer("⚙️ <b>Sozlamalar bo'limi:</b>", reply_markup=settings_menu_keyboard())

@router.callback_query(F.data == "settings_edit_balance")
async def edit_balance_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditBalanceStates.entering_amount)
    await callback.message.answer("✏️ Yangi balansingiz qiymatini kiriting (masalan: <code>500000</code>):")
    await callback.answer()

@router.message(StateFilter(EditBalanceStates.entering_amount))
async def process_edit_balance(message: Message, state: FSMContext) -> None:
    try:
        new_balance = float(message.text.strip().replace(" ", "").replace(",", "."))
        db.set_balance(message.from_user.id, new_balance)
        await state.clear()
        await message.answer(f"✅ Balans o'zgartirildi: <b>{format_money(new_balance)}</b>", reply_markup=main_menu_keyboard(message.from_user.id))
    except ValueError:
        await message.answer("⚠️ Iltimos, to'g'ri son kiriting.")

@router.callback_query(F.data == "settings_clear_history")
async def clear_history(callback: CallbackQuery) -> None:
    db.clear_history(callback.from_user.id)
    await callback.message.edit_text("🗑 Barcha tarix va balans tozalandi!")
    await callback.answer()

# ==============================================================================
# 7. RENDER UCHUN SOKET WEB-SERVER VA DASTURNI ISHGA TUSHIRISH
# ==============================================================================

async def handle_ping(request):
    return web.Response(text="Hamyonim Bot is active!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Render uchun Web-Server {port}-portda ishga tushdi.")

async def main():
    # 1. Soxta serverni parallel ishlatish (Render to'xtab qolmasligi uchun)
    await start_web_server()
    
    # 2. Bot va Dispatcher yaratish
    bot = Bot(token=BOT_TOKEN, properties=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot polling rejimida ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")
