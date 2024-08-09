from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import requests
import os

# Obtén el token de Telegram desde las variables de entorno
TELEGRAM_TOKEN = os.getenv('7227893240:AAH-lq8p9H9PbawMmhymXcHGKhNInafwmJs')
UPLOAD_URL = os.getenv('http://up.hydrax.net/aabe07df18b06d673d7c5ee1f91a6d40')  # La URL donde se subirán los videos

async def start(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text('¡Hola! Envía un video para subirlo.')

async def handle_video(update: Update, context: CallbackContext) -> None:
    file = await update.message.video.get_file()
    await file.download('video.mp4')

    file_name = 'video.mp4'
    file_type = 'video/mp4'
    file_path = './video.mp4'
    files = { 'file': (file_name, open(file_path, 'rb'), file_type) }

    try:
        response = requests.post(UPLOAD_URL, files=files)
        if response.status_code == 200:
            await update.message.reply_text('¡Video subido con éxito!')
        else:
            await update.message.reply_text(f'Hubo un error al subir el video: {response.text}')
    except Exception as e:
        await update.message.reply_text(f'Error al subir el video: {str(e)}')

def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))

    application.run_polling()

if __name__ == '__main__':
    main()
