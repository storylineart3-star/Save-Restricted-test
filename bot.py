import os
import re
import time
import asyncio
import logging
from typing import Optional, Dict, List

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from motor.motor_asyncio import AsyncIOMotorClient

# ===== CONFIG =====
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MONGO_URI = os.getenv("MONGO_URI")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "123456789").split(",")))
COOLDOWN = 10
AUTO_DELETE = 300
MAX_FILE_MB = 50
MAX_CONCURRENT_TASKS = 3  # Process up to 3 files simultaneously

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== DB =====
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["telegram_bot"]
users_col = db["users"]
sessions_col = db["sessions"]

# ===== MEMORY =====
clients: Dict[int, TelegramClient] = {}
last_used: Dict[int, float] = {}

# Queue system with concurrency limit
task_queue = asyncio.Queue()
active_tasks: Dict[int, asyncio.Task] = {}
queue_order: List[int] = []  # order of user_ids waiting
user_queues: Dict[int, int] = {}  # user_id -> position
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# ===== CLIENT MANAGEMENT =====
async def get_client(user_id: int) -> Optional[TelegramClient]:
    if user_id in clients:
        return clients[user_id]

    data = await sessions_col.find_one({"user_id": user_id})
    if not data:
        return None

    client = TelegramClient(StringSession(data["session"]), API_ID, API_HASH)
    await client.connect()
    clients[user_id] = client
    return client

# ===== COMMAND HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await users_col.update_one({"user_id": update.effective_user.id}, {"$set": {}}, upsert=True)
    await update.message.reply_text(
        "👋 **Welcome to the Restricted Content Bot!**\n\n"
        "🔐 Use `/login` to connect your Telegram account\n"
        "📥 Send any Telegram message link\n\n"
        "⚠️ OTP format: `1 2 3 4 5` (with spaces)\n\n"
        "ℹ️ Use `/help` for full guide",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 **GUIDE**\n\n"
        "1. `/login` → connect your Telegram account\n"
        "2. Send any **Telegram message link** (public or private)\n\n"
        "⚠️ **Limits:**\n"
        "- Max file size: **50 MB** (free users)\n"
        "- Cooldown: **10 seconds** between requests\n"
        "- Auto-delete after **5 minutes**\n\n"
        "📌 **Commands:**\n"
        "`/start` `/help` `/login` `/cancel` `/status`\n\n"
        "👑 **Admins** have no limits and bypass cooldown.",
        parse_mode="Markdown"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pos = user_queues.get(user_id)
    if pos is None:
        await update.message.reply_text("📭 You have no pending tasks.")
    else:
        await update.message.reply_text(f"🔄 Your task is at position **{pos}** in the queue.", parse_mode="Markdown")

# ===== LOGIN CONVERSATION =====
PHONE, CODE, PASSWORD = range(3)

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Send your phone number with country code.\nExample: `+919999999999`", parse_mode="Markdown")
    return PHONE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    context.user_data["phone"] = phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(phone)

    context.user_data["client"] = client

    await update.message.reply_text(
        "🔢 Enter the OTP like: `1 2 3 4 5`\n(Spaces are required)",
        parse_mode="Markdown"
    )
    return CODE

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.replace(" ", "")
    client = context.user_data["client"]
    user_id = update.effective_user.id

    try:
        await client.sign_in(context.user_data["phone"], code)
    except SessionPasswordNeededError:
        await update.message.reply_text("🔑 Enter your 2FA password:")
        return PASSWORD

    session = client.session.save()
    await sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {"session": session}},
        upsert=True
    )
    clients[user_id] = client
    await update.message.reply_text("✅ **Login successful!** You can now use the bot.", parse_mode="Markdown")
    return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text("✅ **Login successful!**", parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the user's current task (if any)."""
    user_id = update.effective_user.id
    task = active_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("❌ Task cancelled.")
        # Remove from queue tracking
        if user_id in queue_order:
            queue_order.remove(user_id)
        user_queues.pop(user_id, None)
        # Recalculate positions for remaining users
        _update_queue_positions()
    else:
        await update.message.reply_text("ℹ️ No active task to cancel.")

def _update_queue_positions():
    """Recalculate positions for all users in the queue."""
    for idx, uid in enumerate(queue_order, start=1):
        user_queues[uid] = idx

# ===== QUEUE WORKER =====
async def worker():
    """Process tasks from the queue with concurrency limit."""
    while True:
        task_info = await task_queue.get()
        user_id = task_info["user_id"]
        update_obj = task_info["update"]
        context = task_info["context"]
        client = task_info["client"]
        message = task_info["message"]
        link = task_info["link"]

        # Acquire semaphore to limit concurrency
        async with semaphore:
            # Create the processing coroutine
            async def process():
                try:
                    await process_message(user_id, update_obj, context, client, message, link)
                except asyncio.CancelledError:
                    await update_obj.message.reply_text("❌ Task cancelled.")
                except Exception as e:
                    logger.exception("Error processing task for user %s", user_id)
                    await update_obj.message.reply_text(f"❌ Error: {str(e)}")
                finally:
                    # Clean up
                    active_tasks.pop(user_id, None)
                    if user_id in queue_order:
                        queue_order.remove(user_id)
                    _update_queue_positions()

            task = asyncio.create_task(process())
            active_tasks[user_id] = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                task_queue.task_done()

async def enqueue_task(user_id: int, update_obj: Update, context: ContextTypes.DEFAULT_TYPE,
                       client: TelegramClient, message: Update.message, link: str) -> int:
    """Add a task to the queue and return the position."""
    queue_order.append(user_id)
    pos = len(queue_order)
    user_queues[user_id] = pos
    await task_queue.put({
        "user_id": user_id,
        "update": update_obj,
        "context": context,
        "client": client,
        "message": message,
        "link": link
    })
    return pos

async def process_message(user_id: int, update_obj: Update, context: ContextTypes.DEFAULT_TYPE,
                          client: TelegramClient, message: Update.message, link: str):
    """Actual download and send logic."""
    progress_msg = await update_obj.message.reply_text("🔍 Fetching message...")
    try:
        # Parse link
        match = re.search(r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', link)
        if not match:
            await progress_msg.edit_text("❌ Invalid link format.")
            return
        chat_part = match.group(1)
        msg_id = int(match.group(2))

        # Resolve entity
        if chat_part.isdigit():
            entity = await client.get_entity(int(f"-100{chat_part}"))
        else:
            entity = await client.get_entity(chat_part)

        msg = await client.get_messages(entity, ids=msg_id)
        if not msg:
            await progress_msg.edit_text("❌ Message not found.")
            return

        # Text-only message
        if msg.text and not msg.media:
            await progress_msg.delete()
            await update_obj.message.reply_text(msg.text)
            return

        # File size check (for non-admins)
        if msg.file and user_id not in ADMIN_IDS:
            size_mb = msg.file.size / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                await progress_msg.edit_text(f"❌ File too large ({size_mb:.1f} MB). Max {MAX_FILE_MB} MB.")
                return

        # Download with progress
        await progress_msg.edit_text("📥 Downloading... 0%")
        last_update = 0

        def progress_callback(current, total):
            nonlocal last_update
            now = time.time()
            if now - last_update >= 2 and total > 0:
                percent = int(current * 100 / total)
                # Schedule the edit in the event loop (safe from threads)
                asyncio.create_task(progress_msg.edit_text(f"📥 Downloading... {percent}%"))
                last_update = now

        file_path = await client.download_media(msg, progress_callback=progress_callback)

        # Upload to Telegram
        await progress_msg.edit_text("📤 Uploading...")
        with open(file_path, "rb") as f:
            if msg.audio:
                sent = await update_obj.message.reply_audio(f, caption=msg.text if msg.text else None)
            elif msg.video:
                sent = await update_obj.message.reply_video(f, caption=msg.text if msg.text else None)
            elif msg.photo:
                sent = await update_obj.message.reply_photo(f, caption=msg.text if msg.text else None)
            else:
                sent = await update_obj.message.reply_document(f, caption=msg.text if msg.text else None)

        # Schedule auto-delete of the sent message and local file
        asyncio.create_task(auto_delete(context, sent.chat_id, sent.message_id, file_path))

        await progress_msg.delete()
        await update_obj.message.reply_text("✅ Done!")

    except Exception as e:
        logger.exception("Error processing message for user %s", user_id)
        await progress_msg.edit_text(f"❌ Error: {str(e)}")
        raise

# ===== AUTO DELETE =====
async def auto_delete(context, chat_id, msg_id, file_path):
    await asyncio.sleep(AUTO_DELETE)
    try:
        await context.bot.delete_message(chat_id, msg_id)
    except Exception:
        pass
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass

# ===== MAIN HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # Check if it's a link
    if "t.me" not in text:
        await update.message.reply_text("❌ Please send a valid Telegram message link.")
        return

    # Cooldown (skip for admins)
    if user_id not in ADMIN_IDS:
        now = time.time()
        if user_id in last_used and now - last_used[user_id] < COOLDOWN:
            remaining = int(COOLDOWN - (now - last_used[user_id]))
            await update.message.reply_text(f"⏳ Please wait {remaining} seconds before sending another link.")
            return
        last_used[user_id] = now

    # Check if user is logged in
    client = await get_client(user_id)
    if not client:
        await update.message.reply_text("⚠️ You need to login first. Use /login")
        return

    # Check if user already has a pending task
    if user_id in active_tasks and not active_tasks[user_id].done():
        await update.message.reply_text("⏳ You already have a task in progress. Please wait or use /cancel.")
        return

    # Add to queue
    pos = await enqueue_task(user_id, update, context, client, update.message, text)
    await update.message.reply_text(f"📥 Added to queue at position {pos}. You will be notified when processing starts.")

# ===== START WORKER =====
async def post_init(application: Application):
    """Start the queue worker after the application is ready."""
    asyncio.create_task(worker())

# ===== MAIN =====
def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Conversation handler for login
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
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    PORT = int(os.environ.get("PORT", 10000))

    # Use webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        url_path=BOT_TOKEN
    )

if __name__ == "__main__":
    main()
