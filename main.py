import logging
import sqlite3
import re
import asyncio
import gc  # RAM'ni avtomatik va majburiy tozalash uchun
from aiogram import Bot, Dispatcher, F, html
from aiogram.types import (
    Message, 
    CallbackQuery, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import CommandStart, Command

# ==========================================
# 1. SOZLAMALAR
# ==========================================

BOT_TOKEN = "8888847127:AAHJwLGdphr3JLEaGMreFAuCnNCQ1Zlp_LU"
ADMIN_ID = 1673990832  # Sizning Telegram ID raqamingiz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ==========================================
# 2. MA'LUMOTLAR BAZASI (RAM OPTIMIZATION)
# ==========================================

conn = sqlite3.connect("bank_bot.db", check_same_thread=False)
cursor = conn.cursor()

# RAM'ga tushadigan yukni keskin kamaytirish uchun baza sozlamalari
cursor.execute("PRAGMA journal_mode=WAL;")
cursor.execute("PRAGMA synchronous=NORMAL;")
cursor.execute("PRAGMA cache_size=-2000;") # Maksimal 2MB RAM keshlash
conn.commit()

def init_db():
    """Baza jadvallarini yaratish va tekshirish"""
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT NOT NULL,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS my_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        card_number TEXT NOT NULL,
        card_date TEXT NOT NULL,
        card_type TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """)
    conn.commit()

init_db()

def db_update_user(user_id: int, full_name: str, username: str):
    """Foydalanuvchini bazaga qo'shish yoki yangilash"""
    uname = username if username else "mavjud_emas"
    cursor.execute("""
        INSERT INTO users (user_id, full_name, username) 
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            full_name = excluded.full_name,
            username = excluded.username
    """, (user_id, full_name, uname))
    conn.commit()

def db_add_card(user_id: int, card_number: str, card_date: str, card_type: str) -> bool:
    """Yangi kartani saqlash"""
    try:
        cursor.execute("""
            INSERT INTO my_cards (user_id, card_number, card_date, card_type) 
            VALUES (?, ?, ?, ?)
        """, (user_id, str(card_number), str(card_date), str(card_type)))
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"Karta saqlashda xatolik: {e}")
        return False

def db_get_user_cards(user_id: int):
    """Foydalanuvchi kartalarini olish"""
    cursor.execute("""
        SELECT card_number, card_date, card_type 
        FROM my_cards 
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,))
    return cursor.fetchall()

def db_get_all_users():
    """Admin uchun barcha foydalanuvchilar"""
    cursor.execute("SELECT user_id, full_name, username FROM users")
    return cursor.fetchall()

def db_get_users_count() -> int:
    """Jami foydalanuvchilar soni"""
    cursor.execute("SELECT COUNT(*) FROM users")
    result = cursor.fetchone()
    return result[0] if result else 0

# ==========================================
# 3. FSM (HOLATLAR)
# ==========================================

class CardState(StatesGroup):
    waiting_for_type = State()
    waiting_for_number = State()
    waiting_for_date = State()

# ==========================================
# 4. TUGMALAR VA MENYULAR
# ==========================================

MAIN_MENU_TEXTS = [
    "💳 Mening Kartalarim", 
    "➕ Yangi karta qo'shish", 
    "🧾 Tushumlar Tarixi", 
    "👨‍💻 Adminga bog'lanish", 
    "⚙️ Admin Panel"
]

def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="💳 Mening Kartalarim")],
        [KeyboardButton(text="➕ Yangi karta qo'shish"), KeyboardButton(text="🧾 Tushumlar Tarixi")],
        [KeyboardButton(text="👨‍💻 Adminga bog'lanish")]
    ]
    if user_id == ADMIN_ID:
        buttons.append([KeyboardButton(text="⚙️ Admin Panel")])
        
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True
    )

def get_card_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔹 Uzcard", callback_data="type_uzcard"),
                InlineKeyboardButton(text="🟠 Humo", callback_data="type_humo")
            ]
        ]
    )

def get_add_card_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Yangi karta qo'shish", callback_data="start_add_card")]
        ]
    )

# ==========================================
# 5. HANDLERLAR
# ==========================================

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    db_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    welcome_text = (
        f"Salom, <b>{html.quote(message.from_user.full_name)}</b>!\n\n"
        "💳 Barcha <b>Uzcard</b> va <b>Humo</b> kartalaringizni xavfsiz boshqarish botiga xush kelibsiz.\n\n"
        "Quyidagi menyulardan birini tanlang:"
    )
    await message.answer(
        welcome_text,
        reply_markup=get_main_menu(message.from_user.id),
        parse_mode="HTML"
    )

@dp.message(F.text == "❌ Bekor qilish")
@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🚫 Jarayon bekor qilindi. Bosh menyuga qaytdingiz.",
        reply_markup=get_main_menu(message.from_user.id)
    )

@dp.message(F.text == "➕ Yangi karta qo'shish")
async def add_card_start(message: Message, state: FSMContext):
    await state.clear()
    db_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    await message.answer("Jarayon boshlandi. Bekor qilish uchun pastdagi tugmani bosing:", reply_markup=get_cancel_menu())
    
    text = (
        "💳 <b>Qo'shmoqchi bo'lgan karta turingizni tanlang:</b>\n\n"
        "🔒 <i>Xavfsizlik eslatmasi: Bot hech qachon kartangiz PIN-kodini yoki SMS kodingizni so'ramaydi!</i>"
    )
    await message.answer(text, reply_markup=get_card_type_keyboard(), parse_mode="HTML")
    await state.set_state(CardState.waiting_for_type)

@dp.callback_query(F.data == "start_add_card")
async def inline_add_card_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Jarayon boshlandi. Bekor qilish uchun pastdagi tugmani bosing:", reply_markup=get_cancel_menu())
    
    text = "💳 <b>Qo'shmoqchi bo'lgan karta turingizni tanlang:</b>"
    await callback.message.answer(text, reply_markup=get_card_type_keyboard(), parse_mode="HTML")
    await state.set_state(CardState.waiting_for_type)
    await callback.answer()

@dp.callback_query(CardState.waiting_for_type, F.data.startswith("type_"))
async def process_card_type(callback: CallbackQuery, state: FSMContext):
    card_type = "🔹 Uzcard" if callback.data == "type_uzcard" else "🟠 Humo"
    await state.update_data(card_type=card_type)
    
    text = (
        f"Siz <b>{card_type}</b> kartasini tanladingiz.\n\n"
        f"📥 Endi 16 xonali <b>{card_type}</b> karta raqamingizni kiriting:"
    )
    await callback.message.edit_text(text, parse_mode="HTML")
    await state.set_state(CardState.waiting_for_number)
    await callback.answer()

@dp.message(CardState.waiting_for_number)
async def process_card_number(message: Message, state: FSMContext):
    if message.text in MAIN_MENU_TEXTS:
        await state.clear()
        return

    card_num = message.text.replace(" ", "").replace("-", "").strip()
    
    if not card_num.isdigit() or len(card_num) != 16:
        await message.answer(
            "⚠️ <b>Xatolik!</b> Karta raqami 16 ta raqamdan iborat bo'lishi kerak.\n"
            "Iltimos, karta raqamini to'g'ri kiriting:",
            parse_mode="HTML"
        )
        return

    await state.update_data(card_number=card_num)
    await message.answer("📅 Endi kartaning amal qilish muddatini kiriting (Masalan: <b>12/28</b> yoki <b>06/31</b>):", parse_mode="HTML")
    await state.set_state(CardState.waiting_for_date)

@dp.message(CardState.waiting_for_date)
async def process_card_date(message: Message, state: FSMContext):
    if message.text in MAIN_MENU_TEXTS:
        await state.clear()
        return

    card_date = message.text.strip()
    
    if not re.match(r"^(0[1-9]|1[0-2])\/\d{2}$", card_date):
        await message.answer(
            "⚠️ <b>Xato format!</b> Muddati noto'g'ri kiritildi.\n"
            "Iltimos, <b>MM/YY</b> ko'rinishida kiriting (Masalan: 12/28):",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    card_number = data.get('card_number')
    card_type = data.get('card_type', '🔹 Uzcard')

    if not card_number:
        await state.clear()
        await message.answer("⚠️ Sessiya vaqti tugadi. Qaytadan karta qo'shing.", reply_markup=get_main_menu(message.from_user.id))
        return

    success = db_add_card(message.from_user.id, card_number, card_date, card_type)
    
    # Holat va RAM xotirasini darhol tozalash
    await state.clear()
    gc.collect()

    if success:
        await message.answer(
            f"✅ <b>{card_type}</b> kartangiz muvaffaqiyatli saqlandi!", 
            reply_markup=get_main_menu(message.from_user.id), 
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "⚠️ Tizimda xatolik yuz berdi. Qaytadan urinib ko'ring.", 
            reply_markup=get_main_menu(message.from_user.id)
        )

@dp.message(F.text == "💳 Mening Kartalarim")
async def show_cards(message: Message, state: FSMContext):
    await state.clear()
    db_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    cards = db_get_user_cards(message.from_user.id)
    
    if cards:
        msg = "📱 <b>Sizning ulangan kartalaringiz:</b>\n\n"
        for idx, card in enumerate(cards, 1):
            c_num = str(card[0])
            formatted_num = f"{c_num[:4]} **** **** {c_num[12:]}" if len(c_num) == 16 else c_num
            msg += f"{idx}. <b>{card[2]}</b>\n💳 Karta: <code>{formatted_num}</code>\n📅 Muddati: <b>{card[1]}</b>\n\n"
        
        await message.answer(msg, parse_mode="HTML", reply_markup=get_main_menu(message.from_user.id))
    else:
        await message.answer(
            "❌ <b>Siz hali birorta ham karta kiritmagansiz!</b>\n\n"
            "Karta qo'shish uchun pastdagi tugmani bosing:",
            reply_markup=get_add_card_inline_keyboard(),
            parse_mode="HTML"
        )
    gc.collect()

@dp.message(F.text == "🧾 Tushumlar Tarixi")
async def show_history(message: Message, state: FSMContext):
    await state.clear()
    db_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    await message.answer(
        "📂 <b>Tushumlar va SMS-bildirishnomalar tarixi bo'sh.</b>\n\n"
        "<i>Hozircha hech qanday tranzaksiya amalga oshirilmadi.</i>",
        reply_markup=get_main_menu(message.from_user.id),
        parse_mode="HTML"
    )

@dp.message(F.text == "👨‍💻 Adminga bog'lanish")
async def contact_admin(message: Message, state: FSMContext):
    await state.clear()
    db_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    admin_info = "👨‍💻 <b>Admin bilan bog'lanish:</b>\n\n"
    admin_info += "Savollar, takliflar yoki muammolar bo'lsa adminga murojaat qilishingiz mumkin:\n"
    admin_info += "💬 Telegram: @abduvahob_sakboev"

    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Adminga yozish", url="https://t.me/abduvahob_sakboev")]
        ]
    )
    await message.answer(admin_info, reply_markup=inline_kb, parse_mode="HTML")

@dp.message(F.text == "⚙️ Admin Panel")
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    db_update_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    if message.from_user.id != ADMIN_ID:
        return
    
    total_users = db_get_users_count()
    users = db_get_all_users()
    
    panel_text = f"⚙️ <b>ADMIN PANEL</b>\n\n👥 <b>Jami foydalanuvchilar soni:</b> {total_users} ta\n\n"
    panel_text += "<b>Foydalanuvchilar ro'yxati:</b>\n"
    
    for idx, u in enumerate(users, 1):
        uname = f"@{u[2]}" if u[2] != "mavjud_emas" else "Username yo'q"
        panel_text += f"{idx}. {html.quote(u[1])} | {uname} (ID: <code>{u[0]}</code>)\n"
        
    await message.answer(panel_text, parse_mode="HTML", reply_markup=get_main_menu(message.from_user.id))
    gc.collect()

# ==========================================
# 6. ISHGA TUSHIRISH
# ==========================================

async def main():
    logging.info("Bot yengil va tezkor rejimda ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot to'xtatildi!")
