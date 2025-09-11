# app.py
import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
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
CANAL_ID_STR = os.getenv('CANAL_ID') # Ej: -100123456789

if not TOKEN:
    raise ValueError("Por favor, establece la variable de entorno TELEGRAM_BOT_TOKEN")
if not CANAL_ID_STR:
    raise ValueError("Por favor, establece la variable de entorno CANAL_ID (ej: -100123456789)")

# Convertir el ID a entero
try:
    CANAL_ID = int(CANAL_ID_STR)
except ValueError:
    raise ValueError("CANAL_ID debe ser un número entero (incluyendo el -100).")

# --- Funciones del Bot ---
def start(update: Update, context: CallbackContext):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        f"📌 Canal configurado: ID {CANAL_ID}\n\n"
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

    forwarded_message = None # Para poder borrarlo en el finally

    try:
        # --- Reenviar el mensaje del canal al chat del usuario ---
        forwarded_message = context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=CANAL_ID,
            message_id=message_id
        )
        logger.info(f"Mensaje {message_id} reenviado del canal {CANAL_ID}.")

        # Verificar si el mensaje reenviado tiene video
        if not forwarded_message or not forwarded_message.video:
            update.message.reply_text("❌ El mensaje reenviado no contiene un video.")
            return

        video = forwarded_message.video
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
            "❌ El bot no tiene permiso para leer o reenviar mensajes de ese canal. "
            "Asegúrate de que sigue siendo administrador con permisos de lectura."
        )
    except telegram.error.BadRequest as e:
        error_msg = str(e).lower()
        if "message to.forward not found" in error_msg:
            update.message.reply_text("❌ No se encontró un mensaje con ese ID en el canal.")
        elif "chat not found" in error_msg:
             update.message.reply_text("❌ No se pudo encontrar el canal. Verifica el CANAL_ID.")
        else:
            update.message.reply_text(f"❌ Solicitud incorrecta de la API de Telegram: {e}")
    except Exception as e:
        logger.error(f"Error al procesar el enlace: {e}", exc_info=True)
        update.message.reply_text(
            f"❌ Error inesperado al obtener el enlace.\n"
            f"Detalles: {e}\n\n"
            f"Por favor, inténtalo más tarde o revisa la configuración."
        )
    finally:
        # --- Intentar borrar el mensaje reenviado para mantener el chat limpio ---
        if forwarded_message:
            try:
                context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=forwarded_message.message_id
                )
                logger.info(f"Mensaje reenviado {forwarded_message.message_id} borrado.")
            except Exception as e:
                logger.warning(f"No se pudo borrar el mensaje reenviado: {e}")


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
