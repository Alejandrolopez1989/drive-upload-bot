import logging
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f'Hello {update.effective_user.first_name}')

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message else update.channel_post.text
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

if __name__ == '__main__':
    token = os.getenv("TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("hello", hello))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo))

    # Configuración del webhook
    PORT = int(os.environ.get('PORT', '8443'))
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"https://upload-abyss-bot.vercel.app/{token}"
    )
