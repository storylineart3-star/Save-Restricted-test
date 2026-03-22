import os
import re
import time
import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from motor.motor_asyncio import AsyncIOMotorClient

# ===== CONFIG =====
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MONGO_URI = os.getenv("MONGO_URI")

ADMIN_IDS = [2067674349]

COOLDOWN = 15
AUTO_DELETE = 300
MAX_FILE_MB = 100

logging.basicConfig(level=logging.INFO)

# ===== DB =====
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["telegram_bot"]
users_col = db["users"]
sessions_col = db["sessions"]

# ===== MEMORY =====
clients = {}
last_used = {}
queue = asyncio.Queue()
active_tasks = {}

# ===== GET CLIENT =====
async def get_client(user_id):
    if user_id in clients:
        return clients[user_id]

    data = await sessions_col.find_one({"user_id": user_id})
    if not data:
        return None

    client = TelegramClient(StringSession(data["session"]), API_ID, API_HASH)
    await client.connect()

    clients[user_id] = client
    return client

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await users_col.update_one({"user_id": update.effective_user.id}, {"$set": {}}, upsert=True)

    await update.message.reply_text(
        "👋 *Welcome to Restricted Content Saver Bot!*\n\n"
        "🔐 Use /login to connect your Telegram account\n"
        "📥 Send private/public message links\n\n"
        "⚠️ OTP Format:\n"
        "`1 2 3 4 5` (with spaces)\n\n"
        "ℹ️ Use /help for full guide",
        parse_mode="Markdown"
    )

# ===== HELP =====
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 *Full Guide*\n\n"
        "1️⃣ /login → login your account\n"
        "2️⃣ Send Telegram link\n\n"
        "⚠️ Rules:\n"
        f"- Cooldown: {COOLDOWN}s\n"
        f"- Max size: {MAX_FILE_MB}MB\n\n"
        "📌 Commands:\n"
        "/start - Start bot\n"
        "/login - Login account\n"
        "/cancel - Cancel current task\n"
        "/logout - Logout\n\n"
        "👑 Admins: No limits",
        parse_mode="Markdown"
    )

# ===== LOGIN =====
PHONE, CODE, PASSWORD = range(3)

async def login_start(update, context):
    await update.message.reply_text("📱 Send phone number with country code\nExample: +919999999999")
    return PHONE

async def login_phone(update, context):
    phone = update.message.text
    context.user_data["phone"] = phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(phone)

    context.user_data["client"] = client

    await update.message.reply_text(
        "🔢 Enter OTP like this:\n\n`1 2 3 4 5`\n\n⚠️ Spaces required",
        parse_mode="Markdown"
    )
    return CODE

async def login_code(update, context):
    code = update.message.text.replace(" ", "")
    client = context.user_data["client"]
    user_id = update.effective_user.id

    try:
        await client.sign_in(context.user_data["phone"], code)
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 Enter 2FA password")
        return PASSWORD

    session = client.session.save()

    await sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {"session": session}},
        upsert=True
    )

    clients[user_id] = client

    await update.message.reply_text("✅ Login successful!")
    return ConversationHandler.END

async def login_password(update, context):
    client = context.user_data["client"]
    user_id = update.effective_user.id

    await client.sign_in(password=update.message.text)

    session = client.session.save()
    await sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {"session": session}},
        upsert=True
    )

    clients[user_id] = client

    await update.message.reply_text("✅ Login successful!")
    return ConversationHandler.END

# ===== CANCEL =====
async def cancel(update, context):
    user_id = update.effective_user.id
    if user_id in active_tasks:
        active_tasks[user_id].cancel()
        await update.message.reply_text("❌ Task cancelled")
    else:
        await update.message.reply_text("No active task")

# ===== HANDLE =====
async def handle(update, context):
    user_id = update.effective_user.id
    text = update.message.text

    if user_id not in ADMIN_IDS:
        if user_id in last_used and time.time() - last_used[user_id] < COOLDOWN:
            await update.message.reply_text("⏳ Cooldown active")
            return

    last_used[user_id] = time.time()

    pos = queue.qsize() + 1
    await update.message.reply_text(f"📌 Added to queue. Position: {pos}")

    await queue.put((update, context, text))

# ===== WORKER =====
async def worker(app):
    while True:
        update, context, text = await queue.get()
        user_id = update.effective_user.id

        task = asyncio.current_task()
        active_tasks[user_id] = task

        status = await update.message.reply_text("⏳ Processing...")

        try:
            client = await get_client(user_id)

            if not client:
                await status.edit_text("⚠️ Please /login first")
                continue

            match = re.search(r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', text)
            if not match:
                await status.edit_text("❌ Invalid link")
                continue

            chat = match.group(1)
            msg_id = int(match.group(2))

            entity = await client.get_entity(int(f"-100{chat}") if chat.isdigit() else chat)
            message = await client.get_messages(entity, ids=msg_id)

            if message.file and message.file.size > MAX_FILE_MB * 1024 * 1024 and user_id not in ADMIN_IDS:
                await status.edit_text("❌ File too large")
                continue

            progress_msg = await update.message.reply_text("📥 Downloading... 0%")

            last_update = 0

            def progress(current, total):
                nonlocal last_update
                now = time.time()
                if now - last_update > 2:
                    percent = int(current * 100 / total)
                    asyncio.create_task(progress_msg.edit_text(f"📥 {percent}%"))
                    last_update = now

            file = await client.download_media(message, progress_callback=progress)

            if message.audio:
                sent = await update.message.reply_audio(open(file, "rb"))
            elif message.video:
                sent = await update.message.reply_video(open(file, "rb"))
            elif message.photo:
                sent = await update.message.reply_photo(open(file, "rb"))
            else:
                sent = await update.message.reply_document(open(file, "rb"))

            asyncio.create_task(auto_delete(context, sent.chat_id, sent.message_id, file))

            await status.edit_text("✅ Done")

        except asyncio.CancelledError:
            await status.edit_text("❌ Cancelled")
        except Exception as e:
            await status.edit_text(f"Error: {str(e)}")

        finally:
            active_tasks.pop(user_id, None)
            queue.task_done()

# ===== AUTO DELETE =====
async def auto_delete(context, chat_id, msg_id, file):
    await asyncio.sleep(AUTO_DELETE)
    try:
        await context.bot.delete_message(chat_id, msg_id)
        if os.path.exists(file):
            os.remove(file)
    except:
        pass

# ===== STATS =====
async def stats(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return
    total = await users_col.count_documents({})
    await update.message.reply_text(f"👥 Users: {total}")

# ===== BROADCAST =====
async def broadcast(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return

    msg = " ".join(context.args)
    users = users_col.find()

    sent = 0
    async for u in users:
        try:
            await context.bot.send_message(u["user_id"], msg)
            sent += 1
        except:
            pass

    await update.message.reply_text(f"Sent to {sent}")

# ===== MAIN =====
def main():

    async def post_init(app):
        app.create_task(worker(app))

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    PORT = int(os.environ.get("PORT", 10000))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        url_path=BOT_TOKEN
    )

if __name__ == "__main__":
    main()
