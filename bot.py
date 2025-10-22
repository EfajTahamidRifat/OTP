import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import cloudscraper
from bs4 import BeautifulSoup

# =========================================================
# 🔐 CONFIG — REPLACE THESE
# =========================================================
BOT_TOKEN = "8119637737:AAEPDFrHrUeAHWHSHkR7ZSE_Hp1vg3sYs9c"
ADMIN_ID = 6812877108          # Your Telegram user ID (integer)
GROUP_ID = -1003021667823     # Your IVASMS forward group ID (integer)

OWNER_NAME = "EfajTahamid"
EARNINGS_PER_SMS = 1.0000
MIN_WITHDRAWAL = 250.0
DB_FILE = "ivasms.db"

# IVASMS Credentials
IVASMS_EMAIL = "efajtahamid@gmail.com"
IVASMS_PASSWORD = "cHd8!6bwpW)MB*h"

# Endpoints
LOGIN_URL = "https://www.ivasms.com/login"
BASE = "https://www.ivasms.com"
GET_SMS_URL = f"{BASE}/portal/sms/received/getsms"
GET_NUMBER_URL = f"{BASE}/portal/sms/received/getsms/number"

# =========================================================
# DATABASE
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        numbers TEXT DEFAULT '[]',
        balance REAL DEFAULT 0.0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        bkash TEXT,
        status TEXT DEFAULT 'pending'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS otps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT,
        otp TEXT,
        full_msg TEXT,
        service TEXT,
        country TEXT,
        fetched_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS available_numbers (
        number TEXT PRIMARY KEY,
        country TEXT,
        range_info TEXT,
        assigned_to INTEGER DEFAULT NULL
    )''')
    conn.commit()
    conn.close()

def get_connection():
    return sqlite3.connect(DB_FILE)

# =========================================================
# SCRAPING: Login + Fetch Numbers from IVASMS
# =========================================================
def login_and_scrape_numbers():
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(LOGIN_URL)
        soup = BeautifulSoup(resp.text, 'html.parser')
        token = soup.find('input', {'name': '_token'})
        if not token:
            return False
        csrf = token.get('value')
        login_data = {'_token': csrf, 'email': IVASMS_EMAIL, 'password': IVASMS_PASSWORD}
        login_resp = scraper.post(LOGIN_URL, data=login_data)
        if "portal" not in login_resp.url:
            return False
        
        # Fetch ranges
        data = {"_token": csrf, "from": datetime.now(timezone.utc).date().isoformat(), "to": datetime.now(timezone.utc).date().isoformat()}
        sms_resp = scraper.post(GET_SMS_URL, data=data)
        soup = BeautifulSoup(sms_resp.text, 'html.parser')
        ranges = [opt.get_text(strip=True) for opt in soup.select("select#range option") if opt.get_text(strip=True)]
        if not ranges:
            ranges = [""]
        
        # Fetch numbers
        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM available_numbers")
        for rng in ranges:
            num_data = {"_token": csrf, "start": datetime.now(timezone.utc).date().isoformat(), "end": datetime.now(timezone.utc).date().isoformat(), "range": rng}
            num_resp = scraper.post(GET_NUMBER_URL, data=num_data)
            numbers = re.findall(r"(\+?\d{6,15})", num_resp.text)
            country = "UZBEKISTAN"
            for num in numbers:
                c.execute("INSERT OR IGNORE INTO available_numbers (number, country, range_info) VALUES (?, ?, ?)", (num, country, rng))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Scraping failed: {e}")
        return False

# =========================================================
# UTILS
# =========================================================
def detect_service(text):
    t = text.lower()
    services = {
        "whatsapp": "WhatsApp", "facebook": "Facebook", "telegram": "Telegram",
        "google": "Google", "instagram": "Instagram", "tiktok": "TikTok",
        "apple": "Apple", "1xbet": "1xBet", "melbet": "Melbet", "exness": "Exness",
        "wildberries": "Wildberries", "betwinner": "Betwinner", "netflix": "Netflix"
    }
    for k in sorted(services, key=len, reverse=True):
        if k in t:
            return services[k]
    return "Service"

def detect_country(number):
    return "UZBEKISTAN"

def mask_number(num):
    s = num.strip()
    if len(s) <= 10:
        return s
    return s[:7] + "****" + s[-4:]

def otp_exists(number, otp):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM otps WHERE number = ? AND otp = ?", (number, otp))
    return c.fetchone() is not None

def save_otp(number, otp, msg, service, country):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO otps (number, otp, full_msg, service, country, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
              (number, otp, msg, service, country, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_user_by_number(number):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE numbers LIKE ?", (f'%"{number}"%',))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def credit_user(user_id, amount=EARNINGS_PER_SMS):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def assign_number(user_id, country="UZBEKISTAN"):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT number FROM available_numbers WHERE country = ? AND assigned_to IS NULL LIMIT 1", (country,))
    row = c.fetchone()
    if not row:
        return None
    number = row[0]
    c.execute("UPDATE available_numbers SET assigned_to = ? WHERE number = ?", (user_id, number))
    c.execute("SELECT numbers FROM users WHERE user_id = ?", (user_id,))
    user_row = c.fetchone()
    if user_row:
        numbers = eval(user_row[0])
        numbers.append(number)
        c.execute("UPDATE users SET numbers = ? WHERE user_id = ?", (str(numbers), user_id))
    else:
        c.execute("INSERT INTO users (user_id, numbers) VALUES (?, ?)", (user_id, str([number])))
    conn.commit()
    conn.close()
    return number

# =========================================================
# BOT
# =========================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_states = {}

# Handle OTPs from IVASMS group
@dp.message(F.chat.id == GROUP_ID)
async def handle_otp(m: types.Message):
    if not m.text:
        return
    try:
        number = re.search(r'Number:\s*(\d+)', m.text).group(1)
        otp = re.search(r'OTP Code:\s*(\d{4,8})', m.text).group(1)
    except:
        return
    if otp_exists(number, otp):
        return
    service = detect_service(m.text)
    country = detect_country(number)
    save_otp(number, otp, m.text, service, country)
    msg = (
        f"📱 New OTP! ✨\n\n"
        f"📞 Number: {mask_number(number)}\n"
        f"🌍 Country: {country}\n"
        f"🆔 Provider: {service}\n"
        f"🔑 OTP Code: {otp}\n\n"
        f"📝 Full Message:\n{m.text}\n\n"
        f"{OWNER_NAME} 🎉 You have earned ৳{EARNINGS_PER_SMS:.4f} for this message!"
    )
    await bot.send_message(GROUP_ID, msg)
    user_id = get_user_by_number(number)
    if user_id:
        credit_user(user_id)
        await bot.send_message(user_id, msg)

# User commands
@dp.message(F.text == "/start")
async def start(m: types.Message):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎁 Get Number", callback_data="get_number")],
        [types.InlineKeyboardButton(text="👤 Account", callback_data="account")],
        [types.InlineKeyboardButton(text="💰 Withdraw", callback_data="withdraw")]
    ])
    await m.answer(
        "👋 Welcome to IVASMS OTP Bot!\n\n"
        f"💰 Earn ৳{EARNINGS_PER_SMS:.4f} per OTP\n"
        "📱 Get a number → Use it → Get paid!",
        reply_markup=kb
    )

@dp.callback_query(F.data == "get_number")
async def get_number(q: types.CallbackQuery):
    number = assign_number(q.from_user.id)
    if number:
        await q.message.edit_text(f"✅ Assigned:\n📞 <code>{number}</code>\n🌍 UZBEKISTAN", parse_mode=ParseMode.HTML)
    else:
        await q.message.edit_text("❌ No numbers available. Admin is syncing IVASMS...")

@dp.callback_query(F.data == "account")
async def account(q: types.CallbackQuery):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT balance, numbers FROM users WHERE user_id = ?", (q.from_user.id,))
    row = c.fetchone()
    balance = row[0] if row else 0.0
    numbers = eval(row[1]) if row else []
    conn.close()
    nums = "\n".join([f"• <code>{n}</code>" for n in numbers]) if numbers else "None"
    await q.message.edit_text(
        f"👤 Your Account\n\n"
        f"💰 Balance: ৳{balance:.2f}\n"
        f"📱 Assigned Numbers:\n{nums}",
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "withdraw")
async def withdraw(q: types.CallbackQuery):
    await q.message.edit_text("📲 Send your Bkash number (11 digits, e.g., 017XXXXXXXX):")
    user_states[q.from_user.id] = "awaiting_bkash"

@dp.message(F.text)
async def handle_msg(m: types.Message):
    if user_states.get(m.from_user.id) == "awaiting_bkash":
        bkash = m.text.strip()
        if not (bkash.startswith('01') and len(bkash) == 11 and bkash.isdigit()):
            await m.answer("❌ Invalid Bkash number.")
            return
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (m.from_user.id,))
        balance = c.fetchone()[0] if c.fetchone() else 0.0
        if balance < MIN_WITHDRAWAL:
            await m.answer(f"❌ Minimum withdrawal is ৳{MIN_WITHDRAWAL:.2f}")
            return
        c.execute("INSERT INTO withdrawals (user_id, amount, bkash) VALUES (?, ?, ?)", (m.from_user.id, balance, bkash))
        c.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (m.from_user.id,))
        conn.commit()
        conn.close()
        await m.answer(f"✅ Withdrawal submitted!\nAmount: ৳{balance:.2f}\nBkash: {bkash}")
        del user_states[m.from_user.id]

# Admin commands
@dp.message(F.text == "/admin")
async def admin_panel(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "🔐 Admin Panel\n\n"
        "🔄 /scrape — Sync numbers from IVASMS\n"
        "➕ /addnumber UZBEKISTAN 998992841234 — Add number manually\n"
        "✅ /approve <id> — Approve withdrawal"
    )

@dp.message(F.text == "/scrape")
async def scrape_numbers(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("🔄 Scraping IVASMS for numbers...")
    success = login_and_scrape_numbers()
    await m.answer("✅ Numbers synced!" if success else "❌ Scraping failed. Check credentials.")

@dp.message(F.text.startswith("/addnumber"))
async def add_number(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        _, country, number = m.text.split()
        if not re.match(r"\d{6,15}", number):
            raise ValueError()
        conn = get_connection()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO available_numbers (number, country) VALUES (?, ?)", (number, country))
        conn.commit()
        conn.close()
        await m.answer(f"✅ Added {number} ({country})")
    except:
        await m.answer("Usage: /addnumber COUNTRY NUMBER")

@dp.message(F.text.startswith("/approve"))
async def approve(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        wid = int(m.text.split()[1])
        conn = get_connection()
        c = conn.cursor()
        c.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (wid,))
        conn.commit()
        conn.close()
        await m.answer(f"✅ Approved withdrawal #{wid}")
    except:
        await m.answer("Usage: /approve <id>")

# =========================================================
# RUN WITH POLLING
# =========================================================
async def main():
    init_db()
    print("✅ Bot started with polling.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())