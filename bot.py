import os
import requests
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
UPLOAD_URL = 'http://up.hydrax.net/aabe07df18b06d673d7c5ee1f91a6d40'

def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text('¡Hola! Envíame un video y lo subiré al servidor.')

def upload_video(file_path: str):
    file_name = os.path.basename(file_path)
    file_type = 'video/mp4'
    with open(file_path, 'rb') as f:
        files = {'file': (file_name, f, file_type)}
        response = requests.post(UPLOAD_URL, files=files)
    return response.text

def handle_video(update: Update, context: CallbackContext) -> None:
    video = update.message.video
    if video:
        file_id = video.file_id
        file = context.bot.get_file(file_id)
        file_path = f'/tmp/{file_id}.mp4'
        file.download(file_path)
        
        response_text = upload_video(file_path)
        update.message.reply_text(f'Video subido. Respuesta del servidor:\n{response_text}')
        
        os.remove(file_path)

def main():
    updater = Updater(TELEGRAM_TOKEN)

    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.video, handle_video))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
