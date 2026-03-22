import asyncio
import re
import sqlite3
import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat

# ---------- YOUR CREDENTIALS ----------
API_ID = 31857928
API_HASH = "ded3b00db58fbb2b06c6b3c02ace6243"
BOT_TOKEN = "7779675802:AAFqPxIqkmekLkZ8cbCQHU_5tXc44tfWLfQ"
# ------------------------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
    c.execute("SELECT session_string FROM sessions WHERE user_id = ?", (user_id,))
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

# Global dict for active clients
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

# Conversation states
PHONE, CODE, PASSWORD = range(3)

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("login_start called")
    await update.message.reply_text(
        "🔐 *Login to Telegram*\n\n"
        "Please send your phone number with country code.\n"
        "Example: `+1234567890`\n\n"
        "To cancel, use /cancel",
        parse_mode='Markdown'
    )
    return PHONE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("login_phone called")
    phone = update.message.text
    context.user_data['phone'] = phone
    user_id = update.effective_user.id

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    context.user_data['temp_client'] = client

    try:
        await client.connect()
        await client.send_code_request(phone)
        await update.message.reply_text(
            "📱 *Verification Code Sent*\n\n"
            "Please enter the code you received.\n"
            "⚠️ *Important:* Enter it with spaces between digits.\n"
            "Example: `1 2 3 4 5`\n\n"
            "To cancel, use /cancel",
            parse_mode='Markdown'
        )
        return CODE
    except Exception as e:
        logging.exception("Error during login_phone")
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return ConversationHandler.END

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("login_code called")
    # Remove spaces from the code
    code = update.message.text.replace(" ", "")
    client = context.user_data['temp_client']
    phone = context.user_data['phone']
    user_id = update.effective_user.id

    try:
        await client.sign_in(phone, code)
        session_str = client.session.save()
        save_session_string(user_id, session_str)
        clients[user_id] = client
        await update.message.reply_text(
            "✅ *Login Successful!*\n\n"
            "You can now send me Telegram message links from private channels/groups.\n\n"
            "Example: `https://t.me/channelname/123`",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 *Two‑Factor Authentication*\n\n"
            "Please enter your 2FA password.",
            parse_mode='Markdown'
        )
        return PASSWORD
    except Exception as e:
        logging.exception("Error during login_code")
        await update.message.reply_text(f"❌ Login failed: {str(e)}")
        return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("login_password called")
    password = update.message.text
    client = context.user_data['temp_client']
    phone = context.user_data['phone']
    user_id = update.effective_user.id

    try:
        await client.sign_in(password=password)
        session_str = client.session.save()
        save_session_string(user_id, session_str)
        clients[user_id] = client
        await update.message.reply_text(
            "✅ *Login Successful!*\n\n"
            "You can now send me Telegram message links from private channels/groups.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    except Exception as e:
        logging.exception("Error during login_password")
        await update.message.reply_text(f"❌ Login failed: {str(e)}")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Login cancelled.")
    return ConversationHandler.END

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in clients:
        await clients[user_id].disconnect()
        del clients[user_id]
    save_session_string(user_id, None)
    await update.message.reply_text("✅ You have been logged out.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("handle_message called")
    user_id = update.effective_user.id
    text = update.message.text

    # Check if it's a Telegram link
    # Pattern matches: https://t.me/channelname/123 OR https://t.me/c/1619215255/123
    match = re.search(r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', text)
    if not match:
        await update.message.reply_text(
            "📎 *Send me a Telegram message link*\n\n"
            "Examples:\n"
            "`https://t.me/channelname/123`\n"
            "`https://t.me/c/1619215255/123`",
            parse_mode='Markdown'
        )
        return

    # Ensure user is logged in
    client = get_client(user_id)
    if not client:
        await update.message.reply_text(
            "❌ You are not logged in.\n\n"
            "Please use /login first.",
            parse_mode='Markdown'
        )
        return

    chat_username = match.group(1)
    msg_id = int(match.group(2))

    await update.message.reply_text("⏳ Fetching message...")

    try:
        # Connect if not already
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            await update.message.reply_text("❌ Session expired. Please /login again.")
            return

        # Fix: Handle both channel IDs and usernames correctly
        try:
            # If it's a numeric ID (like from t.me/c/1619215255), it's a channel ID
            if chat_username.isdigit():
                # Channel IDs in t.me/c/ format are positive numbers but Telethon needs negative IDs
                # The format t.me/c/1619215255/123 means channel ID is 1619215255 (positive)
                # But Telethon uses negative IDs for channels: -1001619215255
                # Let's try both formats
                try:
                    # Try as negative ID (channel)
                    entity = await client.get_entity(int(f"-100{chat_username}"))
                except:
                    # Try as positive ID
                    entity = await client.get_entity(int(chat_username))
            else:
                # It's a username
                entity = await client.get_entity(chat_username)
        except Exception as e:
            logging.exception("Entity resolution failed")
            await update.message.reply_text(
                f"❌ Cannot access this chat.\n"
                f"Make sure:\n"
                f"1. You are a member of this channel/group\n"
                f"2. The link is correct\n"
                f"3. The channel/group exists\n\n"
                f"Error: {str(e)}"
            )
            return

        # Get the message
        message = await client.get_messages(entity, ids=msg_id)
        if message is None:
            await update.message.reply_text(
                "❌ Message not found.\n\n"
                "Make sure:\n"
                "1. The message ID is correct\n"
                "2. You have access to this message"
            )
            return

        # Send content
        if message.text:
            # Truncate very long messages
            if len(message.text) > 4000:
                await update.message.reply_text(message.text[:4000] + "\n\n... (message truncated)")
            else:
                await update.message.reply_text(message.text)
        else:
            # Media handling
            if message.photo:
                file = await client.download_media(message, file=bytes)
                await update.message.reply_photo(photo=file)
            elif message.video:
                file = await client.download_media(message, file=bytes)
                await update.message.reply_video(video=file)
            elif message.document:
                file = await client.download_media(message, file=bytes)
                await update.message.reply_document(document=file)
            else:
                await client.forward_messages('me', message)
                await update.message.reply_text("⚠️ Unsupported media type. Forwarded to your saved messages.")
    except FloodWaitError as e:
        await update.message.reply_text(f"⏳ Too many requests. Please wait {e.seconds} seconds.")
    except Exception as e:
        logging.exception("Error fetching message")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Welcome to Restricted Content Saver Bot!*\n\n"
        "This bot allows you to fetch messages from private Telegram channels/groups.\n\n"
        "*Commands:*\n"
        "/login - Log in with your Telegram account\n"
        "/logout - Log out\n"
        "/cancel - Cancel login process\n\n"
        "*How to use:*\n"
        "1. Use /login to authenticate\n"
        "2. Send a message link (e.g., https://t.me/channelname/123)\n"
        "3. The bot will fetch and send the message to you\n\n"
        "⚠️ *Important:* The bot uses YOUR Telegram account to access channels, so you must be a member of any channel you want to fetch from.",
        parse_mode='Markdown'
    )

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for login
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('login', login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("🤖 Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
