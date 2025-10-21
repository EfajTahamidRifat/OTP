import asyncio
import re
import html
import sqlite3
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import os

# =========================================================
# 🔐 HARD-CODED CREDENTIALS — REPLACE THESE
# =========================================================
BOT_TOKEN = "8119637737:AAEPDFrHrUeAHWHSHkR7ZSE_Hp1vg3sYs9c"
ADMIN_ID = 6812877108          # Your Telegram user ID
GROUP_ID = -1003021667823     # Your IVASMS forward group ID
OWNER_LINK = "https://t.me/cryptoearn36"

IVASMS_EMAIL = "efajtahamid@gmail.com"
IVASMS_PASSWORD = "cHd8!6bwpW)MB*h"

FETCH_INTERVAL = 30
DB_FILE = "ivasms_bot.db"

# =========================================================
# DATABASE SETUP
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS otps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT,
        otp TEXT,
        full_msg TEXT,
        service TEXT,
        country TEXT,
        range_info TEXT,
        fetched_at TEXT
    )''')
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
# PLAYWRIGHT SCRAPER (PRIMARY)
# =========================================================
def scrape_with_playwright():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://www.ivasms.com/login", timeout=20000)
            page.wait_for_selector('input[name="_token"]', timeout=15000)
            
            csrf = page.input_value('input[name="_token"]')
            page.fill('input[name="email"]', IVASMS_EMAIL)
            page.fill('input[name="password"]', IVASMS_PASSWORD)
            page.click('button[type="submit"]')
            page.wait_for_url("**/portal**", timeout=15000)
            
            page.goto("https://www.ivasms.com/portal/sms/received")
            new_csrf = page.input_value('input[name="_token"]')
            cookies = {c['name']: c['value'] for c in page.context.cookies()}
            browser.close()
            return new_csrf, cookies
    except Exception as e:
        print(f"[Playwright] Failed: {e}")
        return None, None

# =========================================================
# CLOUDSCRAPER FALLBACK (SECONDARY)
# =========================================================
def scrape_with_cloudscraper():
    try:
        import cloudscraper
        from bs4 import BeautifulSoup
        scraper = cloudscraper.create_scraper()
        resp = scraper.get("https://www.ivasms.com/login", timeout=15)
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
        login_resp = scraper.post("https://www.ivasms.com/login", data=login_data, timeout=15)
        if "portal" not in login_resp.url:
            return None, None
        
        portal_resp = scraper.get("https://www.ivasms.com/portal/sms/received")
        soup2 = BeautifulSoup(portal_resp.text, 'html.parser')
        new_token = soup2.find('input', {'name': '_token'})
        new_csrf = new_token.get('value') if new_token else csrf
        return new_csrf, scraper.cookies.get_dict()
    except Exception as e:
        print(f"[Cloudscraper] Failed: {e}")
        return None, None

# =========================================================
# FETCH DATA (WITH FALLBACK)
# =========================================================
def get_csrf_and_cookies():
    csrf, cookies = scrape_with_playwright()
    if csrf and cookies:
        return csrf, cookies
    print("Falling back to cloudscraper...")
    return scrape_with_cloudscraper()

def fetch_with_session(url, data, cookies):
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        scraper.cookies.update(cookies)
        scraper.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.ivasms.com",
            "Referer": "https://www.ivasms.com/portal/sms/received"
        })
        resp = scraper.post(url, data=data, timeout=20)
        return resp.text if resp.status_code == 200 else ""
    except:
        return ""

def fetch_ranges(csrf, cookies):
    html = fetch_with_session(
        "https://www.ivasms.com/portal/sms/received/getsms",
        {"_token": csrf, "from": datetime.now(timezone.utc).date().isoformat(), "to": datetime.now(timezone.utc).date().isoformat()},
        cookies
    )
    ranges = []
    for match in re.finditer(r"<option[^>]*value=[^>]*>([^<]+)</option>", html):
        val = match.group(1).strip()
        if val and "Select" not in val:
            ranges.append(val)
    return ranges or [""]

def fetch_numbers(csrf, cookies, rng):
    html = fetch_with_session(
        "https://www.ivasms.com/portal/sms/received/getsms/number",
        {"_token": csrf, "start": datetime.now(timezone.utc).date().isoformat(), "end": datetime.now(timezone.utc).date().isoformat(), "range": rng},
        cookies
    )
    return re.findall(r"(\+?\d{6,15})", html)

def fetch_sms(csrf, cookies, number, rng):
    html = fetch_with_session(
        "https://www.ivasms.com/portal/sms/received/getsms/number/sms",
        {"_token": csrf, "start": datetime.now(timezone.utc).date().isoformat(), "Number": number, "Range": rng},
        cookies
    )
    msgs = []
    for match in re.finditer(r"<td[^>]*>([^<]{10,})</td>", html):
        msg = match.group(1).strip()
        if re.search(r"\d{4,8}", msg):
            msgs.append({"message": msg, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
    return msgs

# =========================================================
# UTILS
# =========================================================
def extract_otps(text):
    if m := re.search(r"(?:code|is|:)\s*(\b\d{4,8}\b)", text, re.IGNORECASE):
        return [m.group(1)]
    return re.findall(r"\b(\d{4,8})\b", text)

def detect_service(text):
    t = text.lower()
    services = {"whatsapp": "WhatsApp", "facebook": "Facebook", "telegram": "Telegram", "google": "Google", "instagram": "Instagram"}
    for k in sorted(services, key=len, reverse=True):
        if k in t:
            return services[k]
    return "Service"

def detect_country(number):
    s = number.lstrip("+")
    flags = {"998": "🇺🇿 Uzbekistan", "225": "🇨🇮 Ivory Coast", "93": "🇦🇫 Afghanistan", "234": "🇳🇬 Nigeria"}
    for prefix, name in flags.items():
        if s.startswith(prefix):
            return name
    return "🌍 Unknown"

def mask_number(num):
    s = num.strip()
    if len(s) <= 10:
        return s
    return s[:7] + "****" + s[-3:]

def otp_exists(number, otp):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM otps WHERE number = ? AND otp = ?", (number, otp))
    return c.fetchone() is not None

def save_otp(number, otp, msg, service, country, rng):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO otps (number, otp, full_msg, service, country, range_info, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (number, otp, msg, service, country, rng, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def update_available_numbers(numbers, country, rng):
    conn = get_connection()
    c = conn.cursor()
    for num in numbers:
        c.execute("INSERT OR IGNORE INTO available_numbers (number, country, range_info) VALUES (?, ?, ?)", (num, country, rng))
    conn.commit()
    conn.close()

def assign_number_to_user(user_id, country):
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

def get_user_by_number(number):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE numbers LIKE ?", (f'%"{number}"%',))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def credit_user(user_id, amount=1.0):
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

# =========================================================
# TELEGRAM BOT
# =========================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_states = {}
_worker_running = False

async def forward_entry(e):
    text = (
        f"🔔 <b>NEW OTP DETECTED</b>\n🆕\n\n"
        f"🕰 <b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🌍 <b>Country:</b> {e.get('country')}\n"
        f"⚙️ <b>Service:</b> {e.get('service')}\n"
        f"☎️ <b>Number:</b> {mask_number(e['number'])}\n"
        f"🔑 <b>OTP:</b> <code>{e['otp']}</code>\n\n"
        f"📩 <b>Full Message:</b>\n"
        f"<pre>{html.escape(e.get('full_msg', ''))}</pre>"
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("👑 ×°𝓞𝔀𝓷𝓮𝓻°× 👑", url=OWNER_LINK),
         types.InlineKeyboardButton("༄ 𝐃𝐞𝐯𝐞𝐥𝐨𝐩𝐞𝐫 𒆜", url="https://t.me/BashOnChain ")],
        [types.InlineKeyboardButton("★彡[ᴀʟʟ ɴᴜᴍʙᴇʀꜱ]彡★", url="https://t.me/oxfreebackup ")]
    ])
    await bot.send_message(GROUP_ID, text, reply_markup=kb)

async def send_to_user(user_id, e):
    await bot.send_message(
        user_id,
        f"💰 New OTP!\n🔑 <code>{e['otp']}</code>\n📞 <code>{e['number']}</code>\n\n✅ ৳1.0000 added!",
        parse_mode=ParseMode.HTML
    )

async def worker():
    global _worker_running
    _worker_running = True
    csrf, cookies = get_csrf_and_cookies()
    if not csrf or not cookies:
        print("❌ Login failed with both Playwright and cloudscraper.")
        _worker_running = False
        return
    while _worker_running:
        try:
            ranges = fetch_ranges(csrf, cookies)
            for rng in ranges:
                numbers = fetch_numbers(csrf, cookies, rng)
                country = detect_country(numbers[0]) if numbers else "Unknown"
                update_available_numbers(numbers, country, rng)
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
                        entry = {"number": number, "otp": otp, "full_msg": item['message'], "service": service, "country": country, "range": rng}
                        save_otp(number, otp, item['message'], service, country, rng)
                        await forward_entry(entry)
                        user_id = get_user_by_number(number)
                        if user_id:
                            credit_user(user_id)
                            await send_to_user(user_id, entry)
        except Exception as e:
            print(f"Worker error: {e}")
            csrf, cookies = get_csrf_and_cookies()
            if not csrf or not cookies:
                break
        await asyncio.sleep(FETCH_INTERVAL)
    _worker_running = False

# === HANDLERS ===
@dp.message(F.text == "/start")
async def start(m: types.Message):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("🎁 Get Number", callback_data='get_number')],
        [types.InlineKeyboardButton("👤 Account", callback_data='account')],
        [types.InlineKeyboardButton("💰 Withdraw", callback_data='withdraw')]
    ])
    await m.answer("👋 Welcome! Select an option:", reply_markup=kb)

@dp.callback_query(F.data == "get_number")
async def get_number(q: types.CallbackQuery):
    countries = get_available_countries()
    if not countries:
        await q.message.edit_text("❌ No numbers available. Try again later.")
        return
    kb = [[types.InlineKeyboardButton(f"{k} ({v})", callback_data=f'country_{k}')] for k, v in countries.items()]
    kb.append([types.InlineKeyboardButton("❌ Cancel", callback_data='cancel')])
    await q.message.edit_text("🌍 Select country:", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("country_"))
async def select_country(q: types.CallbackQuery):
    country = q.data.split('_', 1)[1]
    number = assign_number_to_user(q.from_user.id, country)
    if number:
        await q.message.edit_text(f"✅ Assigned:\n📞 <code>{number}</code>\n🌍 {country}", parse_mode=ParseMode.HTML)
    else:
        await q.message.edit_text("❌ No available numbers for this country.")

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
    await q.message.edit_text(f"👤 Account\n💰 Balance: ৳{balance:.2f}\n📱 Numbers:\n{nums}", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "withdraw")
async def withdraw(q: types.CallbackQuery):
    await q.message.edit_text("📲 Send your Bkash number (11 digits, e.g., 017XXXXXXXX):")
    user_states[q.from_user.id] = "awaiting_bkash"

@dp.message(F.text)
async def handle_msg(m: types.Message):
    if user_states.get(m.from_user.id) == "awaiting_bkash":
        bkash = m.text.strip()
        if not (bkash.startswith('01') and len(bkash) == 11 and bkash.isdigit()):
            await m.answer("❌ Invalid Bkash number. Must be 11 digits starting with 01.")
            return
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (m.from_user.id,))
        row = c.fetchone()
        balance = row[0] if row else 0.0
        if balance < 250.0:
            await m.answer("❌ Minimum withdrawal is ৳250.00")
            return
        c.execute("INSERT INTO withdrawals (user_id, amount, bkash) VALUES (?, ?, ?)", (m.from_user.id, balance, bkash))
        c.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (m.from_user.id,))
        conn.commit()
        conn.close()
        await m.answer(f"✅ Withdrawal request submitted!\nAmount: ৳{balance:.2f}\nBkash: {bkash}")
        del user_states[m.from_user.id]

# === ADMIN ===
@dp.message(F.text == "/admin")
async def admin(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("💸 Pending Withdrawals", callback_data='admin_withdrawals')],
        [types.InlineKeyboardButton("✅ Approve Withdrawal", callback_data='admin_approve_info')]
    ])
    await m.answer("🔐 Admin Panel", reply_markup=kb)

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
    text += "\n\nUse /approve <id> to approve."
    await q.message.edit_text(text)

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

# === CONTROL ===
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

# === STARTUP ===
async def on_startup():
    init_db()
    print("✅ IVASMS Bot started with Playwright + cloudscraper fallback.")

if __name__ == "__main__":
    dp.startup.register(on_startup)
    dp.run_polling(bot)