import logging
from telegram.ext import Updater, CommandHandler, MessageHandler

logging.basicConfig(level=logging.INFO)

TOKEN = '7265797972:AAHu3Jl7CXVfXVH87lsc_3TySYq6Itf7lUo'

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