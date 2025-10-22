# main.py
import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from playwright.async_api import async_playwright, TimeoutError as PlayTimeoutError
from bs4 import BeautifulSoup
import html

# -------------------------
# CONFIG (ENV first)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # required
GROUP_ID = int(os.getenv("GROUP_ID", "0"))  # required - your IVASMS forward group id
GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/+NACzz1-K6dNiMjA9")

OWNER_NAME = os.getenv("OWNER_NAME", "Ivash")
EARNINGS_PER_SMS = float(os.getenv("EARNINGS_PER_SMS", "1.0"))
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "250.0"))
DB_FILE = os.getenv("DB_FILE", "ivasms.db")

# IVASMS
IVASMS_EMAIL = os.getenv("IVASMS_EMAIL")  # required
IVASMS_PASSWORD = os.getenv("IVASMS_PASSWORD")  # required
LOGIN_URL = os.getenv("LOGIN_URL", "https://www.ivasms.com/login")
BASE = os.getenv("BASE", "https://www.ivasms.com")
GET_SMS_URL = f"{BASE}/portal/sms/received/getsms"
GET_NUMBER_URL = f"{BASE}/portal/sms/received/getsms/number"
GET_OTP_URL = f"{BASE}/portal/sms/received/getsms/otps"

# behavior
ASYNC_WORKERS = int(os.getenv("ASYNC_WORKERS", "200"))  # concurrency control
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "10"))
MAX_NUMBERS_PER_USER = int(os.getenv("MAX_NUMBERS_PER_USER", "10"))
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "30"))  # seconds
STORAGE_FILE = os.getenv("PLAYWRIGHT_STORAGE", "storage.json")

# Validate required envs (fail early if missing)
if not BOT_TOKEN or not IVASMS_EMAIL or not IVASMS_PASSWORD or ADMIN_ID == 0 or GROUP_ID == 0:
    raise SystemExit("Please set BOT_TOKEN, IVASMS_EMAIL, IVASMS_PASSWORD, ADMIN_ID, GROUP_ID env vars before running.")

# -------------------------
# DB helpers
# -------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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
    return sqlite3.connect(DB_FILE, check_same_thread=False)

# -------------------------
# Playwright login & scraping (async)
# -------------------------
async def ensure_logged_in_and_get_page():
    """
    Returns a logged-in page object. Uses storage.json to persist session if available.
    """
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    storage = None
    if os.path.exists(STORAGE_FILE):
        # reuse storage by creating context with storage state file
        context = await browser.new_context(storage_state=STORAGE_FILE)
    else:
        context = await browser.new_context()

    page = await context.new_page()
    # If not logged in, go to login and perform login
    try:
        await page.goto(LOGIN_URL, timeout=20000)
    except PlayTimeoutError:
        print("[pw] Timeout loading login page.")
        # still attempt to continue
    # detect if already logged in (portal presence)
    page_text = await page.content()
    if "portal" in (page.url or "") or "portal" in page_text.lower() or "logout" in page_text.lower() or "dashboard" in page_text.lower():
        # good — likely logged in
        return playwright, browser, context, page

    # perform login
    try:
        # Fill form elements reliably
        # Try common selectors:
        await page.wait_for_selector('input[name="email"]', timeout=8000)
        await page.fill('input[name="email"]', IVASMS_EMAIL)
        await page.fill('input[name="password"]', IVASMS_PASSWORD)
        # click submit - try button[type=submit] or input[type=submit]
        try:
            await page.click('button[type="submit"]', timeout=3000)
        except Exception:
            try:
                await page.click('input[type="submit"]', timeout=3000)
            except Exception:
                # try pressing Enter in password field
                await page.press('input[name="password"]', "Enter")
        # wait for portal url or some indication of success
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlayTimeoutError:
        print("[pw] login selectors timeout; login may have failed.")
    except Exception as e:
        print("[pw] login exception:", e)

    # After login, save storage state
    try:
        # If login succeeded and page shows portal, save storage
        await context.storage_state(path=STORAGE_FILE)
    except Exception as e:
        print("[pw] storage save failed:", e)

    return playwright, browser, context, page

async def fetch_text_via_fetch_in_page(page, url, data):
    """
    Uses page.evaluate to run fetch() inside the browser context to send POST form data and return text.
    This is reliable as it includes cookies, CSRF, headers automatically.
    """
    script = """
    async (url, bodyObj) => {
        const form = new URLSearchParams();
        for (const k of Object.keys(bodyObj)) form.append(k, bodyObj[k]);
        const resp = await fetch(url, { method: 'POST', body: form, credentials: 'same-origin' });
        const text = await resp.text();
        return text;
    }
    """
    try:
        return await page.evaluate(script, url, data)
    except Exception as e:
        print("[pw] fetch_in_page failed:", e)
        return ""

async def playwright_scrape_numbers():
    """
    Logs in (if needed) and scrapes ranges and numbers via page-level fetches.
    Saves into available_numbers table.
    """
    playwright = None
    browser = None
    context = None
    page = None
    try:
        playwright, browser, context, page = await ensure_logged_in_and_get_page()
        # get fresh token from page DOM if present
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")
        token_input = soup.find("input", {"name": "_token"})
        csrf = token_input.get("value") if token_input and token_input.get("value") else ""
        # POST to GET_SMS_URL to retrieve ranges
        data = {"_token": csrf, "from": datetime.now(timezone.utc).date().isoformat(), "to": datetime.now(timezone.utc).date().isoformat()}
        txt = await fetch_text_via_fetch_in_page(page, GET_SMS_URL, data)
        # parse ranges
        ranges = []
        try:
            soup2 = BeautifulSoup(txt, "html.parser")
            opts = soup2.select("select#range option")
            for o in opts:
                v = o.get_text(strip=True)
                if v:
                    ranges.append(v)
        except Exception:
            pass
        if not ranges:
            ranges = [""]

        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM available_numbers")
        for rng in ranges:
            nd = {"_token": csrf, "start": datetime.now(timezone.utc).date().isoformat(), "end": datetime.now(timezone.utc).date().isoformat(), "range": rng}
            txt2 = await fetch_text_via_fetch_in_page(page, GET_NUMBER_URL, nd)
            numbers = re.findall(r"(?:\+?\d{6,15})", txt2)
            if not numbers:
                # try parsing JSON
                try:
                    j = json.loads(txt2)
                    if isinstance(j, list):
                        for item in j:
                            if isinstance(item, dict):
                                num = item.get("Number") or item.get("number") or item.get("msisdn")
                                if num:
                                    numbers.append(str(num))
                except Exception:
                    pass
            for num in numbers:
                try:
                    c.execute("INSERT OR IGNORE INTO available_numbers (number, country, range_info) VALUES (?, ?, ?)", (num, "UNKNOWN", rng))
                except Exception:
                    pass
        conn.commit()
        conn.close()
        print("[pw] scrape saved numbers.")
        # close browser objects lightly
        try:
            await context.storage_state(path=STORAGE_FILE)
        except Exception:
            pass
    except Exception as e:
        print("[pw] scrape error:", e)
    finally:
        try:
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()
        except Exception:
            pass

# -------------------------
# Utilities & DB ops
# -------------------------
def detect_service(text: str):
    t = (text or "").lower()
    services = {
        "whatsapp":"WhatsApp","facebook":"Facebook","telegram":"Telegram",
        "google":"Google","instagram":"Instagram","tiktok":"TikTok","apple":"Apple",
        "1xbet":"1xBet","melbet":"Melbet","exness":"Exness","wildberries":"Wildberries",
        "betwinner":"Betwinner","netflix":"Netflix"
    }
    for k in sorted(services.keys(), key=len, reverse=True):
        if k in t:
            return services[k]
    return "Service"

def mask_number(num: str):
    s = str(num)
    if len(s) <= 10:
        return s
    return s[:3] + "****" + s[-4:]

def otp_exists(number, otp):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM otps WHERE number=? AND otp=?", (number, otp))
    exists = c.fetchone() is not None
    conn.close()
    return exists

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
    conn = get_connection()
    c = conn.cursor()
    if country_preference and country_preference != "UNKNOWN":
        c.execute("SELECT number FROM available_numbers WHERE country = ? AND assigned_to IS NULL LIMIT 1", (country_preference,))
        row = c.fetchone()
        if not row:
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

def counts_for(user_id, country):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM available_numbers WHERE country = ? AND assigned_to IS NULL", (country,))
    try:
        avail = c.fetchone()[0]
    except:
        avail = 0
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
user_states = {}

# background task: periodically call playwright_scrape_numbers
async def background_scraper_loop():
    print("[bg] starting scraper loop")
    while True:
        try:
            await playwright_scrape_numbers()
        except Exception as e:
            print("[bg] scrape exception:", e)
        await asyncio.sleep(SCRAPE_INTERVAL)

# handle forwarded messages from IVASMS group (same as earlier)
@dp.message(F.chat.id == GROUP_ID)
async def handle_forwarded(m: types.Message):
    text = m.text or m.caption or ""
    if not text:
        return
    number_m = re.search(r'Number:\s*(\+?\d{6,15})', text)
    otp_m = re.search(r'OTP Code:\s*(\d{4,8})', text)
    try:
        if number_m and otp_m:
            number = number_m.group(1)
            otp = otp_m.group(1)
        else:
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
    country = "UNKNOWN"
    save_otp(number, otp, text, svc, country)

    short_msg = (
        f"📱 <b>New OTP!</b> ✨\n"
        f"📞 <b>Number:</b> {mask_number(number)}\n"
        f"🌍 <b>Country:</b> {country}\n"
        f"🆔 <b>Provider:</b> {html.escape(svc)}\n"
        f"🔑 <b>OTP Code:</b> <code>{otp}</code>\n"
        f"📝 <b>Full Message:</b> {html.escape(text)}\n\n"
        f"<b>🎉 You have earned ৳{EARNINGS_PER_SMS:.4f} for this message!</b>"
    )

    try:
        await bot.send_message(GROUP_ID, short_msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        print("[forward] group send failed:", e)

    owner = get_user_by_number(number)
    if owner:
        credit_user(owner)
        try:
            await bot.send_message(owner, short_msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            print("[forward] DM failed:", e)

# basic user commands
@dp.message(F.text == "/start")
async def cmd_start(m: types.Message):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("🎁 Get Number", callback_data="get_number")],
        [types.InlineKeyboardButton("👤 Account", callback_data="account")],
        [types.InlineKeyboardButton("💰 Withdraw", callback_data="withdraw")]
    ])
    await m.answer(f"👋 Welcome — earn ৳{EARNINGS_PER_SMS:.4f} per OTP.", reply_markup=kb)

@dp.callback_query(F.data == "get_number")
async def cb_get_number(q: types.CallbackQuery):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT DISTINCT country FROM available_numbers")
    rows = c.fetchall()
    conn.close()
    countries = [r[0] for r in rows if r and r[0]]
    if not countries:
        countries = ["UNKNOWN"]
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ctry in countries:
        kb.add(types.InlineKeyboardButton(text=ctry, callback_data=f"choose_country:{ctry}"))
    await q.message.edit_text("🌍 Choose a country:", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("choose_country:"))
async def cb_choose_country(q: types.CallbackQuery):
    country = q.data.split(":",1)[1]
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT last_assigned, numbers FROM users WHERE user_id = ?", (q.from_user.id,))
    row = c.fetchone()
    if row:
        last_assigned = row[0]
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
                remaining = max(1, COOLDOWN_SECONDS - int((datetime.now(timezone.utc) - dt).total_seconds()))
                await q.answer(f"⏳ Cooldown: wait {remaining}s", show_alert=True)
                return
        except Exception:
            pass

    if len(numbers) >= MAX_NUMBERS_PER_USER:
        await q.answer(f"⚠️ You already have {len(numbers)}/{MAX_NUMBERS_PER_USER} numbers", show_alert=True)
        return

    number = assign_number(q.from_user.id, country_preference=country)
    if not number:
        await q.message.edit_text("❌ No numbers available for that country right now.")
        return

    avail, user_count = counts_for(q.from_user.id, country)
    with_cc = number if number.startswith("+") else "+" + number
    without_cc = number.lstrip("+")
    msg = (
        f"✅ Number Assigned Successfully!\n"
        f"🌍 Country: {country}\n"
        f"📱 Number:\n• with country code: <code>{with_cc}</code>\n• without country code: <code>{without_cc}</code>\n"
        f"📊 Total Numbers: {user_count}/{MAX_NUMBERS_PER_USER}\n\n"
        f"📨 The OTP will be sent to our <a href=\"{GROUP_LINK}\">group</a> and your inbox.\n"
        f"💡 You can get more numbers after {COOLDOWN_SECONDS} seconds cooldown."
    )
    try:
        await q.message.edit_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await q.answer("Assigned! Check your messages.")

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
    await q.message.edit_text(f"👤 Your Account\n\n💰 Balance: ৳{balance:.2f}\n📱 Assigned Numbers:\n{nums}", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "withdraw")
async def cb_withdraw(q: types.CallbackQuery):
    await q.message.edit_text("📲 Send your Bkash number (11 digits, e.g., 017XXXXXXXX):")
    user_states[q.from_user.id] = "awaiting_bkash"

@dp.message(F.text)
async def handle_text(m: types.Message):
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

# admin panel with Add User
@dp.message(F.text == "/admin")
async def cmd_admin(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("🔄 Scrape", callback_data="admin:scrape")],
        [types.InlineKeyboardButton("➕ Add Number", callback_data="admin:addnumber")],
        [types.InlineKeyboardButton("➕ Add User", callback_data="admin:adduser")],
        [types.InlineKeyboardButton("✅ Approve", callback_data="admin:approve")],
        [types.InlineKeyboardButton("📊 Stats", callback_data="admin:stats")]
    ])
    await m.answer("🔐 Admin Panel", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("admin:"))
async def admin_callbacks(q: types.CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        return
    cmd = q.data.split(":",1)[1]
    if cmd == "scrape":
        await q.answer("Scraping started...")
        await q.message.reply("🔄 Running playwright scrape (this may take a few seconds)...")
        ok = await asyncio.to_thread(lambda: asyncio.run(playwright_scrape_numbers_sync_wrapper()))
        # above wrapper executes the sync wrapper to call playwright from thread
        await q.message.answer("✅ Numbers synced!" if ok else "❌ Scraping failed.")
    elif cmd == "addnumber":
        await q.message.answer("Send text: COUNTRY NUMBER (e.g. NIGERIA +2347055...)")
        user_states[q.from_user.id] = "admin_adding_number"
    elif cmd == "adduser":
        await q.message.answer("Send Telegram user id to add (integer).")
        user_states[q.from_user.id] = "admin_adding_user"
    elif cmd == "approve":
        await q.message.answer("Usage: /approve <withdrawal_id>")
    elif cmd == "stats":
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM available_numbers")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM available_numbers WHERE assigned_to IS NULL")
        free = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users")
        users = c.fetchone()[0]
        conn.close()
        await q.message.answer(f"📊 Stats\nTotal numbers: {total}\nAvailable: {free}\nUsers: {users}")

# admin text flows
@dp.message()
async def admin_text_handler(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    state = user_states.get(m.from_user.id)
    if state == "admin_adding_number":
        try:
            parts = m.text.strip().split()
            country = parts[0]
            number = parts[1]
            conn = get_connection()
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO available_numbers (number, country) VALUES (?, ?)", (number, country))
            conn.commit()
            conn.close()
            await m.answer(f"✅ Added number {number} as {country}")
        except Exception:
            await m.answer("Format: COUNTRY NUMBER (e.g. NIGERIA +2347055...)")
        user_states.pop(m.from_user.id, None)
    elif state == "admin_adding_user":
        try:
            uid = int(m.text.strip())
            conn = get_connection()
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO users (user_id, numbers, balance) VALUES (?, ?, ?)", (uid, "[]", 0.0))
            conn.commit()
            conn.close()
            await m.answer(f"✅ Added user {uid}")
        except Exception:
            await m.answer("Please send a valid integer Telegram user id.")
        user_states.pop(m.from_user.id, None)

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

# -------------------------
# Playwright sync wrapper (for use in threads)
# -------------------------
def playwright_scrape_numbers_sync_wrapper():
    """
    A small wrapper so admin button can call scraping from threadpool easily.
    This starts an asyncio loop to call the async scraper.
    """
    import asyncio
    return asyncio.run(playwright_scrape_numbers())

# -------------------------
# Startup
# -------------------------
async def on_startup():
    init_db()
    # start background scraper loop
    asyncio.create_task(background_scraper_loop())
    print("Bot started. Background scraper running.")

async def main():
    dp.startup.register(on_startup)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
