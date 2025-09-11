# app.py
import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
# Importar errores de la API de Telegram (forma compatible con v20.x)
from telegram.error import (
    TelegramError,
    BadRequest,
    Unauthorized
)

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

# --- Funciones del Bot (ahora async) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        "Envíame el enlace de un mensaje de video en tu canal.\n"
        "Ejemplo: `https://t.me/c/123456789/1122`\n"
        "Te devolveré el enlace de streaming de ese video.",
        parse_mode='Markdown'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los mensajes de texto (enlaces) enviados por el usuario."""
    user_message = update.message.text

    if not user_message.startswith("http"):
        await update.message.reply_text("Por favor, envíame un enlace de Telegram válido.")
        return

    chat_id, message_id, raw_chat_id = parse_private_link(user_message)

    if not chat_id or not message_id:
        await update.message.reply_text("❌ Enlace no válido. Usa el formato `https://t.me/c/...`", parse_mode='Markdown')
        return

    forwarded_message = None # Para poder borrarlo después

    try:
        # --- Reenviar el mensaje del canal al chat del usuario ---
        # El bot debe ser administrador del canal para hacer esto.
        forwarded_message = await context.bot.forward_message(
            chat_id=update.effective_chat.id, # Reenviar al chat actual (el usuario)
            from_chat_id=chat_id,
            message_id=message_id
        )
        logger.info(f"Mensaje {message_id} reenviado del canal {raw_chat_id}.")

        # Verificar si el mensaje reenviado tiene video
        if not forwarded_message or not hasattr(forwarded_message, 'video') or not forwarded_message.video:
            await update.message.reply_text("❌ El mensaje reenviado no contiene un video.")
            return

        video = forwarded_message.video
        file_id = video.file_id
        file_size_bytes = video.file_size

        # Opcional: Borrar el mensaje reenviado para mantener el chat limpio
        # try:
        #     await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=forwarded_message.message_id)
        #     logger.info(f"Mensaje reenviado {forwarded_message.message_id} borrado.")
        # except Exception as e:
        #     logger.warning(f"No se pudo borrar el mensaje reenviado: {e}")

        # Obtener la ruta del archivo usando getFile
        file_info = await context.bot.get_file(file_id=file_id)
        file_path = file_info.file_path

        # Construir el enlace de streaming
        streaming_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

        # Enviar el enlace al usuario
        file_size_mb = file_size_bytes / (1024 * 1024)
        await update.message.reply_text(
            f"✅ *¡Enlace de streaming obtenido!*\n\n"
            f"🔗 [Ver Video]({streaming_url})\n\n"
            f"📁 Tamaño: {file_size_mb:.2f} MB\n"
            f"🆔 File ID: `{file_id}`",
            parse_mode='Markdown'
        )

    # --- Manejo de errores correcto para v20.x ---
    except Unauthorized:
        await update.message.reply_text(
            "❌ El bot no tiene permiso para leer o reenviar mensajes de ese canal. "
            "Asegúrate de que sigue siendo administrador."
        )
    except BadRequest as e:
        error_msg = str(e).lower()
        if "message to forward not found" in error_msg or "message not found" in error_msg:
            await update.message.reply_text("❌ No se encontró un mensaje con ese ID en el canal.")
        elif "chat not found" in error_msg:
             await update.message.reply_text("❌ No se pudo encontrar el canal. Verifica el enlace.")
        else:
            await update.message.reply_text(f"❌ Solicitud incorrecta de la API de Telegram: {e}")
    except TelegramError as e: # Captura general para otros errores de la API
         await update.message.reply_text(f"❌ Error de la API de Telegram: {e}")
    except Exception as e: # Captura cualquier otro error inesperado
        logger.error(f"Error al procesar el enlace: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Error inesperado al obtener el enlace.\n"
            f"Detalles: {e}\n\n"
            f"Por favor, inténtalo más tarde."
        )
    finally:
        # Intentar borrar el mensaje reenviado si se creó
        if forwarded_message:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=forwarded_message.message_id
                )
                logger.info(f"Mensaje reenviado {forwarded_message.message_id} borrado en el finally.")
            except Exception as e:
                logger.warning(f"(Finally) No se pudo borrar el mensaje reenviado: {e}")


def main():
    """Inicia el bot."""
    # Crear la aplicación del bot usando la nueva API
    application = Application.builder().token(TOKEN).build()

    # Comandos y handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Iniciar el bot
    logger.info("Iniciando el bot (v20.7 - forward_message)...")
    application.run_polling()

if __name__ == '__main__':
    main()
