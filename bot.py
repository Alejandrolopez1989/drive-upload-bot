from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import requests
import os

# Obtén el token de Telegram desde las variables de entorno
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
UPLOAD_URL = os.getenv('UPLOAD_URL')  # La URL donde se subirán los videos

def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text('¡Hola! Envía un video para subirlo.')

def handle_video(update: Update, context: CallbackContext) -> None:
    file = update.message.video.get_file()
    file.download('video.mp4')

    with open('video.mp4', 'rb') as f:
        response = requests.post(UPLOAD_URL, files={'file': f})

    if response.status_code == 200:
        update.message.reply_text('¡Video subido con éxito!')
    else:
        update.message.reply_text('Hubo un error al subir el video.')

def main() -> None:
    updater = Updater(TELEGRAM_TOKEN)

    dispatcher = updater.dispatcher
    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(MessageHandler(Filters.video, handle_video))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
