import logging
import os
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f'Hello {update.effective_user.first_name}')

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        text = update.message.text
    elif update.channel_post:
        text = update.channel_post.text
    else:
        text = 'No text found'
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.dispatcher.process_update(update)

# Inicialización de la aplicación de Telegram
telegram_token = os.getenv("TELEGRAM_TOKEN")
telegram_app = ApplicationBuilder().token(telegram_token).build()
telegram_app.add_handler(CommandHandler("hello", hello))
telegram_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo))

@app.route(f"/{telegram_token}", methods=["POST"])
async def webhook():
    # Manejar las solicitudes de Telegram
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    await handle_update(update, telegram_app)
    return "OK", 200

if __name__ == '__main__':
    # Solo necesario para ejecución local
    app.run(port=int(os.environ.get('PORT', 8443)), host="0.0.0.0")
import logging
import os
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f'Hello {update.effective_user.first_name}')

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        text = update.message.text
    elif update.channel_post:
        text = update.channel_post.text
    else:
        text = 'No text found'
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.dispatcher.process_update(update)

# Inicialización de la aplicación de Telegram
telegram_token = os.getenv("TELEGRAM_TOKEN")
telegram_app = ApplicationBuilder().token(telegram_token).build()
telegram_app.add_handler(CommandHandler("hello", hello))
telegram_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo))

@app.route(f"/{telegram_token}", methods=["POST"])
async def webhook():
    # Manejar las solicitudes de Telegram
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    await handle_update(update, telegram_app)
    return "OK", 200

if __name__ == '__main__':
    # Solo necesario para ejecución local
    app.run(port=int(os.environ.get('PORT', 8443)), host="0.0.0.0")
