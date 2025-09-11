# app.py
import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# Cargar variables de entorno
load_dotenv()

# --- Configuración de Logs ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuración de Variables de Entorno ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CANAL_NOMBRE = os.getenv('CANAL_NOMBRE') # Ej: @micanal

if not TOKEN:
    raise ValueError("Por favor, establece la variable de entorno TELEGRAM_BOT_TOKEN")
if not CANAL_NOMBRE:
    raise ValueError("Por favor, establece la variable de entorno CANAL_NOMBRE (ej: @micanal)")

# --- Funciones del Bot ---
def start(update: Update, context: CallbackContext):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        f"📌 Canal configurado: {CANAL_NOMBRE}\n\n"
        "Usa el comando:\n"
        f"/getlink <message_id>\n\n"
        "Ejemplo: /getlink 1234\n"
        "Te daré el enlace de streaming del video en ese mensaje (debe ser un video)."
    )

def get_streaming_link(update: Update, context: CallbackContext):
    """Obtiene el enlace de streaming de un video en el canal administrado."""
    if not context.args or len(context.args) != 1:
        update.message.reply_text(
            "❌ Uso incorrecto.\n"
            "Usa: /getlink <message_id>\n"
            "Ejemplo: /getlink 1234"
        )
        return

    try:
        message_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("❌ El <message_id> debe ser un número.")
        return

    try:
        # El bot, al ser administrador, puede acceder al mensaje por su ID
        message = context.bot.get_message(chat_id=CANAL_NOMBRE, message_id=message_id)
        logger.info(f"Mensaje obtenido: {message_id}")

        # Verificar si el mensaje tiene video
        if not message.video:
            update.message.reply_text("❌ El mensaje no contiene un video.")
            return

        video = message.video
        file_id = video.file_id
        file_size_bytes = video.file_size

        # Obtener la ruta del archivo usando getFile
        file_info = context.bot.get_file(file_id=file_id)
        file_path = file_info.file_path

        # Construir el enlace de streaming
        streaming_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

        # Enviar el enlace al usuario
        file_size_mb = file_size_bytes / (1024 * 1024)
        update.message.reply_text(
            f"✅ *¡Enlace de streaming obtenido!*\n\n"
            f"🔗 [Ver Video]({streaming_url})\n\n"
            f"📁 Tamaño: {file_size_mb:.2f} MB\n"
            f"🆔 File ID: `{file_id}`",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Error al procesar el enlace: {e}")
        update.message.reply_text(
            f"❌ Error al obtener el enlace.\n"
            f"Asegúrate de:\n"
            f"1. El message_id ({message_id}) es correcto.\n"
            f"2. El mensaje contiene un video.\n"
            f"3. El bot sigue siendo administrador del canal.\n\n"
            f"Error: {e}"
        )

def main():
    """Inicia el bot."""
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Comandos
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("getlink", get_streaming_link))

    # Iniciar el bot
    logger.info("Iniciando el bot...")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
