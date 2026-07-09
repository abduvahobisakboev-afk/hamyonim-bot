# ==============================================================================
#                      HAMYONIM TELEGRAM BOT - MAIN PROGRAM
# ==============================================================================
# Dastur muallifi: Abduvakhob Isakboev
# Platforma: Python 3.11+ / aiogram 3.x / SQLite3 / Render Web Hosting
# Tavsif: Foydalanuvchi kartalarini boshqarish va xavfsiz saqlash boti
# ==============================================================================

import logging
import sqlite3
import re
import asyncio
import gc
import sys
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

from aiogram import Bot, Dispatcher, F, html
from aiogram.types import (
    Message, 
    CallbackQuery, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ==============================================================================
# 1. TIZIM VA LOGGING SOZLAMALARI
# ==============================================================================

BOT_TOKEN = "8888847127:AAHJwLGdphr3JLEaGMreFAuCnNCQ1Zlp_LU"
ADMIN_ID = 1673990832
DB_PATH = "bank_bot_v3.db"  # Yangi baza nomi (Eski xatoliklar to'liq yo'qolishi uchun)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - (%(filename)s:%(lineno)d) - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("HamyonimBot")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ==============================================================================
# 2. MA'LUMOTLAR BAZASI BILAN ISHLASH (SQLITE3 AUTO-MIGRATION)
# ==============================================================================

def get_db_connection() -> Tuple[sqlite3.Connection, sqlite3.Cursor]:
    """SQLite bazasiga xavfsiz ulanish."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA cache_size=-2000;")
    return conn, cursor


def init_db() -> None:
    """
    Baza va jadvallarni yaratadi, agar eski baza bo'lsa yetishmayotgan ustunlarni
    avtomatik ravishda qo'shadi (Migration).
    """
    try:
        conn, cursor = get_db_connection()
        
        # 1. Users jadvali
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            username TEXT,
            status TEXT DEFAULT 'active',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 2. Cards jadvali
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS my_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_number TEXT NOT NULL,
            card_date TEXT NOT NULL,
            card_type TEXT DEFAULT '🔹 Uzcard',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
        """)

        # 3. Transactions jadvali
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
        """)

        # AVTO-MIGRATION: Ustunlar mavjudligini tekshirish va qo'shish
        cursor.execute("PRAGMA table_info(my_cards);")
        columns = [column[1] for column in cursor.fetchall()]
        
        if "card_type" not in columns:
            cursor.execute("ALTER TABLE my_cards ADD COLUMN card_type TEXT DEFAULT '🔹 Uzcard';")
            logger.info("my_cards jadvaliga 'card_type' ustuni muvaffaqiyatli qo'shildi.")

        conn.commit()
        conn.close()
        logger.info("Ma'lumotlar bazasi va avto-migratsiya muvaffaqiyatli yakunlandi.")
    except Exception as e:
        logger.error(f"Baza sozlashda xatolik: {e}")


# Baza jadvallarini dastur boshlanishida sozlash
init_db()


def db_add_or_update_user(user_id: int, full_name: str, username: Optional[str]) -> bool:
    """Foydalanuvchini bazaga qo'shish/yangilash."""
    try:
        conn, cursor = get_db_connection()
        u_name = username if username else "mavjud_emas"
        cursor.execute("""
            INSERT INTO users (user_id, full_name, username) 
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                full_name = excluded.full_name,
                username = excluded.username
        """, (user_id, full_name, u_name))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Foydalanuvchi saqlashda xatolar: {e}")
        return False


def db_add_card(user_id: int, card_number: str, card_date: str, card_type: str) -> bool:
    """Yangi kartani xatolarsiz saqlash."""
    try:
        conn, cursor = get_db_connection()
        cursor.execute("""
            INSERT INTO my_cards (user_id, card_number, card_date, card_type) 
            VALUES (?, ?, ?, ?)
        """, (user_id, str(card_number), str(card_date), str(card_type)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Karta qo'shishda xatolik: {e}")
        return False


def db_get_user_cards(user_id: int) -> List[Tuple[Any, ...]]:
    """Foydalanuvchi kartalarini olish."""
    try:
        conn, cursor = get_db_connection()
        cursor.execute("""
            SELECT id, card_number, card_date, card_type, added_at 
            FROM my_cards 
            WHERE user_id = ?
            ORDER BY id DESC
        """, (user_id,))
        cards = cursor.fetchall()
        conn.close()
        return cards
    except Exception as e:
        logger.error(f"Kartalarni olishda xatolik: {e}")
        return []


def db_delete_card(card_id: int, user_id: int) -> bool:
    """Kartani o'chirish."""
    try:
        conn, cursor = get_db_connection()
        cursor.execute("DELETE FROM my_cards WHERE id = ? AND user_id = ?", (card_id, user_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Kartani o'chirishda xatolik: {e}")
        return False


def db_get_all_users() -> List[Tuple[Any, ...]]:
    """Admin uchun foydalanuvchilar ro'yxati."""
    try:
        conn, cursor = get_db_connection()
        cursor.execute("SELECT user_id, full_name, username, joined_at FROM users ORDER BY joined_at DESC")
        users = cursor.fetchall()
        conn.close()
        return users
    except Exception as e:
        logger.error(f"Foydalanuvchilarni olishda xatolik: {e}")
        return []


def db_get_total_stats() -> Dict[str, int]:
    """Tizim statistikasi."""
    stats = {"users_count": 0, "cards_count": 0}
    try:
        conn, cursor = get_db_connection()
        cursor.execute("SELECT COUNT(*) FROM users")
        stats["users_count"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM my_cards")
        stats["cards_count"] = cursor.fetchone()[0]
        conn.close()
    except Exception as e:
        logger.error(f"Statistikada xatolik: {e}")
    return stats


# ==============================================================================
# 3. FSM (FORM STATE MANAGEMENT)
# ==============================================================================

class CardState(StatesGroup):
    waiting_for_type = State()
    waiting_for_number = State()
    waiting_for_date = State()

class AdminBroadcastState(StatesGroup):
    waiting_for_message = State()


# ==============================================================================
# 4. TUGMALAR VA INTERFEYS (KEYBOARDS)
# ==============================================================================

BTN_CARDS = "💳 Mening kartalarim"
BTN_ADD_CARD = "➕ Yangi karta qo'shish"
BTN_HISTORY = "🧾 Tushumlar tarixi"
BTN_CONTACT = "👨‍💻 Adminga bog'lanish"
BTN_ADMIN = "⚙️ Admin panel"
BTN_CANCEL = "❌ Bekor qilish"

ALL_MAIN_BUTTONS = [BTN_CARDS, BTN_ADD_CARD, BTN_HISTORY, BTN_CONTACT, BTN_ADMIN, BTN_CANCEL]


def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Asosiy menyu."""
    keyboard_layout = [
        [KeyboardButton(text=BTN_CARDS)],
        [KeyboardButton(text=BTN_ADD_CARD), KeyboardButton(text=BTN_HISTORY)],
        [KeyboardButton(text=BTN_CONTACT)]
    ]
    if user_id == ADMIN_ID:
        keyboard_layout.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=keyboard_layout, resize_keyboard=True)


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Bekor qilish tugmasi."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True
    )


def get_card_type_inline() -> InlineKeyboardMarkup:
    """Karta turini tanlash."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔹 Uzcard", callback_data="select_uzcard"),
                InlineKeyboardButton(text="🟠 Humo", callback_data="select_humo")
            ]
        ]
    )


def get_empty_cards_inline() -> InlineKeyboardMarkup:
    """Karta bo'lmaganda chiroyli tugma."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Karta qo'shishni bosing", callback_data="start_add_card_inline")]
        ]
    )


def get_card_action_inline(card_id: int) -> InlineKeyboardMarkup:
    """Karta o'chirish tugmasi."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Ushbu kartani o'chirish", callback_data=f"delete_card_{card_id}")]
        ]
    )


def get_admin_panel_inline() -> InlineKeyboardMarkup:
    """Admin panel tugmalari."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Barcha foydalanuvchilarga xabar yuborish", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔄 Statistikani yangilash", callback_data="admin_refresh_stats")]
        ]
    )


# ==============================================================================
# 5. HANDLERLAR (BOT LOGIKASI)
# ==============================================================================

@dp.message(CommandStart())
async def cmd_start_handler(message: Message, state: FSMContext):
    """/start buyrug'i."""
    await state.clear()
    user = message.from_user
    db_add_or_update_user(user.id, user.full_name, user.username)

    welcome_text = (
        f"Salom, <b>{html.quote(user.full_name)}</b>!\n\n"
        f"💳 <b>Hamyonim</b> botiga xush kelibsiz!\n"
        f"Bu bot orqali siz o'zingizning <b>Uzcard</b> va <b>Humo</b> bank kartalaringizni "
        f"tartibli va xavfsiz holatda saqlashingiz hamda boshqarishingiz mumkin.\n\n"
        f"👇 Kerakli bo'limni tanlash uchun quyidagi menyudan foydalaning:"
    )
    await message.answer(text=welcome_text, reply_markup=get_main_menu(user.id), parse_mode="HTML")
    gc.collect()


@dp.message(F.text == BTN_CANCEL)
@dp.message(Command("cancel"))
async def process_cancel(message: Message, state: FSMContext):
    """Bekor qilish."""
    current_state = await state.get_state()
    await state.clear()
    if current_state is not None:
        await message.answer("🚫 Amaliyot bekor qilindi. Bosh menyuga qaytdingiz.", reply_markup=get_main_menu(message.from_user.id))
    else:
        await message.answer("Siz hozirda hech qanday jarayonda emassiz.", reply_markup=get_main_menu(message.from_user.id))


# ==============================================================================
# 6. KARTA KO'RISH (IMLOIY TO'G'RILANGAN)
# ==============================================================================

@dp.message(F.text == BTN_CARDS)
async def show_my_cards_handler(message: Message, state: FSMContext):
    """Mening kartalarim bo'limi."""
    await state.clear()
    user_id = message.from_user.id
    db_add_or_update_user(user_id, message.from_user.full_name, message.from_user.username)

    cards = db_get_user_cards(user_id)

    if not cards:
        empty_text = (
            "Siz hali karta qo'shmagansiz.\n\n"
            "Karta qo'shish uchun pastdagi <b>«➕ Karta qo'shishni bosing»</b> tugmasini bosing."
        )
        await message.answer(text=empty_text, reply_markup=get_empty_cards_inline(), parse_mode="HTML")
    else:
        await message.answer("📱 <b>Sizning saqlangan kartalaringiz ro'yxati:</b>", parse_mode="HTML")
        for idx, card in enumerate(cards, 1):
            card_id, card_num, card_date, card_type, added_at = card
            formatted_num = f"{card_num[:4]} **** **** {card_num[12:]}" if len(card_num) == 16 else card_num
            card_info = (
                f"<b>{idx}-karta:</b> {card_type}\n"
                f"💳 <b>Raqami:</b> <code>{formatted_num}</code>\n"
                f"📅 <b>Muddati:</b> <code>{card_date}</code>\n"
                f"🕒 <b>Qo'shilgan vaqti:</b> {str(added_at)[:10]}"
            )
            await message.answer(text=card_info, reply_markup=get_card_action_inline(card_id), parse_mode="HTML")
    gc.collect()


# ==============================================================================
# 7. KARTA QO'SHISH (FSM)
# ==============================================================================

@dp.message(F.text == BTN_ADD_CARD)
async def start_add_card(message: Message, state: FSMContext):
    """Karta qo'shish boshlanishi."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    await message.answer("Jarayon boshlandi. Bekor qilish uchun pastdagi tugmani bosing:", reply_markup=get_cancel_keyboard())
    prompt_text = (
        "💳 <b>Qo'shmoqchi bo'lgan karta turingizni tanlang:</b>\n\n"
        "🔒 <i>Eslatib o'tamiz: Botimiz mutlaqo xavfsiz va hech qachon kartangizning PIN-kodini so'ramaydi!</i>"
    )
    await message.answer(text=prompt_text, reply_markup=get_card_type_inline(), parse_mode="HTML")
    await state.set_state(CardState.waiting_for_type)


@dp.callback_query(F.data == "start_add_card_inline")
async def start_add_card_inline(callback: CallbackQuery, state: FSMContext):
    """Inline tugma orqali karta qo'shish."""
    await state.clear()
    await callback.message.answer("Jarayon boshlandi. Bekor qilish uchun pastdagi tugmani bosing:", reply_markup=get_cancel_keyboard())
    prompt_text = "💳 <b>Qo'shmoqchi bo'lgan karta turingizni tanlang:</b>"
    await callback.message.answer(text=prompt_text, reply_markup=get_card_type_inline(), parse_mode="HTML")
    await state.set_state(CardState.waiting_for_type)
    await callback.answer()


@dp.callback_query(CardState.waiting_for_type, F.data.startswith("select_"))
async def process_card_type_selection(callback: CallbackQuery, state: FSMContext):
    """Karta turini tanlash."""
    selected_type = "🔹 Uzcard" if callback.data == "select_uzcard" else "🟠 Humo"
    await state.update_data(card_type=selected_type)

    prompt_text = (
        f"Siz <b>{selected_type}</b> kartasini tanladingiz.\n\n"
        f"📥 Endi 16 xonali <b>{selected_type}</b> karta raqamingizni kiriting:\n"
        f"<i>(Masalan: 8600123456789012)</i>"
    )
    await callback.message.edit_text(text=prompt_text, parse_mode="HTML")
    await state.set_state(CardState.waiting_for_number)
    await callback.answer()


@dp.message(CardState.waiting_for_number)
async def process_card_number_input(message: Message, state: FSMContext):
    """Karta raqamini kiritish."""
    if message.text in ALL_MAIN_BUTTONS:
        await state.clear()
        return

    raw_number = message.text.replace(" ", "").replace("-", "").strip()

    if not raw_number.isdigit() or len(raw_number) != 16:
        error_text = (
            "⚠️ <b>Karta raqami noto'g'ri kiritildi!</b>\n\n"
            "Karta raqami faqat 16 ta raqamdan iborat bo'lishi kerak.\n"
            "Iltimos, qaytadan diqqat bilan kiriting:"
        )
        await message.answer(error_text, parse_mode="HTML")
        return

    await state.update_data(card_number=raw_number)
    date_prompt = (
        "📅 <b>Endi kartangizning amal qilish muddatini kiriting:</b>\n\n"
        "Format: <b>OYo/YIL</b> (Masalan: <b>12/28</b> yoki <b>06/31</b>)"
    )
    await message.answer(date_prompt, parse_mode="HTML")
    await state.set_state(CardState.waiting_for_date)


@dp.message(CardState.waiting_for_date)
async def process_card_date_input(message: Message, state: FSMContext):
    """Karta muddatini kiritish va saqlash."""
    if message.text in ALL_MAIN_BUTTONS:
        await state.clear()
        return

    input_date = message.text.strip()

    if not re.match(r"^(0[1-9]|1[0-2])\/\d{2}$", input_date):
        error_text = (
            "⚠️ <b>Amal qilish muddati noto'g'ri kiritildi!</b>\n\n"
            "Iltimos, kartangiz muddatini <b>MM/YY</b> ko'rinishida kiriting.\n"
            "<i>Misol uchun: 12/28 yoki 05/30</i>"
        )
        await message.answer(error_text, parse_mode="HTML")
        return

    state_data = await state.get_data()
    card_number = state_data.get("card_number")
    card_type = state_data.get("card_type", "🔹 Uzcard")

    if not card_number:
        await state.clear()
        await message.answer("⚠️ Vaqt o'tib ketganligi sababli jarayon to'xtatildi.", reply_markup=get_main_menu(message.from_user.id))
        return

    success = db_add_card(message.from_user.id, card_number, input_date, card_type)
    await state.clear()
    gc.collect()

    if success:
        formatted_num = f"{card_number[:4]} **** **** {card_number[12:]}"
        success_text = (
            f"✅ <b>Sizning kartangiz muvaffaqiyatli saqlandi!</b>\n\n"
            f"💳 <b>Karta turi:</b> {card_type}\n"
            f"🔢 <b>Raqami:</b> <code>{formatted_num}</code>\n"
            f"📅 <b>Muddati:</b> <code>{input_date}</code>"
        )
        await message.answer(text=success_text, reply_markup=get_main_menu(message.from_user.id), parse_mode="HTML")
    else:
        await message.answer("⚠️ Tizimda xatolik yuz berdi. Qaytadan urinib ko'ring.", reply_markup=get_main_menu(message.from_user.id))


@dp.callback_query(F.data.startswith("delete_card_"))
async def process_delete_card(callback: CallbackQuery):
    """Karta o'chirish."""
    card_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    if db_delete_card(card_id, user_id):
        await callback.message.edit_text("🗑 <b>Ushbu karta ro'yxatdan o'chirildi.</b>", parse_mode="HTML")
        await callback.answer("Karta o'chirildi!")
    else:
        await callback.answer("Xatolik yuz berdi!", show_alert=True)


@dp.message(F.text == BTN_HISTORY)
async def show_history_handler(message: Message, state: FSMContext):
    """Tushumlar tarixi."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    history_text = (
        "🧾 <b>Tushumlar va tranzaksiyalar tarixi</b>\n\n"
        "📜 Hozircha sizda hech qanday saqlangan tushumlar tarixi mavjud emas.\n"
        "<i>Kelgusida bu bo'limda kartalaringiz bo'yicha kirim va chiqimlar ko'rinadi.</i>"
    )
    await message.answer(text=history_text, reply_markup=get_main_menu(message.from_user.id), parse_mode="HTML")


@dp.message(F.text == BTN_CONTACT)
async def contact_admin_handler(message: Message, state: FSMContext):
    """Adminga bog'lanish."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    contact_text = (
        "👨‍💻 <b>Qo'llab-quvvatlash xizmati</b>\n\n"
        "Sizda qandaydir taklif, savol yoki muammolar bormi?\n"
        "Loyiha admini bilan bevosita bog'lanishingiz mumkin:\n\n"
        "💬 <b>Telegram Admin:</b> @abduvahob_sakboev"
    )
    contact_inline = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Adminga xabar yozish", url="https://t.me/abduvahob_sakboev")]]
    )
    await message.answer(text=contact_text, reply_markup=contact_inline, parse_mode="HTML")


@dp.message(F.text == BTN_ADMIN)
async def admin_panel_handler(message: Message, state: FSMContext):
    """Admin panel."""
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return

    stats = db_get_total_stats()
    users = db_get_all_users()

    admin_text = (
        f"⚙️ <b>ADMINISTRATOR PANELI</b>\n\n"
        f"👥 <b>Jami foydalanuvchilar:</b> {stats['users_count']} ta\n"
        f"💳 <b>Jami saqlangan kartalar:</b> {stats['cards_count']} ta\n\n"
        f"📌 <b>Oxirgi qo'shilgan foydalanuvchilar:</b>\n"
    )
    for idx, u in enumerate(users[:10], 1):
        u_id, u_name, u_user, u_date = u
        un_text = f"@{u_user}" if u_user != "mavjud_emas" else "Username yo'q"
        admin_text += f"{idx}. {html.quote(u_name)} | {un_text} (ID: <code>{u_id}</code>)\n"

    await message.answer(text=admin_text, reply_markup=get_admin_panel_inline(), parse_mode="HTML")


@dp.callback_query(F.data == "admin_refresh_stats")
async def refresh_admin_stats(callback: CallbackQuery):
    """Admin panel statistikani yangilash."""
    if callback.from_user.id != ADMIN_ID:
        return

    stats = db_get_total_stats()
    users = db_get_all_users()

    admin_text = (
        f"⚙️ <b>ADMINISTRATOR PANELI (YANGILANDI)</b>\n\n"
        f"👥 <b>Jami foydalanuvchilar:</b> {stats['users_count']} ta\n"
        f"💳 <b>Jami saqlangan kartalar:</b> {stats['cards_count']} ta\n\n"
        f"📌 <b>Oxirgi qo'shilgan foydalanuvchilar:</b>\n"
    )
    for idx, u in enumerate(users[:10], 1):
        u_id, u_name, u_user, u_date = u
        un_text = f"@{u_user}" if u_user != "mavjud_emas" else "Username yo'q"
        admin_text += f"{idx}. {html.quote(u_name)} | {un_text} (ID: <code>{u_id}</code>)\n"

    try:
        await callback.message.edit_text(text=admin_text, reply_markup=get_admin_panel_inline(), parse_mode="HTML")
        await callback.answer("Statistika yangilandi!")
    except TelegramBadRequest:
        await callback.answer("Ma'lumotlar eng so'nggi holatda!")


@dp.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    """Xabar yuborish."""
    if callback.from_user.id != ADMIN_ID:
        return

    await callback.message.answer(
        "📢 <b>Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni kiriting:</b>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(AdminBroadcastState.waiting_for_message)
    await callback.answer()


@dp.message(AdminBroadcastState.waiting_for_message)
async def process_broadcast_send(message: Message, state: FSMContext):
    """Xabarni tarqatish."""
    if message.from_user.id != ADMIN_ID:
        return

    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer("📢 Xabar yuborish bekor qilindi.", reply_markup=get_main_menu(ADMIN_ID))
        return

    users = db_get_all_users()
    sent_count = 0
    failed_count = 0

    await message.answer("🚀 Xabar yuborish boshlandi...", reply_markup=get_main_menu(ADMIN_ID))

    for u in users:
        u_id = u[0]
        try:
            await message.copy_to(chat_id=u_id)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed_count += 1

    await state.clear()
    result_text = (
        f"✅ <b>Xabar yuborish yakunlandi!</b>\n\n"
        f"📤 <b>Muvaffaqiyatli yetib bordi:</b> {sent_count} ta\n"
        f"❌ <b>Yetib bormadi:</b> {failed_count} ta"
    )
    await message.answer(result_text, parse_mode="HTML")


@dp.message()
async def echo_unknown_message(message: Message, state: FSMContext):
    """Tushunarsiz xabarlar."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await message.answer("Tushunarsiz buyruq kiritildi. Iltimos, menyudan foydalaning:", reply_markup=get_main_menu(message.from_user.id))


async def main():
    """Botni ishga tushirish."""
    logger.info("HAMYONIM BOT ISHGA TUSHMOQDA...")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi!")
