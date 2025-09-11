a# app.py
import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import telegram

# --- Cargar variables de entorno ---
load_dotenv()

# --- Configuración de Logs ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuración de Variables de Entorno ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
# Credenciales para Telethon (misma que usaste en auth.py)
API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
PHONE_NUMBER = os.getenv('PHONE_NUMBER') # Nuevo: Número de teléfono para Telethon

if not TOKEN:
    raise ValueError("Por favor, establece la variable de entorno TELEGRAM_BOT_TOKEN")
if not API_ID or not API_HASH:
    raise ValueError("Por favor, establece TELEGRAM_API_ID y TELEGRAM_API_HASH en las variables de entorno.")
# PHONE_NUMBER es opcional si ya existe una sesión válida

# --- Inicializar cliente de Telethon ---
from telethon import TelegramClient

# Nombre del archivo de sesión
SESSION_NAME = 'bot_session'

# Crear cliente Telethon
telethon_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# --- Funciones auxiliares ---
def parse_private_link(link: str):
    """Extrae chat_id y message_id de un enlace privado de Telegram."""
    # Enlace tipo: https://t.me/c/123456789/1122
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if match:
        raw_chat_id = match.group(1)
        message_id = int(match.group(2))
        # Para canales/grupos privados, el ID real es -100 seguido del ID corto
        chat_id = int(f"-100{raw_chat_id}")
        return chat_id, message_id
    return None, None

# --- Funciones del Bot ---
def start(update: Update, context: CallbackContext):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        "Envíame el enlace de un mensaje de video en tu canal.\n"
        "Ejemplo: `https://t.me/c/123456789/1122`\n"
        "Te devolveré el `file_id` de ese video.",
        parse_mode='Markdown'
    )

def handle_message(update: Update, context: CallbackContext):
    """Maneja los mensajes de texto (enlaces) enviados por el usuario."""
    user_message = update.message.text

    if not user_message.startswith("http"):
        update.message.reply_text("Por favor, envíame un enlace de Telegram válido.")
        return

    chat_id, message_id = parse_private_link(user_message)

    if not chat_id or not message_id:
        update.message.reply_text("❌ Enlace no válido. Usa el formato `https://t.me/c/...`", parse_mode='Markdown')
        return

    # Usar Telethon para obtener el mensaje
    import asyncio
    
    async def get_file_id_internal():
        try:
            # Asegurar que Telethon esté conectado
            if not telethon_client.is_connected():
                # Si no está conectado, intentamos conectar (puede pedir código/password si la sesión no es válida)
                await telethon_client.connect()
                # Si necesitara autenticación, esto la dispararía. Pero como ya debería tener el .session, no debería.
                # Si falla, el error se verá en los logs.
            
            # Obtener el mensaje usando Telethon
            message = await telethon_client.get_messages(chat_id, ids=message_id)
            
            if not message:
                return "❌ Mensaje no encontrado."
            
            if not hasattr(message, 'video') or not message.video:
                return "❌ El mensaje no contiene un video."
            
            file_id = message.video.id
            file_size_bytes = message.video.size
            file_size_mb = file_size_bytes / (1024 * 1024)

            # Opcional: Obtener también el enlace de streaming
            try:
                # Usar la API de bot para getFile
                bot_file_info = context.bot.get_file(file_id=str(file_id))
                file_path = bot_file_info.file_path
                streaming_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                link_part = f"\n\n🔗 [Ver Video]({streaming_url})"
            except Exception as e:
                logger.warning(f"No se pudo obtener el enlace de streaming: {e}")
                link_part = ""

            return (
                f"✅ *File ID obtenido:*\n`{file_id}`\n\n"
                f"📁 Tamaño: {file_size_mb:.2f} MB"
                f"{link_part}"
            )
        except Exception as e:
            logger.error(f"Error al obtener el mensaje con Telethon: {e}", exc_info=True)
            return f"❌ Error al acceder al mensaje: {e}"

    # Ejecutar la función async de Telethon desde el entorno sync de ptb
    # Obtenemos el loop existente o creamos uno nuevo
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    result_message = loop.run_until_complete(get_file_id_internal())
    
    update.message.reply_text(result_message, parse_mode='Markdown')

def main():
    """Inicia el bot."""
    
    # --- Iniciar y conectar Telethon de forma síncrona ANTES de iniciar el bot ---
    logger.info("Iniciando cliente Telethon...")
    import asyncio
    
    async def init_telethon():
        try:
            # Si PHONE_NUMBER está definido, lo usamos para autenticar si es necesario
            # Si no, Telethon intentará usar la sesión existente
            if PHONE_NUMBER:
                await telethon_client.start(phone=PHONE_NUMBER)
            else:
                await telethon_client.start()
            logger.info("Cliente Telethon conectado y listo.")
        except Exception as e:
            logger.error(f"Error crítico al iniciar Telethon: {e}")
            raise # Relanzar el error para que el despliegue falle si Telethon no puede iniciar

    # Ejecutar la inicialización de Telethon en el loop de asyncio
    # Esto debe hacerse antes de que el bot de Telegram tome el control del loop
    try:
        # Intentar obtener el loop existente
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # Si no hay loop, crear uno nuevo
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # Ejecutar la inicialización de Telethon
    loop.run_until_complete(init_telethon())
    # --- Fin de la inicialización de Telethon ---
    
    # Configurar el bot de Telegram
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Comandos y handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Iniciar el bot
    logger.info("Iniciando el bot de Telegram...")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()# app.py
import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import telegram

# --- Cargar variables de entorno ---
load_dotenv()

# --- Configuración de Logs ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuración de Variables de Entorno ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
# Credenciales para Telethon (misma que usaste en auth.py)
API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
PHONE_NUMBER = os.getenv('PHONE_NUMBER') # Nuevo: Número de teléfono para Telethon

if not TOKEN:
    raise ValueError("Por favor, establece la variable de entorno TELEGRAM_BOT_TOKEN")
if not API_ID or not API_HASH:
    raise ValueError("Por favor, establece TELEGRAM_API_ID y TELEGRAM_API_HASH en las variables de entorno.")
if not PHONE_NUMBER:
    raise ValueError("Por favor, establece PHONE_NUMBER en las variables de entorno (tu número de teléfono).")

# --- Inicializar cliente de Telethon ---
# Asegurarse de que el archivo de sesión esté en la carpeta correcta
from telethon import TelegramClient

# Nombre del archivo de sesión
SESSION_NAME = 'bot_session'

# Crear cliente Telethon
telethon_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# --- Funciones auxiliares ---
def parse_private_link(link: str):
    """Extrae chat_id y message_id de un enlace privado de Telegram."""
    # Enlace tipo: https://t.me/c/123456789/1122
    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if match:
        raw_chat_id = match.group(1)
        message_id = int(match.group(2))
        # Para canales/grupos privados, el ID real es -100 seguido del ID corto
        chat_id = int(f"-100{raw_chat_id}")
        return chat_id, message_id
    return None, None

# --- Funciones del Bot ---
def start(update: Update, context: CallbackContext):
    """Envía un mensaje cuando el comando /start es emitido."""
    user = update.effective_user
    update.message.reply_text(
        f"Hola {user.first_name}!\n\n"
        "Envíame el enlace de un mensaje de video en tu canal.\n"
        "Ejemplo: `https://t.me/c/123456789/1122`\n"
        "Te devolveré el `file_id` de ese video.",
        parse_mode='Markdown'
    )

def handle_message(update: Update, context: CallbackContext):
    """Maneja los mensajes de texto (enlaces) enviados por el usuario."""
    user_message = update.message.text

    if not user_message.startswith("http"):
        update.message.reply_text("Por favor, envíame un enlace de Telegram válido.")
        return

    chat_id, message_id = parse_private_link(user_message)

    if not chat_id or not message_id:
        update.message.reply_text("❌ Enlace no válido. Usa el formato `https://t.me/c/...`", parse_mode='Markdown')
        return

    # Usar Telethon para obtener el mensaje
    import asyncio
    async def get_file_id():
        try:
            # Asegurar que Telethon esté conectado
            if not telethon_client.is_connected():
                await telethon_client.connect()
            
            # Obtener el mensaje usando Telethon
            message = await telethon_client.get_messages(chat_id, ids=message_id)
            
            if not message:
                return "❌ Mensaje no encontrado."
            
            if not hasattr(message, 'video') or not message.video:
                return "❌ El mensaje no contiene un video."
            
            file_id = message.video.id
            file_size_bytes = message.video.size
            file_size_mb = file_size_bytes / (1024 * 1024)

            # Opcional: Obtener también el enlace de streaming
            try:
                # Usar la API de bot para getFile
                bot_file_info = context.bot.get_file(file_id=str(file_id))
                file_path = bot_file_info.file_path
                streaming_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                link_part = f"\n\n🔗 [Ver Video]({streaming_url})"
            except Exception as e:
                logger.warning(f"No se pudo obtener el enlace de streaming: {e}")
                link_part = ""

            return (
                f"✅ *File ID obtenido:*\n`{file_id}`\n\n"
                f"📁 Tamaño: {file_size_mb:.2f} MB"
                f"{link_part}"
            )
        except Exception as e:
            logger.error(f"Error al obtener el mensaje con Telethon: {e}", exc_info=True)
            return f"❌ Error al acceder al mensaje: {e}"

    # Ejecutar la función async de Telethon desde el entorno sync de ptb
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result_message = loop.run_until_complete(get_file_id())
    loop.close()
    
    update.message.reply_text(result_message, parse_mode='Markdown')

async def start_telethon_client():
    """Inicia el cliente de Telethon y maneja la autenticación si es necesario."""
    try:
        logger.info("Intentando conectar Telethon...")
        await telethon_client.start(phone=PHONE_NUMBER)
        logger.info("Cliente Telethon conectado y autenticado.")
    except Exception as e:
        logger.error(f"Error al iniciar Telethon: {e}")
        # Si hay un error de autenticación, se puede manejar aquí
        # Por ahora, dejamos que el error se loguee

def main():
    """Inicia el bot."""
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Comandos y handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Iniciar Telethon de forma asíncrona
    import asyncio
    # Creamos una nueva tarea para no bloquear el inicio del bot
    asyncio.create_task(start_telethon_client())
    
    # Iniciar el bot
    logger.info("Iniciando el bot de Telegram...")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
