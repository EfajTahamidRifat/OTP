# main.py - Enhanced IVASMS OTP Bot (scalable, country selection, cooldown, nicer messages)
import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import cloudscraper
from bs4 import BeautifulSoup
import html

# -------------------------
# CONFIG (env-first, then fallbacks)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8119637737:AAEPDFrHrUeAHWHSHkR7ZSE_Hp1vg3sYs9c"
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6812877108)
GROUP_ID = int(os.getenv("GROUP_ID") or -1003021667823)
GROUP_LINK = os.getenv("GROUP_LINK") or "https://t.me/+NACzz1-K6dNiMjA9"   # forward group

OWNER_NAME = os.getenv("OWNER_NAME") or "EfajTahamid"
EARNINGS_PER_SMS = float(os.getenv("EARNINGS_PER_SMS") or 1.0)
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL") or 250.0)
DB_FILE = os.getenv("DB_FILE") or "ivasms.db"

IVASMS_EMAIL = os.getenv("IVASMS_EMAIL") or "efajtahamid@gmail.com"
IVASMS_PASSWORD = os.getenv("IVASMS_PASSWORD") or "cHd8!6bwpW)MB*h"

LOGIN_URL = "https://www.ivasms.com/login"
BASE = "https://www.ivasms.com"
GET_SMS_URL = f"{BASE}/portal/sms/received/getsms"
GET_NUMBER_URL = f"{BASE}/portal/sms/received/getsms/number"
GET_OTP_URL = f"{BASE}/portal/sms/received/getsms/otps"

# Scale & limits
ASYNC_WORKERS = int(os.getenv("ASYNC_WORKERS") or 2000)   # concurrency limit for scraping/processing
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS") or 10)  # cooldown between get number requests per user
MAX_NUMBERS_PER_USER = int(os.getenv("MAX_NUMBERS_PER_USER") or 10)

# -------------------------
# DB helpers and init
# -------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # users: track last_assigned for cooldown
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            numbers TEXT DEFAULT '[]',
            balance REAL DEFAULT 0.0,
            last_assigned TEXT DEFAULT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            bkash TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT,
            otp TEXT,
            full_msg TEXT,
            service TEXT,
            country TEXT,
            fetched_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS available_numbers (
            number TEXT PRIMARY KEY,
            country TEXT,
            range_info TEXT,
            assigned_to INTEGER DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_connection():
    return sqlite3.connect(DB_FILE)

# -------------------------
# Scraper: robust login + number sync
# -------------------------
def login_and_scrape_numbers():
    """
    Logs into IVASMS and syncs available numbers into available_numbers table.
    Returns True on success.
    """
    try:
        scraper = cloudscraper.create_scraper(browser='chrome')
        print("[scrape] GET login page...")
        r = scraper.get(LOGIN_URL, timeout=15)
        if r.status_code != 200:
            print("[scrape] login page status", r.status_code)
            return False

        soup = BeautifulSoup(r.text, "html.parser")
        token_input = soup.find("input", {"name": "_token"})
        if not token_input or not token_input.get("value"):
            print("[scrape] No CSRF token found on login page.")
            return False
        csrf = token_input.get("value")

        # post login
        data = {"_token": csrf, "email": IVASMS_EMAIL, "password": IVASMS_PASSWORD}
        login_resp = scraper.post(LOGIN_URL, data=data, timeout=15, allow_redirects=False)

        # handle redirect to portal
        if login_resp.status_code in (301,302) and "location" in login_resp.headers:
            loc = login_resp.headers["location"]
            if loc.startswith("/"):
                loc = BASE + loc
            portal = scraper.get(loc, timeout=15)
        else:
            portal = login_resp

        # ensure portal presence
        url_to_check = getattr(portal, "url", "") or getattr(login_resp, "url", "")
        if "portal" not in url_to_check and "portal" not in portal.text:
            print("[scrape] Not in portal after login; login failed.")
            return False

        # update csrf if present
        soup2 = BeautifulSoup(portal.text, "html.parser")
        t2 = soup2.find("input", {"name": "_token"})
        if t2 and t2.get("value"):
            csrf = t2.get("value")

        # fetch sms ranges
        data_req = {"_token": csrf, "from": datetime.now(timezone.utc).date().isoformat(), "to": datetime.now(timezone.utc).date().isoformat()}
        sms_resp = scraper.post(GET_SMS_URL, data=data_req, timeout=20)
        if sms_resp.status_code != 200:
            print("[scrape] GET_SMS status", sms_resp.status_code)
            return False

        ranges = [opt.get_text(strip=True) for opt in BeautifulSoup(sms_resp.text,"html.parser").select("select#range option") if opt.get_text(strip=True)]
        if not ranges:
            ranges = [""]

        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM available_numbers")
        for rng in ranges:
            nd = {"_token": csrf, "start": datetime.now(timezone.utc).date().isoformat(), "end": datetime.now(timezone.utc).date().isoformat(), "range": rng}
            r2 = scraper.post(GET_NUMBER_URL, data=nd, timeout=20)
            if r2.status_code != 200:
                print("[scrape] GET_NUMBER failed for range", rng, "status", r2.status_code)
                continue
            # numbers extraction tolerant to either JSON or HTML
            numbers = re.findall(r"(?:\+?\d{6,15})", r2.text)
            if not numbers:
                # try json
                try:
                    j = r2.json()
                    if isinstance(j, list):
                        for item in j:
                            if isinstance(item, dict):
                                num = item.get("Number") or item.get("number") or item.get("msisdn") or item.get("msisdn")
                                if num:
                                    numbers.append(str(num))
                except Exception:
                    pass
            for num in numbers:
                try:
                    c.execute("INSERT OR IGNORE INTO available_numbers (number, country, range_info) VALUES (?, ?, ?)",
                              (num, "UNKNOWN", rng))
                except Exception:
                    pass
        conn.commit()
        conn.close()
        print("[scrape] Sync complete.")
        return True
    except Exception as ex:
        print("[scrape] Exception:", ex)
        return False

# -------------------------
# Utility functions
# -------------------------
def detect_service(text: str):
    t = (text or "").lower()
    services = {
        "whatsapp": "WhatsApp","facebook":"Facebook","telegram":"Telegram",
        "google":"Google","instagram":"Instagram","tiktok":"TikTok","apple":"Apple",
        "1xbet":"1xBet","melbet":"Melbet","exness":"Exness","wildberries":"Wildberries",
        "betwinner":"Betwinner","netflix":"Netflix"
    }
    for k in sorted(services.keys(), key=len, reverse=True):
        if k in t:
            return services[k]
    return "Service"

def mask_number_display(num: str):
    s = str(num)
    if len(s) <= 10:
        return s
    # show 1st 3 + **** + last 4 (but user wanted first 7 earlier; use shorter display for generality)
    return s[:3] + "****" + s[-4:]

def otp_exists(number, otp):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM otps WHERE number=? AND otp=?", (number, otp))
    found = c.fetchone() is not None
    conn.close()
    return found

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
    c.execute("SELECT user_id, numbers FROM users")
    rows = c.fetchall()
    conn.close()
    for user_id, numbers_str in rows:
        try:
            nums = eval(numbers_str or "[]")
            if number in nums:
                return user_id
        except Exception:
            continue
    return None

def credit_user(user_id, amount=EARNINGS_PER_SMS):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def assign_number(user_id, country_preference=None):
    """
    Assign single available number for user_id.
    If country_preference provided, try that country first, else any.
    """
    conn = get_connection()
    c = conn.cursor()
    if country_preference:
        c.execute("SELECT number FROM available_numbers WHERE country = ? AND assigned_to IS NULL LIMIT 1", (country_preference,))
        row = c.fetchone()
        if not row:
            # fallback to any
            c.execute("SELECT number FROM available_numbers WHERE assigned_to IS NULL LIMIT 1")
            row = c.fetchone()
    else:
        c.execute("SELECT number FROM available_numbers WHERE assigned_to IS NULL LIMIT 1")
        row = c.fetchone()
    if not row:
        conn.close()
        return None
    number = row[0]
    c.execute("UPDATE available_numbers SET assigned_to = ? WHERE number = ?", (user_id, number))
    c.execute("SELECT numbers FROM users WHERE user_id = ?", (user_id,))
    user_row = c.fetchone()
    if user_row:
        try:
            numbers = eval(user_row[0])
        except Exception:
            numbers = []
        if number not in numbers:
            numbers.append(number)
        c.execute("UPDATE users SET numbers = ?, last_assigned = ? WHERE user_id = ?", (str(numbers), datetime.now(timezone.utc).isoformat(), user_id))
    else:
        c.execute("INSERT INTO users (user_id, numbers, last_assigned) VALUES (?, ?, ?)", (user_id, str([number]), datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return number

# helper: get counts for country and user's number count
def counts_for(user_id, country):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM available_numbers WHERE country = ? AND assigned_to IS NULL", (country,))
    avail = c.fetchone()[0]
    c.execute("SELECT numbers FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    user_count = 0
    if row and row[0]:
        try:
            user_count = len(eval(row[0]))
        except Exception:
            user_count = 0
    conn.close()
    return avail, user_count

# -------------------------
# Bot & handlers
# -------------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_states = {}  # for withdraw flow
get_country_wait = {}  # temp storage for callback flow: user_id -> message to edit

# semaphore for background worker concurrency control
_worker_semaphore = asyncio.Semaphore(ASYNC_WORKERS)

# background worker: continuous poll for IVASMS messages and forward them
async def background_worker():
    print("[worker] Starting background worker loop.")
    # We will call GET_SMS_URL periodically via cloudscraper with session
    session = cloudscraper.create_scraper(browser='chrome')
    while True:
        # run scraping small dedicated fetch: reuse login_and_scrape_numbers to keep numbers in DB
        # But we also want to fetch OTP content for currently assigned numbers and forward them.
        try:
            # ensure logged in & sync numbers
            login_and_scrape_numbers()
            # Optionally, you could fetch per-number OTPs here using GET_OTP_URL
            # For this simplified worker: we will simply sleep then re-run scrape to keep DB fresh
        except Exception as e:
            print("[worker] exception:", e)
        await asyncio.sleep(30)  # repeat every 30s (tune as needed)

# --- Message handler for forwarded IVASMS group messages ---
@dp.message(F.chat.id == GROUP_ID)
async def handle_forwarded(m: types.Message):
    # parse number and otp robustly
    text = m.text or m.caption or ""
    if not text:
        return
    # try to find a number and otp
    number_m = re.search(r'Number:\s*(\+?\d{6,15})', text)
    otp_m = re.search(r'OTP Code:\s*(\d{4,8})', text)
    try:
        if number_m and otp_m:
            number = number_m.group(1)
            otp = otp_m.group(1)
        else:
            # fallback: find first long number and first 4-8 digit token
            number = re.search(r'(\+?\d{6,15})', text)
            otp = re.search(r'(\d{4,8})', text)
            if number: number = number.group(1)
            if otp: otp = otp.group(1)
            if not number or not otp:
                return
    except Exception:
        return

    if otp_exists(number, otp):
        return

    svc = detect_service(text)
    country = detect_country(number) if 'detect_country' in globals() else "UNKNOWN"
    save_otp(number, otp, text, svc, country)

    # notify group and owner
    # Format as requested:
    short_msg = (
        f"📱 <b>New OTP!</b> ✨\n"
        f"📞 <b>Number:</b> {mask_number_display(number)}\n"
        f"🌍 <b>Country:</b> {country}\n"
        f"🆔 <b>Provider:</b> {html.escape(svc)}\n"
        f"🔑 <b>OTP Code:</b> <code>{otp}</code>\n"
        f"📝 <b>Full Message:</b> {html.escape(text)}\n\n"
        f"<b>🎉 You have earned ৳{EARNINGS_PER_SMS:.4f} for this message!</b>"
    )
    # send to group (summary)
    try:
        await bot.send_message(GROUP_ID, short_msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        print("[forward] failed to send group message:", e)

    # credit user if number belongs to someone
    owner = get_user_by_number(number)
    if owner:
        credit_user(owner)
        try:
            await bot.send_message(owner, short_msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            print("[forward] failed to DM owner:", e)

# --- User commands & flows ---
@dp.message(F.text == "/start")
async def cmd_start(m: types.Message):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎁 Get Number", callback_data="get_number")],
        [types.InlineKeyboardButton(text="👤 Account", callback_data="account")],
        [types.InlineKeyboardButton(text="💰 Withdraw", callback_data="withdraw")]
    ])
    await m.answer(
        "👋 Welcome! Earn by receiving OTPs.\n\n"
        f"💰 Earn ৳{EARNINGS_PER_SMS:.4f} per OTP\n"
        "📱 Get a number → Use it → Get paid!",
        reply_markup=kb
    )

# Present country choices
@dp.callback_query(F.data == "get_number")
async def cb_get_number(q: types.CallbackQuery):
    # gather available countries
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT DISTINCT country FROM available_numbers")
    rows = c.fetchall()
    conn.close()
    countries = [r[0] for r in rows if r and r[0]]
    if not countries:
        # fallback: show single option unknown
        countries = ["UNKNOWN"]
    # build keyboard
    kb_rows = []
    for country in countries:
        kb_rows.append([types.InlineKeyboardButton(text=f"{country}", callback_data=f"choose_country:{country}")])
    await q.message.edit_text("🌍 Choose country for your number:", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
    get_country_wait[q.from_user.id] = q.message.message_id

@dp.callback_query(lambda c: c.data and c.data.startswith("choose_country:"))
async def cb_choose_country(q: types.CallbackQuery):
    parts = q.data.split(":", 1)
    if len(parts) < 2:
        await q.answer("Invalid selection.")
        return
    country = parts[1]
    # enforce cooldown & per-user max
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT last_assigned, numbers FROM users WHERE user_id = ?", (q.from_user.id,))
    row = c.fetchone()
    if row:
        last_assigned = row[0]
        numbers = []
        try:
            numbers = eval(row[1]) if row[1] else []
        except Exception:
            numbers = []
    else:
        last_assigned = None
        numbers = []

    if last_assigned:
        try:
            dt = datetime.fromisoformat(last_assigned)
            if datetime.now(timezone.utc) - dt < timedelta(seconds=COOLDOWN_SECONDS):
                remaining = COOLDOWN_SECONDS - int((datetime.now(timezone.utc) - dt).total_seconds())
                await q.answer(f"⏳ Cooldown: wait {remaining}s before requesting another number.", show_alert=True)
                return
        except Exception:
            pass

    if len(numbers) >= MAX_NUMBERS_PER_USER:
        await q.answer(f"⚠️ You already have {len(numbers)}/{MAX_NUMBERS_PER_USER} numbers.", show_alert=True)
        return

    # assign
    number = assign_number(q.from_user.id, country_preference=country)
    if not number:
        await q.message.edit_text("❌ No numbers available for that country right now. Try again later.")
        return

    # counts
    avail, user_count = counts_for(q.from_user.id, country)
    # produce formatted message
    with_cc = number if number.startswith("+") else "+" + number  # best-effort
    without_cc = number.lstrip("+")
    total_user = user_count
    msg = (
        f"✅ Number Assigned Successfully!\n"
        f"🌍 Country: {country}\n"
        f"📱 Number:\n"
        f"• with country code: <code>{with_cc}</code>\n"
        f"• without country code: <code>{without_cc}</code>\n"
        f"📊 Total Numbers: {total_user}/{MAX_NUMBERS_PER_USER}\n\n"
        f"📨 The OTP will be sent to our <a href=\"{GROUP_LINK}\">group</a> and your inbox.\n"
        f"💡 You can get more numbers after {COOLDOWN_SECONDS} seconds cooldown."
    )
    try:
        await q.message.edit_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        try:
            await q.answer("Assigned! Check your inbox.")
        except:
            pass

@dp.callback_query(F.data == "account")
async def cb_account(q: types.CallbackQuery):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT balance, numbers FROM users WHERE user_id = ?", (q.from_user.id,))
    row = c.fetchone()
    conn.close()
    balance = row[0] if row else 0.0
    numbers = []
    if row and row[1]:
        try:
            numbers = eval(row[1])
        except Exception:
            numbers = []
    nums = "\n".join([f"• <code>{n}</code>" for n in numbers]) if numbers else "None"
    await q.message.edit_text(
        f"👤 Your Account\n\n"
        f"💰 Balance: ৳{balance:.2f}\n"
        f"📱 Assigned Numbers:\n{nums}",
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "withdraw")
async def cb_withdraw(q: types.CallbackQuery):
    await q.message.edit_text("📲 Send your Bkash number (11 digits, e.g., 017XXXXXXXX):")
    user_states[q.from_user.id] = "awaiting_bkash"

@dp.message(F.text)
async def handle_text(m: types.Message):
    # withdrawal flow
    if user_states.get(m.from_user.id) == "awaiting_bkash":
        bkash = m.text.strip()
        if not (bkash.startswith("01") and len(bkash) == 11 and bkash.isdigit()):
            await m.answer("❌ Invalid Bkash number.")
            return
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (m.from_user.id,))
        row = c.fetchone()
        balance = row[0] if row else 0.0
        if balance < MIN_WITHDRAWAL:
            await m.answer(f"❌ Minimum withdrawal is ৳{MIN_WITHDRAWAL:.2f}")
            conn.close()
            return
        c.execute("INSERT INTO withdrawals (user_id, amount, bkash) VALUES (?, ?, ?)", (m.from_user.id, balance, bkash))
        c.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (m.from_user.id,))
        conn.commit()
        conn.close()
        await m.answer(f"✅ Withdrawal submitted!\nAmount: ৳{balance:.2f}\nBkash: {bkash}")
        user_states.pop(m.from_user.id, None)

# Admin commands
@dp.message(F.text == "/admin")
async def cmd_admin(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "🔐 Admin Panel\n\n"
        "🔄 /scrape — Sync numbers from IVASMS\n"
        "➕ /addnumber COUNTRY NUMBER — Add number manually\n"
        "✅ /approve <id> — Approve withdrawal\n"
        "📊 /stats — Quick stats"
    )

@dp.message(F.text == "/scrape")
async def cmd_scrape(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("🔄 Scraping IVASMS for numbers...")
    ok = login_and_scrape_numbers()
    await m.answer("✅ Numbers synced!" if ok else "❌ Scraping failed. Check logs.")

@dp.message(F.text.startswith("/addnumber"))
async def cmd_addnumber(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        parts = m.text.split()
        _, country, number = parts[:3]
        conn = get_connection()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO available_numbers (number, country) VALUES (?, ?)", (number, country))
        conn.commit()
        conn.close()
        await m.answer(f"✅ Added {number} ({country})")
    except Exception:
        await m.answer("Usage: /addnumber COUNTRY NUMBER")

@dp.message(F.text.startswith("/approve"))
async def cmd_approve(m: types.Message):
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
    except Exception:
        await m.answer("Usage: /approve <id>")

@dp.message(F.text == "/stats")
async def cmd_stats(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM available_numbers")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM available_numbers WHERE assigned_to IS NULL")
    free = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users")
    users = c.fetchone()[0]
    conn.close()
    await m.answer(f"📊 Stats\nTotal numbers: {total}\nAvailable: {free}\nRegistered users: {users}")

# -------------------------
# Startup + main
# -------------------------
async def on_startup():
    init_db()
    # start background worker
    loop = asyncio.get_event_loop()
    loop.create_task(background_worker())
    print("✅ Bot started and background worker launched.")

async def main():
    init_db()
    dp.startup.register(on_startup)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
