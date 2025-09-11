import os
import re
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from telegram import Bot
from telethon import TelegramClient
from telethon.errors import MessageIdInvalidError, ChatAdminRequiredError, ChannelPrivateError
from telegram.error import TelegramError

# Cargar variables de entorno desde .env (útil para local)
load_dotenv()

# --- Configuración de Logs ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuración de Variables de Entorno ---
API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("Por favor, establece TELEGRAM_API_ID, TELEGRAM_API_HASH y TELEGRAM_BOT_TOKEN en las variables de entorno.")

# --- Inicialización de Clientes ---
# Bot API (para getFile)
bot = Bot(token=BOT_TOKEN)

# Telethon Client (para acceder a mensajes)
# La sesión se guardará en 'telethon_session/session_name.session'
client = TelegramClient('telethon_session/bot_session', API_ID, API_HASH)

app = FastAPI(title="Telegram Video Linker")

# --- Funciones auxiliares ---
async def parse_tg_link(link: str):
    """Extrae chat_id y message_id de un enlace de Telegram público o privado."""
    # Enlaces tipo: https://t.me/c/123456789/1122 o https://t.me/username/1122
    # Para canales/grupos privados (t.me/c/...), el chat_id es -100 + los números
    private_match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if private_match:
        raw_chat_id = private_match.group(1)
        message_id = int(private_match.group(2))
        # Para canales/grupos privados, el ID real es -100 seguido del ID corto
        chat_id = int(f"-100{raw_chat_id}")
        return chat_id, message_id

    public_match = re.match(r"https?://t\.me/([a-zA-Z0-9_]+)/(\d+)", link)
    if public_match:
        username = public_match.group(1)
        message_id = int(public_match.group(2))
        # Para enlaces públicos, el chat_id es el username con @
        chat_id = f"@{username}"
        return chat_id, message_id

    raise ValueError("Formato de enlace no válido. Usa https://t.me/c/... o https://t.me/username/...")

async def get_streaming_url(file_id: str) -> str:
    """Obtiene el enlace de streaming usando el API de Bot."""
    try:
        file_info = await bot.get_file(file_id=file_id)
        file_path = file_info.file_path
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    except TelegramError as e:
        logger.error(f"Error al obtener el archivo del bot: {e}")
        raise HTTPException(status_code=500, detail=f"Error del API de Bot: {e}")

# --- Rutas de la API ---
@app.get("/")
async def read_root():
    return {"message": "¡Hola! Envíame un enlace de Telegram para obtener el enlace de streaming."}

@app.get("/get_streaming_link")
async def get_link(tg_link: str):
    """
    Obtiene el enlace de streaming para un video en Telegram.
    Parámetro: tg_link (URL del mensaje de Telegram)
    """
    try:
        chat_id, message_id = await parse_tg_link(tg_link)
        logger.info(f"Procesando enlace: Chat ID: {chat_id}, Message ID: {message_id}")

        # Asegurarse de que el cliente Telethon esté conectado
        if not client.is_connected():
            await client.connect()

        # Obtener el mensaje
        message = await client.get_messages(chat_id, ids=message_id)
        
        if not message:
             raise HTTPException(status_code=404, detail="Mensaje no encontrado.")
        
        if not message.media:
             raise HTTPException(status_code=400, detail="El mensaje no contiene un archivo multimedia.")

        # Obtener el file_id del video
        file_id = None
        if hasattr(message.media, 'document') and message.media.document.mime_type.startswith('video'):
            file_id = message.media.document.id
        elif hasattr(message, 'video') and message.video:
             file_id = message.video.id
        else:
             raise HTTPException(status_code=400, detail="El mensaje no contiene un video válido.")

        # Convertir file_id a string si es necesario
        file_id_str = str(file_id) 

        # Obtener el enlace de streaming usando el API de Bot
        streaming_url = await get_streaming_url(file_id_str)

        file_size_bytes = 0
        if hasattr(message.media, 'document'):
            file_size_bytes = message.media.document.size
        elif hasattr(message, 'video'):
             file_size_bytes = message.video.size

        file_size_mb = file_size_bytes / (1024 * 1024)

        return {
            "success": True,
            "tg_link": tg_link,
            "streaming_url": streaming_url,
            "file_size_mb": round(file_size_mb, 2)
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MessageIdInvalidError, ChatAdminRequiredError, ChannelPrivateError) as e:
        logger.error(f"Error de Telethon al acceder al mensaje: {e}")
        raise HTTPException(status_code=403, detail=f"Acceso denegado o mensaje inválido: {e}")
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {e}")

# --- Punto de entrada para Render ---
# Render requiere que el objeto de la app se llame `app`
# y que se ejecute con `uvicorn app:app ...`s
