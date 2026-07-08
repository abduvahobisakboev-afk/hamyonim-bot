import logging
import sqlite3
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
from aiogram.filters import CommandStart, Command

# ----------------- SOZLAMALAR -----------------
BOT_TOKEN = "8888847127:AAHJwLGdphr3JLEaGMreFAuCnNCQ1Zlp_LU"
ADMIN_ID = 1673990832  # Sizning Telegram ID raqamingiz

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ----------------- BAZA BILAN ISHLASH -----------------
conn = sqlite3.connect("bank_bot.db")
cursor = conn.cursor()

# Foydalanuvchilar jadvali
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    full_name TEXT,
    username TEXT
)
""")

# Kartalar jadvali
cursor.execute("""
CREATE TABLE IF NOT EXISTS my_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    card_number TEXT,
    card_date TEXT,
    card_type TEXT
)
""")
conn.commit()

# ----------------- USERNAME'NI AVTO-YANGILASH -----------------
def update_user_info(user_id: int, full_name: str, username: str):
    uname = username if username else "mavjud_emas"
    cursor.execute("""
        INSERT INTO users (user_id, full_name, username) 
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            full_name = excluded.full_name,
            username = excluded.username
    """, (user_id, full_name, uname))
    conn.commit()

# ----------------- STATES (HOLATLAR) -----------------
class CardState(StatesGroup):
    waiting_for_type = State()
    waiting_for_number = State()
    waiting_for_date = State()

# ----------------- MENYULAR -----------------
def get_main_menu(user_id: int):
    buttons = [
        [KeyboardButton(text="💳 Mening Kartalarim")],
        [KeyboardButton(text="➕ Yangi karta qo'shish"), KeyboardButton(text="🧾 Tushumlar Tarixi")],
        [KeyboardButton(text="👨‍💻 Adminga bog'lanish")]
    ]
    if user_id == ADMIN_ID:
        buttons.append([KeyboardButton(text="⚙️ Admin Panel")])
        
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_cancel_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True
    )

def get_card_type_keyboard():
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔹 Uzcard", callback_data="type_uzcard"),
                InlineKeyboardButton(text="🟠 Humo", callback_data="type_humo")
            ]
        ]
    )
    return inline_kb

# ----------------- BEKOR QILISH HANDLERI -----------------
@dp.message(F.text == "❌ Bekor qilish")
@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
    
    await message.answer(
        "🚫 Karta qo'shish bekor qilindi.",
        reply_markup=get_main_menu(message.from_user.id)
    )

# ----------------- HANDLERLAR -----------------
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    update_user_info(message.from_user.id, message.from_user.full_name, message.from_user.username)

    await message.answer(
        f"Salom, {html.bold(message.from_user.full_name)}!\n\n"
        "💳 Barcha **Uzcard** va **Humo** kartalaringizni xavfsiz boshqarish botiga xush kelibsiz.",
        reply_markup=get_main_menu(message.from_user.id),
        parse_mode="HTML"
    )

# --- 1-QADAM: Karta turini tanlash ---
@dp.message(F.text == "➕ Yangi karta qo'shish")
async def add_card_start(message: Message, state: FSMContext):
    update_user_info(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    # Menyunialmashtirib, faqat Bekor qilish tugmasini ko'rsatamiz
    await message.answer("Jarayon boshlandi. Bekor qilish uchun tugmani bosing:", reply_markup=get_cancel_menu())
    
    msg = (
        "💳 **Qo'shmoqchi bo'lgan karta turingizni tanlang:**\n\n"
        "🔒 *Xavfsizlik eslatmasi: Bot hech qachon kartangiz PIN-kodini yoki SMS kodingizni so'ramaydi!*"
    )
    await message.answer(msg, reply_markup=get_card_type_keyboard(), parse_mode="Markdown")
    await state.set_state(CardState.waiting_for_type)

# --- 2-QADAM: Tugma bosilganda Karta raqamini so'rash ---
@dp.callback_query(CardState.waiting_for_type, F.data.startswith("type_"))
async def process_card_type(callback: CallbackQuery, state: FSMContext):
    card_type = "🔹 Uzcard" if callback.data == "type_uzcard" else "🟠 Humo"
    await state.update_data(card_type=card_type)
    
    await callback.message.edit_text(
        f"Siz **{card_type}** kartasini tanladingiz.\n\n"
        f"📥 Endi 16 xonali **{card_type}** karta raqamingizni kiriting:",
        parse_mode="Markdown"
    )
    await state.set_state(CardState.waiting_for_number)
    await callback.answer()

# --- 3-QADAM: Karta raqamini tekshirish ---
@dp.message(CardState.waiting_for_number)
async def process_card_number(message: Message, state: FSMContext):
    card_num = message.text.replace(" ", "").strip()
    data = await state.get_data()
    selected_type = data.get("card_type")
    
    if not card_num.isdigit() or len(card_num) != 16:
        await message.answer("⚠️ Xato! Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qaytadan kiriting (yoki Bekor qilishni bosing):")
        return
    
    # Karta turiga mosligini tekshirish
    if selected_type == "🔹 Uzcard" and not (card_num.startswith("8600") or card_num.startswith("5614")):
        await message.answer("⚠️ Bu Uzcard karta raqamiga o'xshamaydi (Uzcard raqamlari 8600 bilan boshlanadi). Qaytadan kiriting:")
        return
    elif selected_type == "🟠 Humo" and not card_num.startswith("9860"):
        await message.answer("⚠️ Bu Humo karta raqamiga o'xshamaydi (Humo raqamlari 9860 bilan boshlanadi). Qaytadan kiriting:")
        return

    await state.update_data(card_number=card_num)
    await message.answer("📅 Endi kartaning amal qilish muddatini kiriting (Masalan: 12/28):")
    await state.set_state(CardState.waiting_for_date)

# --- 4-QADAM: Amal qilish muddatini saqlash ---
@dp.message(CardState.waiting_for_date)
async def process_card_date(message: Message, state: FSMContext):
    card_date = message.text.strip()
    if len(card_date) != 5 or "/" not in card_date:
        await message.answer("⚠️ Xato! Muddatni to'g'ri formatda kiriting (Masalan: 12/28):")
        return
        
    data = await state.get_data()
    card_number = data['card_number']
    card_type = data['card_type']
    
    cursor.execute("INSERT INTO my_cards (user_id, card_number, card_date, card_type) VALUES (?, ?, ?, ?)", 
                   (message.from_user.id, card_number, card_date, card_type))
    conn.commit()
    
    await state.clear()
    await message.answer(
        f"✅ **{card_type}** kartangiz muvaffaqiyatli saqlandi!", 
        reply_markup=get_main_menu(message.from_user.id), 
        parse_mode="Markdown"
    )

# ----------------- KARTALARNI KO'RISH -----------------
@dp.message(F.text == "💳 Mening Kartalarim")
async def show_cards(message: Message):
    update_user_info(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    cursor.execute("SELECT card_number, card_date, card_type FROM my_cards WHERE user_id = ?", (message.from_user.id,))
    cards = cursor.fetchall()
    
    if cards:
        msg = "📱 <b>Sizning ulangan kartalaringiz:</b>\n\n"
        for idx, card in enumerate(cards, 1):
            c_num = card[0]
            formatted_num = f"{c_num[:4]} **** **** {c_num[12:]}"
            msg += f"{idx}. {card[2]}\n💳 Karta: <code>{formatted_num}</code>\n📅 Muddati: {card[1]}\n\n"
        await message.answer(msg, parse_mode="HTML")
    else:
        add_btn = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Karta qo'shish", callback_data="start_add_card")]
            ]
        )
        await message.answer(
            "❌ <b>Siz hali birorta ham karta kiritmagansiz!</b>\n\n"
            "Karta qo'shish uchun pastdagi <b>'➕ Karta qo'shish'</b> tugmasini bosing:",
            reply_markup=add_btn,
            parse_mode="HTML"
        )

# Inline "➕ Karta qo'shish" tugmasi bosilganda
@dp.callback_query(F.data == "start_add_card")
async def inline_add_card(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Jarayon boshlandi. Bekor qilish uchun tugmani bosing:", reply_markup=get_cancel_menu())
    msg = (
        "💳 **Qo'shmoqchi bo'lgan karta turingizni tanlang:**\n\n"
        "🔒 *Xavfsizlik eslatmasi: Bot hech qachon kartangiz PIN-kodini yoki SMS kodingizni so'ramaydi!*"
    )
    await callback.message.answer(msg, reply_markup=get_card_type_keyboard(), parse_mode="Markdown")
    await state.set_state(CardState.waiting_for_type)
    await callback.answer()

@dp.message(F.text == "🧾 Tushumlar Tarixi")
async def show_history(message: Message):
    update_user_info(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await message.answer("📂 Tushumlar va sms-bildirishnomalar tarixi bo'sh.")

@dp.message(F.text == "👨‍💻 Adminga bog'lanish")
async def contact_admin(message: Message):
    update_user_info(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    cursor.execute("SELECT username FROM users WHERE user_id = ?", (ADMIN_ID,))
    admin_row = cursor.fetchone()
    admin_username = admin_row[0] if admin_row and admin_row[0] != "mavjud_emas" else None

    if admin_username:
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💬 Adminga yozish", url=f"https://t.me/{admin_username}")]
            ]
        )
        await message.answer("👨‍💻 Savollar yoki takliflar bo'lsa, quyidagi tugma orqali adminga yozishingiz mumkin:", reply_markup=inline_kb)
    else:
        await message.answer("👨‍💻 Admin bilan bog'lanish uchun kuting yoki keyinroq urinib ko'ring.")

# ----------------- ADMIN PANEL -----------------
@dp.message(F.text == "⚙️ Admin Panel")
async def admin_panel(message: Message):
    update_user_info(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    if message.from_user.id != ADMIN_ID:
        return
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT user_id, full_name, username FROM users")
    users = cursor.fetchall()
    
    user_list_text = f"⚙️ <b>ADMIN PANEL</b>\n\n👥 <b>Jami foydalanuvchilar soni:</b> {total_users} ta\n\n<b>Foydalanuvchilar ro'yxati:</b>\n"
    
    for idx, user in enumerate(users, 1):
        uname = f"@{user[2]}" if user[2] != "mavjud_emas" else "Username yo'q"
        user_list_text += f"{idx}. {user[1]} | {uname} (ID: <code>{user[0]}</code>)\n"
        
    await message.answer(user_list_text, parse_mode="HTML")

# ----------------- BOTNI ISHGA TUSHIRISH -----------------
if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))