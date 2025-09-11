# app.py
import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import telegram

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

if not TOKEN:
    raise ValueError("Por favor, establece la variable de entorno TELEGRAM_BOT_TOKEN")

# --- Funciones auxiliares ---
def parse_private_link(link: str):
    """Extrae raw_chat_id y message_id de un enlace privado de Telegram."""
    # Enlace tipo: https://t.me/c/123456789/1122
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if match:
        raw_chat_id = match.group(1) # 123456789
        message_id = int(match.group(2)) # 1122
        # Para canales/grupos privados, el ID real es -100 seguido del ID corto
        chat_id = int(f"-100{raw_chat_id}") # -100123456789
        return chat_id, message_id, raw_chat_id
    return None, None, None

# --- Funciones del Bot ---
def start(update: Update, context: CallbackContext):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        "Envíame el enlace de un mensaje de video en tu canal.\n"
        "Ejemplo: `https://t.me/c/123456789/1122`\n"
        "Te devolveré el enlace de streaming de ese video.",
        parse_mode='Markdown'
    )

def handle_message(update: Update, context: CallbackContext):
    """Maneja los mensajes de texto (enlaces) enviados por el usuario."""
    user_message = update.message.text

    if not user_message.startswith("http"):
        update.message.reply_text("Por favor, envíame un enlace de Telegram válido.")
        return

    chat_id, message_id, raw_chat_id = parse_private_link(user_message)

    if not chat_id or not message_id:
        update.message.reply_text("❌ Enlace no válido. Usa el formato `https://t.me/c/...`", parse_mode='Markdown')
        return

    try:
        # El bot, al ser administrador, puede acceder al mensaje directamente
        # usando el chat_id calculado y el message_id
        message = context.bot.get_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Mensaje {message_id} obtenido del canal {raw_chat_id}.")

        # Verificar si el mensaje tiene video
        if not message or not hasattr(message, 'video') or not message.video:
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

    except telegram.error.Unauthorized:
        update.message.reply_text(
            "❌ El bot no tiene permiso para leer mensajes de ese canal. "
            "Asegúrate de que sigue siendo administrador."
        )
    except telegram.error.BadRequest as e:
        error_msg = str(e).lower()
        if "message not found" in error_msg:
            update.message.reply_text("❌ No se encontró un mensaje con ese ID en el canal.")
        elif "chat not found" in error_msg:
             update.message.reply_text("❌ No se pudo encontrar el canal. Verifica el enlace.")
        else:
            update.message.reply_text(f"❌ Solicitud incorrecta de la API de Telegram: {e}")
    except Exception as e:
        logger.error(f"Error al procesar el enlace: {e}", exc_info=True)
        update.message.reply_text(
            f"❌ Error inesperado al obtener el enlace.\n"
            f"Detalles: {e}\n\n"
            f"Por favor, inténtalo más tarde."
        )

def main():
    """Inicia el bot."""
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Comandos y handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Iniciar el bot
    logger.info("Iniciando el bot...")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
