import asyncio
import re
import sqlite3
import logging
import os

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

# ---------- ENV VARIABLES ----------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# ----------------------------------

logging.basicConfig(level=logging.INFO)

DB_PATH = "sessions.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (user_id INTEGER PRIMARY KEY, session_string TEXT)''')
    conn.commit()
    conn.close()

def get_session_string(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_string FROM sessions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_session_string(user_id, session_string):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO sessions (user_id, session_string) VALUES (?, ?)",
              (user_id, session_string))
    conn.commit()
    conn.close()

clients = {}

def get_client(user_id):
    if user_id in clients:
        return clients[user_id]
    session_str = get_session_string(user_id)
    if session_str:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        clients[user_id] = client
        return client
    return None

PHONE, CODE, PASSWORD = range(3)

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send phone number with country code:")
    return PHONE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    context.user_data['phone'] = phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    context.user_data['temp_client'] = client

    await client.connect()
    await client.send_code_request(phone)

    await update.message.reply_text("Enter OTP (with spaces like 1 2 3 4 5):")
    return CODE

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.replace(" ", "")
    client = context.user_data['temp_client']
    phone = context.user_data['phone']
    user_id = update.effective_user.id

    try:
        await client.sign_in(phone, code)
        session_str = client.session.save()
        save_session_string(user_id, session_str)
        clients[user_id] = client

        await update.message.reply_text("✅ Login successful!")
        return ConversationHandler.END

    except SessionPasswordNeededError:
        await update.message.reply_text("Enter 2FA password:")
        return PASSWORD

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text
    client = context.user_data['temp_client']
    user_id = update.effective_user.id

    await client.sign_in(password=password)
    session_str = client.session.save()
    save_session_string(user_id, session_str)
    clients[user_id] = client

    await update.message.reply_text("✅ Login successful!")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled")
    return ConversationHandler.END

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in clients:
        await clients[user_id].disconnect()
        del clients[user_id]
    save_session_string(user_id, None)
    await update.message.reply_text("Logged out")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    match = re.search(r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', text)
    if not match:
        await update.message.reply_text("Send valid Telegram link")
        return

    client = get_client(user_id)
    if not client:
        await update.message.reply_text("Login first using /login")
        return

    chat_username = match.group(1)
    msg_id = int(match.group(2))

    await update.message.reply_text("Fetching...")

    try:
        if not client.is_connected():
            await client.connect()

        if chat_username.isdigit():
            entity = await client.get_entity(int(f"-100{chat_username}"))
        else:
            entity = await client.get_entity(chat_username)

        message = await client.get_messages(entity, ids=msg_id)

        if message.text:
            await update.message.reply_text(message.text[:4000])
        elif message.photo:
            file = await client.download_media(message, file=bytes)
            await update.message.reply_photo(photo=file)
        elif message.video:
            file = await client.download_media(message, file=bytes)
            await update.message.reply_video(video=file)
        elif message.document:
            file = await client.download_media(message, file=bytes)
            await update.message.reply_document(document=file)

    except FloodWaitError as e:
        await update.message.reply_text(f"Wait {e.seconds} seconds")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot started. Use /login")

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    PORT = int(os.environ.get("PORT", 10000))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
