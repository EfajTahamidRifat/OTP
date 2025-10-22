import asyncio
import re
import html
import sqlite3
import os
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import logging
import cloudscraper
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================================
# 🔐 HARD-CODED CONFIG — REPLACE THESE
# =========================================================
BOT_TOKEN = "8119637737:AAEPDFrHrUeAHWHSHkR7ZSE_Hp1vg3sYs9c"
ADMIN_ID = 6812877108
GROUP_ID = -1001234567890
WEBHOOK_URL = "https://otp-xfm2.onrender.com/webhook"
PORT = 10000

OWNER_NAME = "Rezan"
EARNINGS_PER_SMS = 1.0000
MIN_WITHDRAWAL = 250.0
DB_PATH = "/tmp/ivasms_bot.db"

# IVASMS Credentials
IVASMS_EMAIL = "efajtahamid.com"
IVASMS_PASSWORD = "cHd8!6bwpW)MB*h"

# Endpoints
LOGIN_URL = "https://www.ivasms.com/login"
BASE = "https://www.ivasms.com"
GET_SMS_URL = f"{BASE}/portal/sms/received/getsms"
GET_NUMBER_URL = f"{BASE}/portal/sms/received/getsms/number"
GET_OTP_URL = f"{BASE}/portal/sms/received/getsms/number/sms"

# Branding
OWNER_LINK = "https://t.me/BashOnChain"
NUMBERS_CHANNEL = "https://t.me/oxfreebackup"

# =========================================================
# COUNTRY & SERVICE MAPPINGS
# =========================================================
COUNTRY_FLAGS = {
    "998": "UZBEKISTAN", "225": "IVORY COAST", "93": "AFGHANISTAN",
    "234": "NIGERIA", "967": "YEMEN", "63": "PHILIPPINES",
    "20": "EGYPT", "994": "AZERBAIJAN", "996": "KYRGYZSTAN",
    "228": "TOGO", "961": "LEBANON", "229": "BENIN",
    "977": "NEPAL", "216": "TUNISIA"
}

SERVICES = {
    "whatsapp": "WhatsApp", "facebook": "Facebook", "telegram": "Telegram",
    "google": "Google", "instagram": "Instagram", "tiktok": "TikTok",
    "apple": "Apple", "1xbet": "1xBet", "melbet": "Melbet", "exness": "Exness",
    "wildberries": "Wildberries", "betwinner": "Betwinner", "netflix": "Netflix"
}

# =========================================================
# DATABASE
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
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
    return sqlite3.connect(DB_PATH)

# =========================================================
# SCRAPING LOGIC
# =========================================================
def login_and_get_tokens():
    scraper = cloudscraper.create_scraper()
    resp = scraper.get(LOGIN_URL)
    soup = BeautifulSoup(resp.text, 'html.parser')
    token = soup.find('input', {'name': '_token'})
    if not token:
        return None, None
    csrf = token.get('value')
    login_data = {
        '_token': csrf,
        'email': IVASMS_EMAIL,
        'password': IVASMS_PASSWORD
    }
    login_resp = scraper.post(LOGIN_URL, data=login_data)
    if "portal" not in login_resp.url:
        return None, None
    portal_resp = scraper.get(f"{BASE}/portal/sms/received")
    soup2 = BeautifulSoup(portal_resp.text, 'html.parser')
    new_token = soup2.find('input', {'name': '_token'})
    new_csrf = new_token.get('value') if new_token else csrf
    return new_csrf, scraper.cookies.get_dict()

def fetch_ranges(csrf, cookies):
    scraper = cloudscraper.create_scraper()
    scraper.cookies.update(cookies)
    scraper.headers.update({"X-Requested-With": "XMLHttpRequest"})
    data = {"_token": csrf, "from": datetime.now(timezone.utc).date().isoformat(), "to": datetime.now(timezone.utc).date().isoformat()}
    resp = scraper.post(GET_SMS_URL, data=data)
    if resp.status_code != 200:
        return [""]
    soup = BeautifulSoup(resp.text, 'html.parser')
    ranges = []
    for opt in soup.select("select#range option"):
        val = opt.get_text(strip=True)
        if val and "Select" not in val:
            ranges.append(val)
    return ranges or [""]

def fetch_numbers(csrf, cookies, rng):
    scraper = cloudscraper.create_scraper()
    scraper.cookies.update(cookies)
    scraper.headers.update({"X-Requested-With": "XMLHttpRequest"})
    data = {"_token": csrf, "start": datetime.now(timezone.utc).date().isoformat(), "end": datetime.now(timezone.utc).date().isoformat(), "range": rng}
    resp = scraper.post(GET_NUMBER_URL, data=data)
    if resp.status_code != 200:
        return []
    return re.findall(r"(\+?\d{6,15})", resp.text)

def fetch_sms(csrf, cookies, number, rng):
    scraper = cloudscraper.create_scraper()
    scraper.cookies.update(cookies)
    scraper.headers.update({"X-Requested-With": "XMLHttpRequest"})
    data = {"_token": csrf, "start": datetime.now(timezone.utc).date().isoformat(), "Number": number, "Range": rng}
    resp = scraper.post(GET_OTP_URL, data=data)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, 'html.parser')
    msgs = []
    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) >= 3:
            msg = tds[2].get_text(strip=True)
            if re.search(r"\d{4,8}", msg):
                msgs.append({"message": msg, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
    return msgs

def update_available_numbers():
    csrf, cookies = login_and_get_tokens()
    if not csrf or not cookies:
        return False
    ranges = fetch_ranges(csrf, cookies)
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM available_numbers")
    for rng in ranges:
        numbers = fetch_numbers(csrf, cookies, rng)
        country = detect_country(numbers[0]) if numbers else "Unknown"
        for num in numbers:
            c.execute("INSERT OR IGNORE INTO available_numbers (number, country, range_info) VALUES (?, ?, ?)", (num, country, rng))
    conn.commit()
    conn.close()
    return True

# =========================================================
# UTILS
# =========================================================
def detect_country(number):
    s = number.lstrip("+")
    for prefix, name in COUNTRY_FLAGS.items():
        if s.startswith(prefix):
            return name
    return "Unknown"

def detect_service(text):
    t = text.lower()
    for k in sorted(SERVICES, key=len, reverse=True):
        if k in t:
            return SERVICES[k]
    return "Service"

def extract_otps(text):
    if m := re.search(r"(?:code|is|:)\s*(\b\d{4,8}\b)", text, re.IGNORECASE):
        return [m.group(1)]
    return re.findall(r"\b(\d{4,8})\b", text)

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

def get_available_countries():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT country, COUNT(*) FROM available_numbers WHERE assigned_to IS NULL GROUP BY country")
    rows = c.fetchall()
    conn.close()
    return dict(rows)

def assign_number(user_id, country):
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
# TELEGRAM BOT
# =========================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_states = {}
_worker_running = False

async def worker():
    global _worker_running
    _worker_running = True
    while _worker_running:
        try:
            success = update_available_numbers()
            if not success:
                await asyncio.sleep(30)
                continue
            csrf, cookies = login_and_get_tokens()
            if not csrf or not cookies:
                await asyncio.sleep(30)
                continue
            ranges = fetch_ranges(csrf, cookies)
            for rng in ranges:
                numbers = fetch_numbers(csrf, cookies, rng)
                country = detect_country(numbers[0]) if numbers else "Unknown"
                for number in numbers:
                    msgs = fetch_sms(csrf, cookies, number, rng)
                    for item in msgs:
                        otps = extract_otps(item['message'])
                        if not otps:
                            continue
                        otp = otps[0]
                        if otp_exists(number, otp):
                            continue
                        service = detect_service(item['message'])
                        save_otp(number, otp, item['message'], service, country)
                        message_text = (
                            f"📱 New OTP! ✨\n\n"
                            f"📞 Number: {mask_number(number)}\n"
                            f"🌍 Country: {country}\n"
                            f"🆔 Provider: {service}\n"
                            f"🔑 OTP Code: {otp}\n\n"
                            f"📝 Full Message:\n"
                            f"{item['message']}\n\n"
                            f"{OWNER_NAME} 🎉 You have earned ৳{EARNINGS_PER_SMS:.4f} for this message!"
                        )
                        await bot.send_message(GROUP_ID, message_text)
                        user_id = get_user_by_number(number)
                        if user_id:
                            credit_user(user_id)
                            await bot.send_message(user_id, message_text)
        except Exception as e:
            logger.error(f"Worker error: {e}")
        await asyncio.sleep(60)
    _worker_running = False

# Handlers
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
    countries = get_available_countries()
    if not countries:
        await q.message.edit_text("❌ No numbers available. Scraping IVASMS...")
        return
    kb = [[types.InlineKeyboardButton(text=f"{k} ({v})", callback_data=f"country_{k}")] for k, v in countries.items()]
    kb.append([types.InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    await q.message.edit_text("🌍 Select country:", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("country_"))
async def select_country(q: types.CallbackQuery):
    country = q.data.split('_', 1)[1]
    number = assign_number(q.from_user.id, country)
    if number:
        await q.message.edit_text(f"✅ Assigned:\n📞 <code>{number}</code>\n🌍 {country}", parse_mode=ParseMode.HTML)
    else:
        await q.message.edit_text("❌ No available numbers.")

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
        row = c.fetchone()
        balance = row[0] if row else 0.0
        if balance < MIN_WITHDRAWAL:
            await m.answer(f"❌ Minimum withdrawal is ৳{MIN_WITHDRAWAL:.2f}")
            return
        c.execute("INSERT INTO withdrawals (user_id, amount, bkash) VALUES (?, ?, ?)", (m.from_user.id, balance, bkash))
        c.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (m.from_user.id,))
        conn.commit()
        conn.close()
        await m.answer(
            f"✅ Withdrawal request submitted!\n\n"
            f"Amount: ৳{balance:.2f}\n"
            f"Bkash: {bkash}\n\n"
            "Admin will process your request soon."
        )
        del user_states[m.from_user.id]

# Admin
@dp.message(F.text == "/admin")
async def admin(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Scrape IVASMS", callback_data="admin_scrape")],
        [types.InlineKeyboardButton(text="💸 Withdrawals", callback_data="admin_withdrawals")],
        [types.InlineKeyboardButton(text="👥 Users", callback_data="admin_users")]
    ])
    await m.answer("🔐 Admin Panel", reply_markup=kb)

@dp.callback_query(F.data == "admin_scrape")
async def admin_scrape(q: types.CallbackQuery):
    await q.message.edit_text("🔄 Scraping IVASMS for numbers...")
    success = update_available_numbers()
    await q.message.edit_text("✅ Scraping completed!" if success else "❌ Scraping failed.")

@dp.callback_query(F.data == "admin_withdrawals")
async def admin_wd(q: types.CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, user_id, amount, bkash FROM withdrawals WHERE status = 'pending'")
    pending = c.fetchall()
    conn.close()
    if not pending:
        await q.message.edit_text("✅ No pending withdrawals.")
        return
    text = "💸 Pending Withdrawals:\n\n" + "\n".join([f"ID: {w[0]} | User: {w[1]} | ৳{w[2]:.2f} | {w[3]}" for w in pending])
    text += f"\n\nUse /approve <id> to approve."
    await q.message.edit_text(text)

@dp.callback_query(F.data == "admin_users")
async def admin_users(q: types.CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, balance, numbers FROM users")
    users = c.fetchall()
    conn.close()
    if not users:
        await q.message.edit_text("📭 No users found.")
        return
    text = "👥 Users:\n\n"
    for uid, uname, bal, nums in users:
        count = len(eval(nums))
        name = uname or f"User{uid}"
        text += f"ID: <code>{uid}</code> | {name} | Balance: ৳{bal:.2f} | Numbers: {count}\n"
    await q.message.edit_text(text, parse_mode=ParseMode.HTML)

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
        await m.answer(f"✅ Withdrawal #{wid} approved and paid!")
    except:
        await m.answer("Usage: /approve <id>")

@dp.message(F.text == "/on")
async def on(m: types.Message):
    if m.from_user.id == ADMIN_ID and not _worker_running:
        asyncio.create_task(worker())
        await m.answer("✅ Worker started.")

@dp.message(F.text == "/off")
async def off(m: types.Message):
    if m.from_user.id == ADMIN_ID:
        global _worker_running
        _worker_running = False
        await m.answer("🛑 Worker stopped.")

# =========================================================
# MAIN (Webhook)
# =========================================================
async def on_startup(app):
    init_db()
    await bot.set_webhook(WEBHOOK_URL)
    logger.info("✅ Bot started with IVASMS scraper.")

async def on_shutdown(app):
    await bot.delete_webhook(drop_pending_updates=True)

if __name__ == "__main__":
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path="/webhook")
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)