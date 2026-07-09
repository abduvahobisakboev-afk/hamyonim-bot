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

BOT_TOKEN = "8888847127:AAG4D9TC2tPwuXHJ_Cp-xKIzmsnF9OVcfXs"
ADMIN_ID = 1673990832
DB_PATH = "bank_bot.db"

# Console uchun logging formatini sozlash
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - (%(filename)s:%(lineno)d) - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("HamyonimBot")

# Bot va Dispatcher obyektlarini yaratish
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ==============================================================================
# 2. MA'LUMOTLAR BAZASI BILAN ISHLASH (SQLITE3 OPTIMIZED)
# ==============================================================================

def get_db_connection() -> Tuple[sqlite3.Connection, sqlite3.Cursor]:
    """
    SQLite ma'lumotlar bazasi bilan tezkor va xavfsiz ulanish o'rnatadi.
    RAM iste'molini kamaytirish uchun WAL rejimidan foydalaniladi.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA cache_size=-2000;")  # 2MB cache cheklovi
    return conn, cursor


def init_db() -> None:
    """
    Baza mavjudligini va kerakli jadvallar (users, my_cards, transactions)
    yaratilganini avtomatik ravishda tekshiradi.
    """
    try:
        conn, cursor = get_db_connection()
        
        # 1. Foydalanuvchilar jadvali
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            username TEXT,
            status TEXT DEFAULT 'active',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 2. Saqlangan kartalar jadvali
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS my_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_number TEXT NOT NULL,
            card_date TEXT NOT NULL,
            card_type TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
        """)

        # 3. Tushumlar va tranzaksiyalar tarixi jadvali
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

        conn.commit()
        conn.close()
        logger.info("Ma'lumotlar bazasi va barcha jadvallar muvaffaqiyatli tekshirildi.")
    except Exception as e:
        logger.error(f"Ma'lumotlar bazasini ishga tushirishda qat'iy xatolik: {e}")


# Baza jadvallarini dastur boshlanishida yaratish
init_db()


def db_add_or_update_user(user_id: int, full_name: str, username: Optional[str]) -> bool:
    """Foydalanuvchini bazaga qo'shadi yoki ma'lumotlarini yangilaydi."""
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
        logger.error(f"Foydalanuvchi bazaga saqlashda xatolik: {e}")
        return False


def db_add_card(user_id: int, card_number: str, card_date: str, card_type: str) -> bool:
    """Yangi kartani my_cards jadvaliga qo'shadi."""
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
        logger.error(f"Karta qo'shishda xatolik yuz berdi: {e}")
        return False


def db_get_user_cards(user_id: int) -> List[Tuple[Any, ...]]:
    """Foydalanuvchiga tegishli barcha kartalar ro'yxatini qaytaradi."""
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
        logger.error(f"Kartalarni bazadan olishda xatolik: {e}")
        return []


def db_delete_card(card_id: int, user_id: int) -> bool:
    """Foydalanuvchining ma'lum kartasini o'chiradi."""
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
    """Admin panel uchun barcha foydalanuvchilar ro'yxatini qaytaradi."""
    try:
        conn, cursor = get_db_connection()
        cursor.execute("SELECT user_id, full_name, username, joined_at FROM users ORDER BY joined_at DESC")
        users = cursor.fetchall()
        conn.close()
        return users
    except Exception as e:
        logger.error(f"Foydalanuvchilar ro'yxatini olishda xatolik: {e}")
        return []


def db_get_total_stats() -> Dict[str, int]:
    """Tizimdagi umumiy statistikani hisoblaydi."""
    stats = {"users_count": 0, "cards_count": 0}
    try:
        conn, cursor = get_db_connection()
        cursor.execute("SELECT COUNT(*) FROM users")
        stats["users_count"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM my_cards")
        stats["cards_count"] = cursor.fetchone()[0]
        conn.close()
    except Exception as e:
        logger.error(f"Statistikani hisoblashda xatolik: {e}")
    return stats


# ==============================================================================
# 3. FSM (FORM STATE MANAGEMENT - HOLATLAR)
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
    """Asosiy menyu tugmalarini shakllantiradi."""
    keyboard_layout = [
        [KeyboardButton(text=BTN_CARDS)],
        [KeyboardButton(text=BTN_ADD_CARD), KeyboardButton(text=BTN_HISTORY)],
        [KeyboardButton(text=BTN_CONTACT)]
    ]
    
    if user_id == ADMIN_ID:
        keyboard_layout.append([KeyboardButton(text=BTN_ADMIN)])
        
    return ReplyKeyboardMarkup(keyboard=keyboard_layout, resize_keyboard=True)


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Bekor qilish tugmasini ko'rsatish."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True
    )


def get_card_type_inline() -> InlineKeyboardMarkup:
    """Karta turini tanlash uchun Inline tugmalar."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔹 Uzcard", callback_data="select_uzcard"),
                InlineKeyboardButton(text="🟠 Humo", callback_data="select_humo")
            ]
        ]
    )


def get_empty_cards_inline() -> InlineKeyboardMarkup:
    """
    Karta mavjud bo'lmaganda chiqadigan tugma.
    Imloiy jihatdan to'liq va xatosiz yozilgan.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Karta qo'shishni bosing", callback_data="start_add_card_inline")]
        ]
    )


def get_card_action_inline(card_id: int) -> InlineKeyboardMarkup:
    """Karta ostida o'chirish tugmasini ko'rsatish."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Ushbu kartani o'chirish", callback_data=f"delete_card_{card_id}")]
        ]
    )


def get_admin_panel_inline() -> InlineKeyboardMarkup:
    """Admin panel uchun harakatlar tugmasi."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Barcha foydalanuvchilarga xabar yuborish", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔄 Statistikani yangilash", callback_data="admin_refresh_stats")]
        ]
    )


# ==============================================================================
# 5. ASOSIY HANDLERLAR (BOT LOGIKASI)
# ==============================================================================

@dp.message(CommandStart())
async def cmd_start_handler(message: Message, state: FSMContext):
    """
    /start buyrug'i uchun javob beruvchi handler.
    """
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
    
    await message.answer(
        text=welcome_text,
        reply_markup=get_main_menu(user.id),
        parse_mode="HTML"
    )
    gc.collect()


@dp.message(F.text == BTN_CANCEL)
@dp.message(Command("cancel"))
async def process_cancel(message: Message, state: FSMContext):
    """Jarayonlarni bekor qilish handleri."""
    current_state = await state.get_state()
    await state.clear()
    
    if current_state is not None:
        await message.answer(
            "🚫 Amaliyot bekor qilindi. Bosh menyuga qaytdingiz.",
            reply_markup=get_main_menu(message.from_user.id)
        )
    else:
        await message.answer(
            "Siz hozirda hech qanday jarayonda emassiz.",
            reply_markup=get_main_menu(message.from_user.id)
        )


# ==============================================================================
# 6. KARTA KO'RISH VA MAVJUD BO'LMAGANDA CHIQADIGAN MANTIQ
# ==============================================================================

@dp.message(F.text == BTN_CARDS)
async def show_my_cards_handler(message: Message, state: FSMContext):
    """
    Foydalanuvchi 'Mening kartalarim' tugmasini bosganda ishlaydi.
    Agar karta bo'lmasa, imloiy to'g'ri va aniq xabar chiqariladi.
    """
    await state.clear()
    user_id = message.from_user.id
    db_add_or_update_user(user_id, message.from_user.full_name, message.from_user.username)

    cards = db_get_user_cards(user_id)

    if not cards:
        # IMLOIY TO'G'RILANGAN MATN:
        empty_text = (
            "Siz hali karta qo'shmagansiz.\n\n"
            "Karta qo'shish uchun pastdagi <b>«➕ Karta qo'shishni bosing»</b> tugmasini bosing."
        )
        await message.answer(
            text=empty_text,
            reply_markup=get_empty_cards_inline(),
            parse_mode="HTML"
        )
    else:
        await message.answer("📱 <b>Sizning saqlangan kartalaringiz ro'yxati:</b>", parse_mode="HTML")
        
        for idx, card in enumerate(cards, 1):
            card_id, card_num, card_date, card_type, added_at = card
            
            # Karta raqamini qisman yashirish (Xavfsizlik)
            formatted_num = f"{card_num[:4]} **** **** {card_num[12:]}" if len(card_num) == 16 else card_num
            
            card_info = (
                f"<b>{idx}-karta:</b> {card_type}\n"
                f"💳 <b>Raqami:</b> <code>{formatted_num}</code>\n"
                f"📅 <b>Muddati:</b> <code>{card_date}</code>\n"
                f"🕒 <b>Qo'shilgan vaqti:</b> {added_at[:10]}"
            )
            
            await message.answer(
                text=card_info,
                reply_markup=get_card_action_inline(card_id),
                parse_mode="HTML"
            )
            
    gc.collect()


# ==============================================================================
# 7. KARTA QO'SHISH BOSQICHMA-BOSQICH (FSM)
# ==============================================================================

@dp.message(F.text == BTN_ADD_CARD)
async def start_add_card(message: Message, state: FSMContext):
    """Karta qo'shish jarayonini boshlash."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    await message.answer(
        "Jarayon boshlandi. Bekor qilish uchun pastdagi tugmani bosing:",
        reply_markup=get_cancel_keyboard()
    )

    prompt_text = (
        "💳 <b>Qo'shmoqchi bo'lgan karta turingizni tanlang:</b>\n\n"
        "🔒 <i>Eslatib o'tamiz: Botimiz mutlaqo xavfsiz va hechn qachon kartangizning PIN-kodini so'ramaydi!</i>"
    )
    
    await message.answer(
        text=prompt_text,
        reply_markup=get_card_type_inline(),
        parse_mode="HTML"
    )
    await state.set_state(CardState.waiting_for_type)


@dp.callback_query(F.data == "start_add_card_inline")
async def start_add_card_inline(callback: CallbackQuery, state: FSMContext):
    """Inline tugma orqali karta qo'shishni boshlash."""
    await state.clear()
    await callback.message.answer(
        "Jarayon boshlandi. Bekor qilish uchun pastdagi tugmani bosing:",
        reply_markup=get_cancel_keyboard()
    )

    prompt_text = "💳 <b>Qo'shmoqchi bo'lgan karta turingizni tanlang:</b>"
    await callback.message.answer(
        text=prompt_text,
        reply_markup=get_card_type_inline(),
        parse_mode="HTML"
    )
    await state.set_state(CardState.waiting_for_type)
    await callback.answer()


@dp.callback_query(CardState.waiting_for_type, F.data.startswith("select_"))
async def process_card_type_selection(callback: CallbackQuery, state: FSMContext):
    """Karta turi tanlangandan so'ng raqam so'rash."""
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
    """Karta raqami kiritilganda uni tekshirish va saqlash."""
    if message.text in ALL_MAIN_BUTTONS:
        await state.clear()
        return

    # Probellarni va chiziqchalarni olib tashlash
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
    """Karta muddatini tekshirish va bazaga saqlash."""
    if message.text in ALL_MAIN_BUTTONS:
        await state.clear()
        return

    input_date = message.text.strip()

    # MM/YY formatini regex bilan tekshirish
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
        await message.answer(
            "⚠️ Vaqt o'tib ketganligi sababli jarayon to'xtatildi. Qaytadan urinib ko'ring.",
            reply_markup=get_main_menu(message.from_user.id)
        )
        return

    # Bazaga qo'shish
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
        await message.answer(
            text=success_text,
            reply_markup=get_main_menu(message.from_user.id),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "⚠️ Tizimda xatolik yuz berdi. Qaytadan urinib ko'ring.",
            reply_markup=get_main_menu(message.from_user.id)
        )


# ==============================================================================
# 8. KARTANI O'CHIRISH HANDLERI
# ==============================================================================

@dp.callback_query(F.data.startswith("delete_card_"))
async def process_delete_card(callback: CallbackQuery):
    """Karta o'chirish tugmasi bosilganda ishlaydi."""
    card_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    success = db_delete_card(card_id, user_id)

    if success:
        await callback.message.edit_text("🗑 <b>Ushbu karta ro'yxatdan o'chirildi.</b>", parse_mode="HTML")
        await callback.answer("Karta muvaffaqiyatli o'chirildi!")
    else:
        await callback.answer("Xatolik: Kartani o'chirib bo'lmadi!", show_alert=True)


# ==============================================================================
# 9. TUSHUMLAR TARIXI VA ADMINGA BOG'LANISH
# ==============================================================================

@dp.message(F.text == BTN_HISTORY)
async def show_history_handler(message: Message, state: FSMContext):
    """Tushumlar va SMS tranzaksiyalari bo'limi."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    history_text = (
        "🧾 <b>Tushumlar va tranzaksiyalar tarixi</b>\n\n"
        "📜 Hozircha sizda hech qanday saqlangan tushumlar tarixi mavjud emas.\n"
        "<i>Kelgusida bu bo'limda kartalaringiz bo'yicha kirim va chiqimlar ko'rinadi.</i>"
    )
    
    await message.answer(
        text=history_text,
        reply_markup=get_main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@dp.message(F.text == BTN_CONTACT)
async def contact_admin_handler(message: Message, state: FSMContext):
    """Admin bilan bog'lanish bo'limi."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    contact_text = (
        "👨‍💻 <b>Qo'llab-quvvatlash xizmati</b>\n\n"
        "Sizda qandaydir taklif, savol yoki muammolar bormi?\n"
        "Loyiha admini bilan bevosita bog'lanishingiz mumkin:\n\n"
        "💬 <b>Telegram Admin:</b> @abduvahob_sakboev"
    )

    contact_inline = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Adminga xabar yozish", url="https://t.me/abduvahob_sakboev")]
        ]
    )

    await message.answer(
        text=contact_text,
        reply_markup=contact_inline,
        parse_mode="HTML"
    )


# ==============================================================================
# 10. ADMIN PANEL VA BOSHGARUV MANTIQI
# ==============================================================================

@dp.message(F.text == BTN_ADMIN)
async def admin_panel_handler(message: Message, state: FSMContext):
    """Admin panelni ochish."""
    await state.clear()
    user_id = message.from_user.id

    if user_id != ADMIN_ID:
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

    await message.answer(
        text=admin_text,
        reply_markup=get_admin_panel_inline(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_refresh_stats")
async def refresh_admin_stats(callback: CallbackQuery):
    """Admin paneldagi statistikani yangilash."""
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
        await callback.answer("Ma'lumotlar allaqachon eng so'nggi holatda!")


@dp.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    """Barcha foydalanuvchilarga xabar yuborishni boshlash."""
    if callback.from_user.id != ADMIN_ID:
        return

    await callback.message.answer(
        "📢 <b>Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni kiriting:</b>\n\n"
        "<i>Jarayonni bekor qilish uchun '❌ Bekor qilish' tugmasini bosing.</i>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(AdminBroadcastState.waiting_for_message)
    await callback.answer()


@dp.message(AdminBroadcastState.waiting_for_message)
async def process_broadcast_send(message: Message, state: FSMContext):
    """Xabarni barcha foydalanuvchilarga tarqatish."""
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
            await asyncio.sleep(0.05)  # Telegram limitlariga tushmaslik uchun
        except (TelegramForbiddenError, TelegramBadRequest):
            failed_count += 1
        except Exception as e:
            logger.error(f"Xabar yuborishda xato ({u_id}): {e}")
            failed_count += 1

    await state.clear()
    result_text = (
        f"✅ <b>Xabar yuborish yakunlandi!</b>\n\n"
        f"📤 <b>Muvaffaqiyatli yetib bordi:</b> {sent_count} ta\n"
        f"❌ <b>Yetib bormadi (Botni bloklagan):</b> {failed_count} ta"
    )
    await message.answer(result_text, parse_mode="HTML")


# ==============================================================================
# 11. NOTO'G'RI BUYRUQLARNI USHLASH VA DASTURNI ISHGA TUSHIRISH
# ==============================================================================

@dp.message()
async def echo_unknown_message(message: Message, state: FSMContext):
    """Tushunarsiz buyruqlar yoki textlar kelganda javob qaytaradi."""
    await state.clear()
    db_add_or_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    await message.answer(
        "Tushunarsiz buyruq kiritildi. Iltimos, quyidagi menyudan foydalaning:",
        reply_markup=get_main_menu(message.from_user.id)
    )


async def main():
    """
    Botni ishga tushirish funksiyasi.
    """
    logger.info("="*50)
    logger.info("HAMYONIM BOT ISHGA TUSHMOQDA...")
    logger.info("="*50)

    # Eski kutilayotgan update'larni o'chirib tashlash (Conflict hosil qilmaslik uchun)
    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot majburiy to'xtatildi!")
