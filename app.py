import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        "Usa el comando:\n"
        "/getlink @nombre_canal <message_id>\n\n"
        "Ejemplo: /getlink @micanal 123\n"
        "Te daré el enlace de streaming del video en ese mensaje."
    )

async def get_streaming_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obtiene el enlace de streaming de un video en un canal."""
    user_id = update.effective_user.id
    # Opcional: Restringir el uso a tu user_id
    # TU_USER_ID = int(os.getenv('TU_USER_ID', 0)) # Añade TU_USER_ID a .env
    # if TU_USER_ID and user_id != TU_USER_ID:
    #     await update.message.reply_text("❌ No tienes permiso para usar este comando.")
    #     return

    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "❌ Uso incorrecto.\n"
            "Usa: /getlink @nombre_canal <message_id>\n"
            "Ejemplo: /getlink @micanal 123"
        )
        return

    channel_username = context.args[0]
    try:
        message_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ El <message_id> debe ser un número.")
        return

    try:
        # 1. Obtener el chat_id del canal (verifica que el bot esté en el canal o tenga acceso)
        chat = await context.bot.get_chat(channel_username)
        chat_id = chat.id
        logger.info(f"Accediendo al canal: {channel_username} (ID: {chat_id})")

        # 2. Obtener el mensaje del canal
        message = await context.bot.get_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Mensaje obtenido: {message_id}")

        # 3. Verificar si el mensaje tiene video
        if not message.video:
            await update.message.reply_text("❌ El mensaje no contiene un video.")
            return

        video = message.video
        file_id = video.file_id
        file_size_bytes = video.file_size

        # 4. Obtener la ruta del archivo usando getFile
        file_info = await context.bot.get_file(file_id=file_id)
        file_path = file_info.file_path

        # 5. Construir el enlace de streaming
        streaming_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

        # 6. Enviar el enlace al usuario
        file_size_mb = file_size_bytes / (1024 * 1024)
        await update.message.reply_text(
            f"✅ *¡Enlace de streaming obtenido!*\n\n"
            f"🔗 [Ver Video]({streaming_url})\n\n"
            f"📁 Tamaño: {file_size_mb:.2f} MB\n"
            f"🆔 File ID: `{file_id}`",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Error al procesar el enlace: {e}")
        await update.message.reply_text(
            f"❌ Error al obtener el enlace.\n"
            f"Asegúrate de:\n"
            f"1. El bot es administrador del canal.\n"
            f"2. El message_id es correcto.\n"
            f"3. El mensaje contiene un video.\n\n"
            f"Error: {e}"
        )

def main():
    """Inicia el bot."""
    application = Application.builder().token(TOKEN).build()

    # Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("getlink", get_streaming_link))

    # Iniciar el bot
    logger.info("Iniciando el bot...")
    application.run_polling()

if __name__ == '__main__':
    main()
