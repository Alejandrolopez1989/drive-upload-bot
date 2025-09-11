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

if not TOKEN:
    raise ValueError("Por favor, establece la variable de entorno TELEGRAM_BOT_TOKEN")

# --- Funciones del Bot ---
def start(update: Update, context: CallbackContext):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        "Usa el comando:\n"
        "/getlink @nombre_canal <message_id>\n\n"
        "Ejemplo: /getlink @micanal 123\n"
        "Te daré el enlace de streaming del video en ese mensaje."
    )

def get_streaming_link(update: Update, context: CallbackContext):
    """Obtiene el enlace de streaming de un video en un canal."""
    # Opcional: Restringir el uso a tu user_id (añade TU_USER_ID a las variables de entorno de Render)
    # TU_USER_ID = int(os.getenv('TU_USER_ID', 0))
    # if TU_USER_ID and update.effective_user.id != TU_USER_ID:
    #     update.message.reply_text("❌ No tienes permiso para usar este comando.")
    #     return

    if not context.args or len(context.args) != 2:
        update.message.reply_text(
            "❌ Uso incorrecto.\n"
            "Usa: /getlink @nombre_canal <message_id>\n"
            "Ejemplo: /getlink @micanal 123"
        )
        return

    channel_username = context.args[0]
    try:
        message_id = int(context.args[1])
    except ValueError:
        update.message.reply_text("❌ El <message_id> debe ser un número.")
        return

    try:
        # 1. Obtener el chat_id del canal
        chat = context.bot.get_chat(channel_username)
        chat_id = chat.id
        logger.info(f"Accediendo al canal: {channel_username} (ID: {chat_id})")

        # 2. Obtener el mensaje del canal
        message = context.bot.get_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Mensaje obtenido: {message_id}")

        # 3. Verificar si el mensaje tiene video
        if not message.video:
            update.message.reply_text("❌ El mensaje no contiene un video.")
            return

        video = message.video
        file_id = video.file_id
        file_size_bytes = video.file_size

        # 4. Obtener la ruta del archivo usando getFile
        file_info = context.bot.get_file(file_id=file_id)
        file_path = file_info.file_path

        # 5. Construir el enlace de streaming
        streaming_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

        # 6. Enviar el enlace al usuario
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
            f"1. El bot es administrador del canal.\n"
            f"2. El message_id es correcto.\n"
            f"3. El mensaje contiene un video.\n\n"
            f"Error: {e}"
        )

def main():
    """Inicia el bot."""
    updater = Updater(TOKEN, use_context=True)

    # Get the dispatcher to register handlers
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
