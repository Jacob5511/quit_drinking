import os
import logging
import httpx
import uuid
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
ADMIN_ID = int(os.environ["ADMIN_ID"])

banned_cache = {}  # msg_id -> {chat_id, user_id, username, text}

WELCOME_MESSAGE = """
Зарегистрироваться на мастер-класс можно здесь:
https://bothelp.cc/mini?domain=allencarrlife&id=3
""".strip()

SPAM_KEYWORDS = [
    "аработок", "заработок", "заработка", "доп. доход", "доход",
    "$", "usd", "баксы", "баксов",
    "удаленная занятость", "удалёнка", "удаленную", "удалённой",
    "способ заработка", "бизнес-предложение", "бизнес предложение",
    "вакансии", "от 18 лет", "2-3 часа", "3 человека",
    "строго", "оплат", "предлагаю",
    "remote", "work from home"
]

SPAM_CHECK_PROMPT = """
You are a Telegram moderation filter in a russian-speaking group chat.

Your only job is to detect EXTERNAL ADVERTISING or SELLING.

You are NOT a general spam detector.
--------------------------------

Message:
\"\"\"{message}\"\"\"

--------------------------------
BLOCK (SPAM) ONLY IF:

1. Selling or promoting anything:
- "buy this", "for sale", "selling"
- offers of services or products

2. Asking users to contact privately:
- "DM me", "message me", "write me privately"

3. Advertising or scams:
- links to channels, groups, bots, websites
- crypto, investments, jobs, income schemes

4. If the message contains any of these:
    "аработок", "заработок", "заработка", "доп. доход", "доход",
    "$", "usd", "баксы", "баксов",
    "удаленная занятость", "удалёнка", "удаленную", "удалённой",
    "способ заработка", "бизнес-предложение", "бизнес предложение",
    "вакансии", "от 18 лет", "2-3 часа", "3 человека",
    "строго", "оплат", "предлагаю",
    "remote", "work from home"

--------------------------------
ALLOW (LEGITIMATE):

Everything else, including:
- greetings ("hello", "hi", "привет")
- random messages
- questions
- insults (do NOT block for insults)
- questions about the course
- price questions
- "what is this course?"
- "how much does it cost?"
- any curiosity about the product

IMPORTANT RULE:

- If the message is NOT clearly advertising or selling → LEGITIMATE
- If you are unsure → LEGITIMATE
- Never block questions about the course or pricing

--------------------------------
OUTPUT FORMAT:

Return a word:
SPAM or LEGITIMATE
"""


def rule_based_spam(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in SPAM_KEYWORDS)


async def is_spam(text: str) -> bool:
    prompt = SPAM_CHECK_PROMPT.format(message=text[:1000])
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0},
            },
        )
        response.raise_for_status()
        raw = response.json().get("response", "").strip().upper()
        logger.info(f"Ollama raw output: {raw!r}")
        return "SPAM" in raw


async def mute_and_notify(context: ContextTypes.DEFAULT_TYPE, message: Message):
    from telegram import ChatPermissions
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    user_id = message.from_user.id
    chat_id = message.chat_id
    deleted_text = message.text

    msg_id = str(uuid.uuid4())[:8]
    banned_cache[msg_id] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "username": username,
        "text": deleted_text,
    }

    await message.delete()

    # Mute: remove all send permissions
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=ChatPermissions(
            can_send_messages=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
        ),
    )
    logger.info(f"Muted {username} ({user_id}) for spam: {deleted_text[:80]}")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Keep muted", callback_data=f"keep|{msg_id}"),
            InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute|{msg_id}"),
        ]
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🔇 Muted {username} for spam.\n\nTheir message:\n{deleted_text}",
        reply_markup=keyboard,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.message
    if not message or not message.text:
        return

    user_id = message.from_user.id
    chat_id = message.chat_id

    # IGNORE ADMINS
    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status in ["administrator", "creator"]:
        print(member.status)
        return

    text = message.text.strip()

    # "+" trigger
    if text == "+":
        await message.reply_text(WELCOME_MESSAGE)
        return

    # Skip bots
    if message.from_user.is_bot:
        return

    # Only moderate group chats
    if message.chat.type not in ("group", "supergroup"):
        return

    # Layer 1: fast keyword check
    if rule_based_spam(text):
        await mute_and_notify(context, message)
        return

    # # Layer 2: Ollama check
    # if await is_spam(text):
    #     await mute_and_notify(context, message)
    #     return


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import ChatPermissions
    query = update.callback_query
    await query.answer()

    action, msg_id = query.data.split("|")
    stored = banned_cache.pop(msg_id, None)

    if not stored:
        await query.edit_message_text(query.message.text + "\n\n⚠️ Already handled.")
        return

    if action == "keep":
        await query.edit_message_text(query.message.text + "\n\n✅ Stays muted.")

    elif action == "unmute":
        try:
            await context.bot.restrict_chat_member(
                chat_id=stored["chat_id"],
                user_id=stored["user_id"],
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )
            await query.edit_message_text(
                query.message.text + f"\n\n🔊 {stored['username']} was unmuted."
            )
        except Exception as e:
            await query.edit_message_text(query.message.text + f"\n\n❌ Unmute failed: {e}")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()