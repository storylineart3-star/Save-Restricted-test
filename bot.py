import os
import re
import time
import asyncio
import logging
from datetime import datetime

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

ADMIN_IDS = [123456789]

COOLDOWN = 15
AUTO_DELETE = 300
MAX_FILE_MB = 100

logging.basicConfig(level=logging.INFO)

# ===== MONGO =====
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["telegram_bot"]
users_col = db["users"]
sessions_col = db["sessions"]

# ===== MEMORY =====
clients = {}
last_used = {}
queue = asyncio.Queue()

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

# ===== LOGIN =====
PHONE, CODE, PASSWORD = range(3)

async def login_start(update, context):
    await update.message.reply_text("📱 Send phone number")
    return PHONE

async def login_phone(update, context):
    phone = update.message.text
    context.user_data["phone"] = phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(phone)

    context.user_data["client"] = client
    await update.message.reply_text("Enter OTP")
    return CODE

async def login_code(update, context):
    code = update.message.text.replace(" ", "")
    client = context.user_data["client"]
    phone = context.user_data["phone"]
    user_id = update.effective_user.id

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        await update.message.reply_text("Enter 2FA password")
        return PASSWORD

    session = client.session.save()
    await sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {"session": session}},
        upsert=True
    )

    clients[user_id] = client
    await users_col.update_one({"user_id": user_id}, {"$set": {}}, upsert=True)

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

# ===== QUEUE WORKER =====
async def worker():
    while True:
        update, context, text = await queue.get()
        user_id = update.effective_user.id

        msg = await update.message.reply_text("⏳ Processing...")

        try:
            client = await get_client(user_id)

            match = re.search(r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', text)
            chat = match.group(1)
            msg_id = int(match.group(2))

            entity = await client.get_entity(int(f"-100{chat}") if chat.isdigit() else chat)
            message = await client.get_messages(entity, ids=msg_id)

            if message.file and message.file.size > MAX_FILE_MB * 1024 * 1024:
                await msg.edit_text("❌ File too large")
                continue

            progress_msg = await update.message.reply_text("📥 Downloading... 0%")

            def progress(current, total):
                percent = int(current * 100 / total)
                asyncio.create_task(progress_msg.edit_text(f"📥 Downloading... {percent}%"))

            file = await client.download_media(message, progress_callback=progress)

            sent = await update.message.reply_document(open(file, "rb"))

            asyncio.create_task(auto_delete(context, sent.chat_id, sent.message_id, file))

            await msg.edit_text("✅ Done")

        except Exception as e:
            await msg.edit_text(f"Error: {str(e)}")

        queue.task_done()

# ===== AUTO DELETE =====
async def auto_delete(context, chat_id, msg_id, file):
    await asyncio.sleep(AUTO_DELETE)
    try:
        await context.bot.delete_message(chat_id, msg_id)
        os.remove(file)
    except:
        pass

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
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    loop = asyncio.get_event_loop()
    loop.create_task(worker())

    PORT = int(os.environ.get("PORT", 10000))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        url_path=BOT_TOKEN
    )

if __name__ == "__main__":
    main()
