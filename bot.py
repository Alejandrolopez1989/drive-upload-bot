import os
import pickle
import asyncio
import logging
import base64
import json
import time
import mimetypes
import aiofiles
import secrets # Para generar 'state' seguro
from quart import Quart, request, redirect, url_for
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, CallbackQuery
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# --- CONFIGURACIÓN DESDE VARIABLES DE ENTORNO ---
API_ID = int(os.environ.get("TELEGRAM_API_ID"))
API_HASH = os.environ.get("TELEGRAM_API_HASH")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# --- CONFIGURACIÓN DEL ADMINISTRADOR ---
# Se obtiene de la variable de entorno. Asegúrate de configurarla en Render.
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", 0)) # 0 por defecto si no está configurada
# Correo del administrador (puedes dejarlo fijo aquí o en una variable de entorno)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "telegramprueba30@gmail.com") # Correo fijo o configurable

# --- CONFIGURACIÓN DE GOOGLE DRIVE ---
SCOPES = ['https://www.googleapis.com/auth/drive']
# TOKEN_FILE ya no se usa como archivo único

# --- FORZAR LA URI DE REDIRECCIÓN ---
# Reemplaza TU_NOMBRE_DE_SERVICIO_EN_RENDER con el nombre real de tu servicio en Render
RENDER_REDIRECT_URI = "https://google-drive-vip.onrender.com/oauth2callback"

# --- Inicialización ---
app_quart = Quart(__name__)
app_telegram = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Diccionario para almacenar operaciones en curso y poder cancelarlas
active_operations = {}

# Diccionario para almacenar credenciales de Google Drive por user_id
# NOTA: Esto se reinicia al apagar el bot. Para persistencia, usar base de datos.
user_credentials = {}

# Diccionario para asociar 'state' de OAuth con user_id temporalmente
login_states = {}

# Diccionarios para gestión de usuarios pendientes y aprobados
pending_emails = {}  # {user_id: email}
approved_users = set() # {user_id, ...}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Función para verificar si un usuario está autenticado ---
def is_user_authenticated(user_id):
    """Verifica si un usuario tiene credenciales válidas."""
    creds = user_credentials.get(user_id)
    if not creds:
        return False
    if creds.valid:
        return True
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            user_credentials[user_id] = creds # Actualizar credenciales refrescadas
            return True
        except Exception as e:
            logger.error(f"Error al refrescar credenciales para el usuario {user_id}: {e}")
            user_credentials.pop(user_id, None) # Eliminar credenciales inválidas
            return False
    return False

# --- Función para obtener el servicio de Drive del usuario ---
def get_user_drive_service(user_id):
    """Obtiene el servicio autenticado de Google Drive para un usuario específico."""
    creds = user_credentials.get(user_id)
    if not creds:
        logger.info(f"No se encontraron credenciales para el usuario {user_id}")
        return None

    if creds and creds.valid:
        logger.info(f"Credenciales válidas encontradas para el usuario {user_id}")
        return build('drive', 'v3', credentials=creds)
    elif creds.expired and creds.refresh_token:
        try:
            logger.info(f"Intentando refrescar credenciales para el usuario {user_id}")
            creds.refresh(Request())
            user_credentials[user_id] = creds # Actualizar credenciales refrescadas
            logger.info(f"Credenciales refrescadas y actualizadas para el usuario {user_id}")
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            logger.error(f"Error al refrescar el token para el usuario {user_id}: {e}")
            user_credentials.pop(user_id, None) # Eliminar credenciales inválidas
            return None
    else:
        logger.error(f"Credenciales cargadas para el usuario {user_id} pero no válidas.")
        user_credentials.pop(user_id, None) # Limpiar credenciales inválidas
        return None

# --- Clase mejorada para manejar la subida con progreso y cancelación ---
class ProgressMediaUpload(MediaIoBaseUpload):
    def __init__(self, filename, mimetype=None, chunksize=1024 * 1024, resumable=False, callback=None, cancel_flag=None):
        self._filename = filename
        self._file_handle = open(filename, 'rb')
        self._total_size = os.path.getsize(filename)
        self._callback = callback
        self._cancel_flag = cancel_flag
        self._uploaded = 0
        super().__init__(self._file_handle, mimetype or 'application/octet-stream', chunksize=chunksize, resumable=resumable)

    def next_chunk(self, http=None, num_retries=0):
        if self._cancel_flag and self._cancel_flag.is_set():
            self._file_handle.close()
            raise Exception("Operación cancelada por el usuario.")

        pre_pos = self._file_handle.tell()
        status, response = super().next_chunk(http=http, num_retries=num_retries)
        post_pos = self._file_handle.tell()
        chunk_uploaded = post_pos - pre_pos
        self._uploaded += chunk_uploaded

        if self._callback and self._total_size > 0:
            progress = min(100, int((self._uploaded / self._total_size) * 100))
            try:
                self._callback(progress)
            except Exception as e:
                logger.warning(f"Error en callback de progreso de subida: {e}")

        if self._cancel_flag and self._cancel_flag.is_set():
            self._file_handle.close()
            raise Exception("Operación cancelada por el usuario.")

        if response is not None:
            self._file_handle.close()

        return status, response

    def __del__(self):
        if hasattr(self, '_file_handle') and not self._file_handle.closed:
            self._file_handle.close()

# --- Función para subir con progreso y cancelación ---
async def upload_to_drive_with_progress(user_id, file_path, file_name, progress_callback, cancel_flag):
    """Sube un archivo a Google Drive del usuario y devuelve el ID del archivo."""
    service = get_user_drive_service(user_id) # <-- Usar servicio del usuario
    if not service:
        logger.error(f"Servicio de Drive no disponible para el usuario {user_id} al intentar subir.")
        return None
    try:
        file_metadata = {'name': file_name}
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = 'application/octet-stream'

        media = ProgressMediaUpload(
            filename=file_path,
            mimetype=mime_type,
            chunksize=1024 * 1024,
            resumable=True,
            callback=progress_callback,
            cancel_flag=cancel_flag
        )

        request = service.files().create(body=file_metadata, media_body=media, fields='id')

        response = None
        while response is None:
            if cancel_flag.is_set():
                raise Exception("Operación cancelada por el usuario.")
            status, response = request.next_chunk()

        return response.get('id')
    except Exception as e:
        if "Operación cancelada por el usuario" in str(e):
            logger.info(f"Subida cancelada por el usuario {user_id}.")
            raise e
        else:
            logger.error(f"Error al subir a Drive para el usuario {user_id}: {e}")
            raise e

def get_file_url(file_id):
    """Devuelve el enlace de descarga del archivo."""
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

# --- Función mejorada para listar videos con nombre para mostrar ---
def list_drive_videos(user_id): # <-- Pasar user_id
    """Lista solo los archivos de video en Google Drive del usuario."""
    service = get_user_drive_service(user_id) # <-- Usar servicio del usuario
    if not service:
        logger.error(f"Servicio de Drive no disponible para el usuario {user_id} al intentar listar.")
        return []
    try:
        # Consulta más específica para videos subidos por el bot
        query = "name contains 'video_' and (mimeType contains 'video/' or name contains '.mp4' or name contains '.avi' or name contains '.mov' or name contains '.wmv' or name contains '.flv' or name contains '.webm')"
        results = service.files().list(
            pageSize=100,
            fields="nextPageToken, files(id, name, mimeType, size, createdTime)",
            q=query,
            orderBy="createdTime desc"
        ).execute()
        items = results.get('files', [])

        processed_items = []
        for item in items:
            drive_name = item.get('name', 'Sin_nombre')
            if drive_name.startswith("video_") and '_' in drive_name:
                parts = drive_name.split('_', 2)
                if len(parts) == 3:
                    display_name = parts[2]
                else:
                    display_name = drive_name
            else:
                 display_name = drive_name

            item['display_name'] = display_name
            processed_items.append(item)

        return processed_items
    except Exception as e:
        logger.error(f"Error al listar videos de Drive para el usuario {user_id}: {e}")
        return []

# --- Función para eliminar de Drive ---
def delete_from_drive(file_id, user_id): # <-- Pasar user_id
    """Elimina un archivo de Google Drive del usuario."""
    service = get_user_drive_service(user_id) # <-- Usar servicio del usuario
    if not service:
        logger.error(f"Servicio de Drive no disponible para el usuario {user_id} al intentar eliminar.")
        return False
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error al eliminar de Drive para el usuario {user_id}: {e}")
        return False

# --- Manejadores de Pyrogram (Telegram) ---

@app_telegram.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    welcome_text = (
        "¡Hola! 👋\n\n"
        "Antes de usar el bot, necesitas conectar tu cuenta de Google Drive.\n"
        "Usa el comando /drive_login para autenticarte.\n\n"
        "Después de autenticarte, envíame un video para subirlo a tu Google Drive.\n\n"
        "Usa los comandos del menú para interactuar conmigo.\n"
    )
    await message.reply_text(welcome_text)

async def set_bot_commands(client: Client):
    """Establece los comandos del bot en el menú de Telegram."""
    commands = [
        BotCommand("start", "Mostrar mensaje de inicio"),
        BotCommand("drive_login", "Conectar tu cuenta de Google Drive"),
        BotCommand("ver_nube", "Ver tus videos en la nube"),
    ]
    try:
        await client.set_bot_commands(commands)
        logger.info("✅ Menú de comandos establecido correctamente.")
    except Exception as e:
        logger.error(f"Error al establecer el menú de comandos: {e}")

@app_telegram.on_message(filters.command("drive_login"))
async def drive_login_command(client: Client, message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or message.from_user.username or "Usuario"

    # Verificar si ya está autenticado
    if is_user_authenticated(user_id):
        await message.reply_text("✅ Tu cuenta de Google Drive ya está conectada.")
        return

    # --- Excepción para el Administrador ---
    # Si el usuario es el administrador, le damos acceso directo al enlace.
    if user_id == ADMIN_TELEGRAM_ID:
        await message.reply_text(
            f"✅ ¡Hola Administrador {user_name}!\n"
            "Como administrador, tienes acceso directo al enlace de autenticación.\n"
            f"Asegúrate de que tu correo (`{ADMIN_EMAIL}`) esté agregado como 'Usuario de prueba' en Google Cloud Console."
        )
        # Proceder directamente con el flujo de OAuth
        # Generar un 'state' único para esta solicitud
        state = secrets.token_urlsafe(32)
        login_states[state] = user_id # Asociar state con user_id

        creds_data = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_data: # <-- Corrección aquí
            await message.reply_text("❌ Error del servidor: Credenciales de Google no configuradas.")
            return

        try:
            async with aiofiles.open('credentials_temp.json', 'w') as f:
                await f.write(creds_data)

            flow = Flow.from_client_secrets_file(
                'credentials_temp.json', scopes=SCOPES,
                redirect_uri=RENDER_REDIRECT_URI)
            # Pasar el 'state' generado
            authorization_url, _ = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='true',
                state=state)

            login_url = authorization_url

            # --- Mensaje al Admin con el enlace ---
            await message.reply_text(
                f"**Haz clic en el siguiente enlace para iniciar el proceso de autenticación con Google:**\n"
                f"{login_url}\n\n"
                "**Importante:** Asegúrate de que tu correo esté en la lista de prueba."
            )

        except Exception as e:
            logger.error(f"Error al iniciar login para el administrador {user_id}: {e}")
            await message.reply_text("❌ Ocurrió un error al iniciar el proceso de login. Inténtalo de nuevo más tarde.")
        finally:
            if os.path.exists('credentials_temp.json'):
                os.remove('credentials_temp.json')
        return # Salir, no seguir con el flujo normal de usuarios

    # --- Flujo normal para usuarios no-administradores ---
    # Verificar si el usuario ha sido aprobado por el administrador
    if user_id not in approved_users:
        # Verificar si ya envió su correo
        if user_id in pending_emails:
            await message.reply_text(
                f"Hola {user_name}!\n"
                "Ya has enviado tu correo. El administrador ha sido notificado.\n"
                "Por favor, espera a que el administrador te agregue como 'Usuario de prueba' en Google Cloud Console.\n"
                "Una vez hecho eso, el administrador usará un comando para indicar que puedes continuar.\n"
                "Te avisaremos cuando puedas proceder a obtener el enlace de autenticación."
            )
        else:
            # Primer intento o no ha enviado correo
            await message.reply_text(
                f"Hola {user_name}!\n\n"
                "Para conectar tu Google Drive, sigue estos pasos:\n\n"
                "1️⃣ **Envíame (al bot) únicamente tu dirección de correo electrónico de Google** que deseas usar para Drive. "
                "(Ejemplo: `tu_correo@gmail.com`)\n"
                "2️⃣ El administrador recibirá una notificación con tu solicitud, tu ID y tu correo.\n"
                "3️⃣ El administrador agregará ese correo a 'Usuarios de prueba' en Google Cloud Console.\n"
                "4️⃣ El administrador te notificará cuando estés listo para continuar.\n\n"
                "**Importante:** No intentes usar `/drive_login` para obtener el enlace de autenticación hasta "
                "que el administrador te confirme que has sido agregado. "
            )
        return # Salir, no mostrar el enlace aún

    # Si el usuario está aprobado, proceder con el flujo normal de OAuth
    # Generar un 'state' único para esta solicitud
    state = secrets.token_urlsafe(32)
    login_states[state] = user_id # Asociar state con user_id

    creds_data = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_data: # <-- Corrección aquí
        await message.reply_text("❌ Error del servidor: Credenciales de Google no configuradas.")
        # Notificar al admin del error crítico
        if ADMIN_TELEGRAM_ID:
            try:
                await client.send_message(ADMIN_TELEGRAM_ID, f"❌ Error crítico en /drive_login: GOOGLE_CREDENTIALS_JSON no configuradas.")
            except Exception as e:
                logger.error(f"Error al notificar al admin: {e}")
        return

    try:
        async with aiofiles.open('credentials_temp.json', 'w') as f:
            await f.write(creds_data)

        flow = Flow.from_client_secrets_file(
            'credentials_temp.json', scopes=SCOPES,
            redirect_uri=RENDER_REDIRECT_URI)
        # Pasar el 'state' generado
        authorization_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            state=state)

        login_url = authorization_url

        # --- Mensaje al Usuario (cuando ya está aprobado) ---
        await message.reply_text(
            f"✅ ¡Hola {user_name}! Has sido aprobado para conectar tu Google Drive.\n\n"
            "**Haz clic en el siguiente enlace para iniciar el proceso de autenticación con Google:**\n"
            f"{login_url}\n\n"
            "**Importante:** Este enlace solo funcionará porque el administrador te ha agregado como usuario de prueba. "
            "Si por algún motivo el enlace falla, intenta ejecutar `/drive_login` nuevamente."
        )

    except Exception as e:
        logger.error(f"Error al iniciar login para el usuario {user_id}: {e}")
        await message.reply_text("❌ Ocurrió un error al iniciar el proceso de login. Inténtalo de nuevo más tarde.")
        # Notificar al admin del error
        if ADMIN_TELEGRAM_ID:
            try:
                await client.send_message(ADMIN_TELEGRAM_ID, f"❌ Error en /drive_login para usuario {user_id} ({user_name}): {e}")
            except Exception as ee:
                logger.error(f"Error al notificar al admin del error de login: {ee}")
    finally:
        if os.path.exists('credentials_temp.json'):
            os.remove('credentials_temp.json')


@app_telegram.on_message(filters.command("ver_nube"))
async def ver_nube_command(client: Client, message: Message):
    user_id = message.from_user.id
    # Verificar autenticación
    if not is_user_authenticated(user_id):
        await message.reply_text(
            "❌ Necesitas conectar tu cuenta de Google Drive primero.\n"
            "Usa el comando /drive_login para autenticarte."
        )
        return

    service = get_user_drive_service(user_id) # <-- Usar servicio del usuario
    if not service:
        await message.reply_text(
            "❌ El bot está teniendo un problema de conexión con tu cuenta de Google Drive.\n"
            "Intenta desconectarte y volver a conectarte usando /drive_login."
        )
        return

    status_message = await message.reply_text("🔍 Buscando tus videos en Google Drive...")
    videos = list_drive_videos(user_id) # <-- Pasar user_id

    if not videos:
        await status_message.edit_text("No se encontraron videos en tu nube.")
        return

    response_text = f"*{len(videos)} videos en tu nube:*\n"
    for video in videos:
        file_name_to_display = video.get('display_name', 'Sin_nombre')
        file_id = video.get('id')
        display_name_limited = (file_name_to_display[:45] + '...') if len(file_name_to_display) > 48 else file_name_to_display
        file_url = get_file_url(file_id)
        delete_command = f"`/delete_{file_id}`"
        response_text += f"\n🎬 [{display_name_limited}]({file_url})\n🗑️ {delete_command}\n"

    if len(response_text) > 4096:
        parts = [response_text[i:i+4096] for i in range(0, len(response_text), 4096)]
        await status_message.edit_text(parts[0], parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)
        for part in parts[1:]:
             await message.reply_text(part, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await status_message.edit_text(response_text, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)

@app_telegram.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    user_id = message.from_user.id

    # Verificar autenticación
    if not is_user_authenticated(user_id):
        await message.reply_text(
            "❌ Necesitas conectar tu cuenta de Google Drive primero.\n"
            "Usa el comando /drive_login para autenticarte."
        )
        return

    if user_id in active_operations:
        await message.reply_text("⚠️ Ya tienes una operación en curso. Espera a que termine o cancélala.")
        return

    service = get_user_drive_service(user_id) # <-- Usar servicio del usuario
    if not service:
        await message.reply_text(
            "❌ El bot está teniendo un problema de conexión con tu cuenta de Google Drive.\n"
            "Intenta desconectarte y volver a conectarte usando /drive_login."
        )
        return

    try:
        cancel_flag = asyncio.Event()
        cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{user_id}")]]
        reply_markup = InlineKeyboardMarkup(cancel_button)

        status_message = await message.reply_text("📥 Descargando el video... 0%", reply_markup=reply_markup)
        status_message_id = status_message.id

        active_operations[user_id] = {
            'task': asyncio.current_task(),
            'file_path': None,
            'status_message_id': status_message_id,
            'cancel_flag': cancel_flag
        }

        # --- Descarga con progreso ---
        last_update = time.time()
        main_loop = asyncio.get_running_loop()
        last_shown_progress = 0

        def progress_callback(current, total):
            nonlocal last_update, last_shown_progress
            current_time = time.time()
            if cancel_flag.is_set():
                raise Exception("Operación cancelada por el usuario.")
            if current_time - last_update > 2 or current == total:
                if total > 0:
                    progress = int((current / total) * 100)
                    if progress >= 100:
                        current_milestone = 100
                    elif progress >= 75:
                        current_milestone = 75
                    elif progress >= 50:
                        current_milestone = 50
                    elif progress >= 25:
                        current_milestone = 25
                    else:
                        current_milestone = 0

                    if current_milestone > last_shown_progress:
                        main_loop.call_soon_threadsafe(
                            asyncio.create_task,
                            update_status_message(client, message.chat.id, status_message_id, f"📥 Descargando el video... {current_milestone}%", user_id)
                        )
                        last_shown_progress = current_milestone

                last_update = current_time

        file_path = await client.download_media(message, progress=progress_callback)

        if cancel_flag.is_set():
            await update_status_message(client, message.chat.id, status_message_id, "❌ Operación cancelada durante la descarga.", user_id, remove_buttons=True)
            if os.path.exists(file_path):
                os.remove(file_path)
            active_operations.pop(user_id, None)
            return

        await update_status_message(client, message.chat.id, status_message_id, "📥 Descargando el video... 100%", user_id)
        await asyncio.sleep(0.5)

        active_operations[user_id]['file_path'] = file_path
        await update_status_message(client, message.chat.id, status_message_id, "☁️ Subiendo a tu Google Drive... 0%", user_id)

        # --- Subida con progreso ---
        last_shown_progress_upload = 0
        main_loop_upload = asyncio.get_running_loop()

        def update_upload_progress(progress):
            nonlocal last_shown_progress_upload
            if cancel_flag.is_set():
                raise Exception("Operación cancelada por el usuario.")

            if progress >= 100:
                current_milestone = 100
            elif progress >= 75:
                current_milestone = 75
            elif progress >= 50:
                current_milestone = 50
            elif progress >= 25:
                current_milestone = 25
            else:
                current_milestone = 0

            if current_milestone > last_shown_progress_upload:
                main_loop_upload.call_soon_threadsafe(
                    asyncio.create_task,
                    update_status_message(client, message.chat.id, status_message_id, f"☁️ Subiendo a tu Google Drive... {current_milestone}%", user_id)
                )
                last_shown_progress_upload = current_milestone

        file_name = f"video_{message.video.file_unique_id}_{message.video.file_name or 'video.mp4'}"
        # Pasar user_id a la función de subida
        file_id = await upload_to_drive_with_progress(user_id, file_path, file_name, update_upload_progress, cancel_flag)

        if cancel_flag.is_set():
            await update_status_message(client, message.chat.id, status_message_id, "❌ Operación cancelada durante la subida.", user_id, remove_buttons=True)
            if os.path.exists(file_path):
                os.remove(file_path)
            active_operations.pop(user_id, None)
            return

        if file_id:
            file_url = get_file_url(file_id)
            await update_status_message(client, message.chat.id, status_message_id,
                f"✅ ¡Video subido exitosamente a tu Google Drive!\n\n"
                f"🔗 [Descargar Video]({file_url})\n\n"
                f"Usa /ver_nube para ver y gestionar tus videos.",
                user_id, remove_buttons=True
            )
        else:
            await update_status_message(client, message.chat.id, status_message_id, "❌ Error al subir el video a tu Google Drive.", user_id, remove_buttons=True)

        if os.path.exists(file_path):
            os.remove(file_path)

    except Exception as e:
        if "Operación cancelada por el usuario" in str(e):
            pass
        else:
            logger.error(f"Error en handle_video para el usuario {user_id}: {e}")
            status_message_id = active_operations.get(user_id, {}).get('status_message_id')
            if status_message_id:
                await update_status_message(client, message.chat.id, status_message_id, f"❌ Ocurrió un error: {str(e)}", user_id, remove_buttons=True)
            try:
                if 'file_path' in locals() and os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass
    finally:
        active_operations.pop(message.from_user.id, None)

# --- Función auxiliar para actualizar mensajes de estado ---
async def update_status_message(client: Client, chat_id: int, message_id: int, text: str, user_id: int, remove_buttons: bool = False):
    """Actualiza un mensaje de estado, manejando errores de edición."""
    try:
        if remove_buttons:
            await client.edit_message_text(chat_id, message_id, text, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)
        else:
            cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(cancel_button)
            await client.edit_message_text(chat_id, message_id, text, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            logger.warning(f"No se pudo actualizar el mensaje de estado (no modificado): {e}")
        else:
            logger.error(f"No se pudo actualizar el mensaje de estado: {e}")

# --- Manejador para Callback Queries (Botones Inline) ---
@app_telegram.on_callback_query()
async def on_callback_query(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id

    if data.startswith("cancel_"):
        target_user_id = int(data.split("_")[1])
        if user_id != target_user_id:
            await callback_query.answer("❌ No puedes cancelar la operación de otro usuario.", show_alert=True)
            return

        if user_id in active_operations:
            operation = active_operations[user_id]
            operation['cancel_flag'].set()
            status_message_id = operation['status_message_id']
            await update_status_message(client, callback_query.message.chat.id, status_message_id, "⏳ Cancelando operación...", user_id, remove_buttons=True)
            await callback_query.answer("Operación cancelada.")
        else:
            await callback_query.answer("❌ No hay operación activa para cancelar.", show_alert=True)

    else:
        await callback_query.answer("❌ Acción no reconocida.", show_alert=True)

@app_telegram.on_message(filters.regex(r"^/delete_([a-zA-Z0-9_-]+)$"))
async def delete_file(client: Client, message: Message):
    user_id = message.from_user.id # Obtener user_id
    # Verificar autenticación
    if not is_user_authenticated(user_id):
        await message.reply_text(
            "❌ Necesitas conectar tu cuenta de Google Drive primero.\n"
            "Usa el comando /drive_login para autenticarte."
        )
        return

    service = get_user_drive_service(user_id) # <-- Usar servicio del usuario
    if not service:
        await message.reply_text(
            "❌ El bot está teniendo un problema de conexión con tu cuenta de Google Drive.\n"
            "Intenta desconectarte y volver a conectarte usando /drive_login."
        )
        return

    match = message.matches[0] if message.matches else None
    if not match:
        await message.reply_text("❌ Comando no válido.")
        return

    file_id = match.group(1)

    status_message = await message.reply_text("🗑️ Eliminando video de tu Google Drive...")
    # Pasar user_id a la función de eliminación
    if delete_from_drive(file_id, user_id):
        await status_message.edit_text("✅ Video eliminado exitosamente de tu Google Drive.")
    else:
        await status_message.edit_text("❌ Error al eliminar el video de tu Google Drive o el video no existe.")


# --- Nuevo manejador para recibir el correo del usuario (excluyendo al admin) ---
@app_telegram.on_message(filters.text & filters.private & ~filters.me)
async def handle_user_email(client: Client, message: Message):
    user_id = message.from_user.id
    # Excluir al administrador de este flujo
    if user_id == ADMIN_TELEGRAM_ID:
        return # El admin usa /drive_login directamente

    user_name = message.from_user.first_name or message.from_user.username or "Usuario"
    text = message.text.strip()

    # Solo procesar si el usuario no está autenticado ni aprobado
    if is_user_authenticated(user_id) or user_id in approved_users:
        return

    # Verificar si parece un correo electrónico válido (básico)
    if "@" in text and "." in text and " " not in text:
        email = text
        pending_emails[user_id] = email

        # Notificar al administrador
        if ADMIN_TELEGRAM_ID:
            try:
                admin_msg = (
                    f"📧 **Nuevo correo recibido para aprobación:**\n"
                    f"**Nombre:** {user_name}\n"
                    f"**ID de Telegram:** `{user_id}`\n"
                    f"**Correo proporcionado:** `{email}`\n\n"
                    f"**Acción requerida:** Agrega este correo a 'Usuarios de prueba' en Google Cloud Console "
                    f"y luego usa el comando `/aprobar_usuario {user_id}` para permitirle acceder al enlace de autenticación."
                )
                await client.send_message(ADMIN_TELEGRAM_ID, admin_msg, parse_mode=enums.ParseMode.MARKDOWN)
                await message.reply_text("✅ Correo recibido. Se ha notificado al administrador. "
                                        "Una vez te agregue como usuario de prueba, podrás continuar con la autenticación.")
            except Exception as e:
                logger.error(f"Error al procesar correo o notificar admin: {e}")
                await message.reply_text("❌ Hubo un error al procesar tu correo. Inténtalo de nuevo más tarde.")
        else:
             await message.reply_text("⚠️ El administrador no ha configurado su ID. No se puede procesar tu solicitud.")
    else:
         # Si no es un correo válido, recordarle la instrucción
         await message.reply_text("Por favor, envíame únicamente tu dirección de correo electrónico de Google. "
                                 "Por ejemplo: `tu_correo@gmail.com`", parse_mode=enums.ParseMode.MARKDOWN)

# --- Nuevo comando para el administrador para aprobar usuarios ---
@app_telegram.on_message(filters.command("aprobar_usuario") & filters.private)
async def approve_user_command(client: Client, message: Message):
    # Verificar que el que ejecuta el comando es el administrador
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        await message.reply_text("❌ No tienes permiso para ejecutar este comando.")
        return

    # Extraer el user_id del argumento del comando
    command_parts = message.text.split()
    if len(command_parts) < 2:
        await message.reply_text("Uso: `/aprobar_usuario <user_id>`", parse_mode=enums.ParseMode.MARKDOWN)
        return

    try:
        target_user_id = int(command_parts[1])
    except ValueError:
        await message.reply_text("❌ El ID de usuario debe ser un número.")
        return

    # --- Modificación: Aprobar incluso si no hay correo pendiente ---
    # Verificar si el usuario tiene un correo pendiente (opcional, para info)
    user_email = pending_emails.get(target_user_id, "No proporcionado")
    if target_user_id not in pending_emails:
        # Opcional: Notificar que no había correo pendiente
        await message.reply_text(f"⚠️ El usuario `{target_user_id}` no tenía un correo pendiente, pero será aprobado igualmente.")

    # Marcar al usuario como aprobado
    approved_users.add(target_user_id)

    # Notificar al administrador
    await message.reply_text(f"✅ Usuario `{target_user_id}` aprobado. "
                            f"Se le ha notificado que puede proceder con `/drive_login`.")

    # Notificar al usuario
    try:
        user_msg = (
            f"🎉 ¡Hola! El administrador ha aprobado tu solicitud.\n\n"
            f"Ahora puedes continuar con el proceso de autenticación.\n"
            f"Por favor, usa el comando `/drive_login` nuevamente para obtener el enlace de autenticación con Google."
        )
        await client.send_message(target_user_id, user_msg)
    except Exception as e:
        logger.error(f"Error al notificar al usuario {target_user_id} de aprobación: {e}")
        await message.reply_text(f"⚠️ El usuario fue aprobado, pero no se pudo enviarle el mensaje de confirmación: {e}")


# --- Rutas Web para Autenticación OAuth ---
@app_quart.route('/')
async def index():
    return '<h1>Bot Listo</h1><p>El bot está en funcionamiento. Usa Telegram para interactuar.</p>'

# La ruta /authorize ya no es necesaria como endpoint público,
# ya que el enlace se genera dinámicamente en /drive_login


@app_quart.route('/oauth2callback')
async def oauth2callback():
    code = request.args.get('code')
    state = request.args.get('state') # Obtener el state de la URL
    if not code:
        return 'Error: No se recibió el código de autorización.', 400
    if not state or state not in login_states:
         return 'Error: Estado de autenticación no válido o expirado.', 400

    # Obtener el user_id asociado con este state
    user_id = login_states.pop(state, None) # Elimina el state después de usarlo
    if not user_id:
        return 'Error: No se pudo asociar el código con un usuario.', 400

    creds_data = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_data: # <-- Corrección aquí
        # No se puede enviar mensaje a Telegram desde aquí fácilmente sin más setup
        return "Error: GOOGLE_CREDENTIALS_JSON no está configurado en el servidor.", 500

    try:
        async with aiofiles.open('credentials_temp.json', 'w') as f:
            await f.write(creds_data)

        flow = Flow.from_client_secrets_file(
            'credentials_temp.json', scopes=SCOPES,
            redirect_uri=RENDER_REDIRECT_URI)

        flow.fetch_token(code=code)
        creds = flow.credentials

        # Almacenar las credenciales para este usuario específico
        user_credentials[user_id] = creds

        if os.path.exists('credentials_temp.json'):
            os.remove('credentials_temp.json')

        # Mensaje de éxito en la web
        return f"""
        <h1>¡Autenticación Exitosa!</h1>
        <p>Tu cuenta de Google Drive ha sido conectada al bot.</p>
        <p>ID de usuario asociado: {user_id}</p>
        <p>Puedes cerrar esta ventana y usar el bot en Telegram.</p>
        <script>
            setTimeout(function() {{
                window.close();
            }}, 5000); // Cierra automáticamente después de 5 segundos
        </script>
        """

    except Exception as e:
        logger.error(f"Error en oauth2callback para el usuario {user_id}: {e}")
        if os.path.exists('credentials_temp.json'):
            os.remove('credentials_temp.json')
        return f'Error durante la autenticación: {e}', 500

# --- Punto de Entrada ---
if __name__ == "__main__":
    # No se necesita cargar token global
    # success = load_token_from_env() # Eliminar esta línea
    # if not success:
    #     logger.warning("⚠️ El bot podría no estar autenticado. Falta GOOGLE_DRIVE_TOKEN_BASE64 o token.pickle.")

    async def run_bot():
        await app_telegram.start()
        logger.info("Bot de Telegram iniciado.")
        await set_bot_commands(app_telegram)

    async def run_quart():
        await app_quart.run_task(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
    loop.run_until_complete(run_quart())
