import logging
from telegram.ext import Updater, CommandHandler, MessageHandler

logging.basicConfig(level=logging.INFO)

TOKEN = 'YOUR_API_TOKEN'

def start(update, context):
    update.message.reply_text('¡Hola! Soy tu bot de Telegram')

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(MessageHandler(Filters.text, start))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()