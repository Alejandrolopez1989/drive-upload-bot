import os
import pickle
import asyncio
import logging
import base64
import json
import time
import mimetypes
import aiofiles
import secrets
from collections import deque # <-- Importante para la cola
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
try:
    ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", 0))
except (ValueError, TypeError):
    ADMIN_TELEGRAM_ID = 0

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "telegramprueba30@gmail.com")

# --- CONFIGURACIÓN DE GOOGLE DRIVE ---
SCOPES = ['https://www.googleapis.com/auth/drive']
RENDER_REDIRECT_URI = "https://google-drive-vip.onrender.com/oauth2callback"

# --- Inicialización ---
app_quart = Quart(__name__)
app_telegram = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Diccionarios y Colas en memoria ---
active_operations = {} # Solo debería tener 0 o 1 elemento
# Cola para las solicitudes de subida pendientes
upload_queue = deque() # [(user_id, message_obj), ...]
# Diccionario para rastrear los mensajes de estado de la cola {user_id: message_id}
queue_status_messages = {}
user_credentials = {}
login_states = {}
pending_emails = {}
approved_users = set()
user_info = {} # {user_id: {'name': '...', 'username': '...'}}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Funciones auxiliares para Google Drive ---
def is_user_authenticated(user_id):
    creds = user_credentials.get(user_id)
    if not creds:
        return False
    if creds.valid:
        return True
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            user_credentials[user_id] = creds
            return True
        except Exception as e:
            logger.error(f"Error refrescando credenciales para {user_id}: {e}")
            user_credentials.pop(user_id, None)
            return False
    return False

def get_user_drive_service(user_id):
    creds = user_credentials.get(user_id)
    if not creds:
        return None
    if creds and creds.valid:
        return build('drive', 'v3', credentials=creds)
    elif creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            user_credentials[user_id] = creds
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            logger.error(f"Error refrescando token para {user_id}: {e}")
            user_credentials.pop(user_id, None)
            return None
    else:
        user_credentials.pop(user_id, None)
        return None

# --- Clase para subida con progreso ---
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
        self._uploaded += (post_pos - pre_pos)

        if self._callback and self._total_size > 0:
            progress = min(100, int((self._uploaded / self._total_size) * 100))
            try:
                self._callback(progress)
            except Exception as e:
                logger.warning(f"Error en callback de progreso: {e}")

        if self._cancel_flag and self._cancel_flag.is_set():
            self._file_handle.close()
            raise Exception("Operación cancelada por el usuario.")

        if response is not None:
            self._file_handle.close()
        return status, response

    def __del__(self):
        if hasattr(self, '_file_handle') and not self._file_handle.closed:
            self._file_handle.close()

async def upload_to_drive_with_progress(user_id, file_path, file_name, progress_callback, cancel_flag):
    service = get_user_drive_service(user_id)
    if not service:
        return None
    try:
        file_metadata = {'name': file_name}
        mime_type, _ = mimetypes.guess_type(file_path)
        media = ProgressMediaUpload(
            filename=file_path,
            mimetype=mime_type or 'application/octet-stream',
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
        logger.error(f"Error subiendo a Drive para {user_id}: {e}")
        raise e

def get_file_url(file_id):
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

def list_drive_videos(user_id):
    service = get_user_drive_service(user_id)
    if not service:
        return []
    try:
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
        logger.error(f"Error listando videos para {user_id}: {e}")
        return []

def delete_from_drive(file_id, user_id):
    service = get_user_drive_service(user_id)
    if not service:
        return False
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error eliminando de Drive para {user_id}: {e}")
        return False

# --- Manejadores de Pyrogram ---
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
    commands = [
        BotCommand("start", "Mostrar mensaje de inicio"),
        BotCommand("drive_login", "Conectar tu cuenta de Google Drive"),
        BotCommand("ver_nube", "Ver tus videos en la nube"),
        # Comandos exclusivos del administrador
        BotCommand("lista_aprobados", "🔐 Ver lista de usuarios aprobados (Admin)"),
        BotCommand("desaprobar_usuario", "🔐 Desaprobar un usuario (Admin)"),
    ]
    try:
        await client.set_bot_commands(commands)
        logger.info("✅ Menú de comandos establecido.")
    except Exception as e:
        logger.error(f"Error estableciendo comandos: {e}")

# --- Función para actualizar mensajes de usuarios en cola ---
async def update_queue_messages(client: Client):
    """Actualiza los mensajes de estado para todos los usuarios en la cola."""
    for i, (user_id, _) in enumerate(upload_queue):
        position = i + 1 # Las posiciones comienzan en 1
        message_id = queue_status_messages.get(user_id)
        if message_id:
            try:
                await client.edit_message_text(
                    user_id, message_id,
                    f"⏳ Tu video está en cola. Posición: {position}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.warning(f"No se pudo actualizar mensaje de cola para {user_id}: {e}")
                # Opcional: eliminar mensaje fallido del rastreador
                # queue_status_messages.pop(user_id, None)

# --- Función para procesar la cola ---
async def process_queue(client: Client):
    """Toma el primer elemento de la cola y lo procesa."""
    if active_operations:
        # Si ya hay una operación activa, no procesamos la cola
        return

    if upload_queue:
        user_id, message = upload_queue.popleft()
        # Eliminar el mensaje de estado de la cola del rastreador
        queue_status_messages.pop(user_id, None)
        # Actualizar mensajes de los usuarios restantes en cola
        await update_queue_messages(client)
        # Iniciar el procesamiento real
        await handle_video_from_queue(client, message)

# --- Manejador modificado para videos ---
@app_telegram.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    user_id = message.from_user.id

    if not is_user_authenticated(user_id):
        await message.reply_text("❌ Conecta tu cuenta de Google Drive primero con /drive_login.")
        return

    # --- Sistema de Cola ---
    if active_operations:
        # Si hay una operación activa, añadir a la cola
        position = len(upload_queue) + 1 # La nueva posición será el tamaño actual + 1
        upload_queue.append((user_id, message))
        queue_msg = await message.reply_text(f"⏳ Tu video está en cola. Posición: {position}")
        queue_status_messages[user_id] = queue_msg.id
        logger.info(f"Usuario {user_id} añadido a la cola en posición {position}")
        # Actualizar mensajes de otros usuarios en cola si es necesario
        # (No es estrictamente necesario aquí, ya que se actualiza al procesar)
        return
    # --- Fin Sistema de Cola ---

    # Si no hay operaciones activas, procesar inmediatamente
    await handle_video_from_queue(client, message)

# --- Nueva función que contiene la lógica original de manejo de video ---
async def handle_video_from_queue(client: Client, message: Message):
    """Lógica principal de descarga y subida, ahora llamada desde el manejador o la cola."""
    user_id = message.from_user.id
    # Esta verificación ya se hizo, pero por seguridad la repetimos
    if not is_user_authenticated(user_id):
        await message.reply_text("❌ (Cola) Conecta tu cuenta de Google Drive primero con /drive_login.")
        # Intentar procesar el siguiente en la cola
        await process_queue(client)
        return

    try:
        cancel_flag = asyncio.Event()
        cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{user_id}")]]
        reply_markup = InlineKeyboardMarkup(cancel_button)
        status_message = await message.reply_text("📥 Descargando el video... 0%", reply_markup=reply_markup)
        status_message_id = status_message.id

        # Registrar la operación activa
        active_operations[user_id] = {
            'task': asyncio.current_task(),
            'file_path': None,
            'status_message_id': status_message_id,
            'cancel_flag': cancel_flag
        }
        logger.info(f"Iniciando procesamiento para usuario {user_id}")

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
                    milestones = [0, 25, 50, 75, 100]
                    current_milestone = 0
                    for m in reversed(milestones):
                        if progress >= m:
                            current_milestone = m
                            break
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
            # Procesar siguiente en cola
            await process_queue(client)
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
            milestones = [0, 25, 50, 75, 100]
            current_milestone = 0
            for m in reversed(milestones):
                if progress >= m:
                    current_milestone = m
                    break
            if current_milestone > last_shown_progress_upload:
                main_loop_upload.call_soon_threadsafe(
                    asyncio.create_task,
                    update_status_message(client, message.chat.id, status_message_id, f"☁️ Subiendo a tu Google Drive... {current_milestone}%", user_id)
                )
                last_shown_progress_upload = current_milestone

        file_name = f"video_{message.video.file_unique_id}_{message.video.file_name or 'video.mp4'}"
        file_id = await upload_to_drive_with_progress(user_id, file_path, file_name, update_upload_progress, cancel_flag)

        if cancel_flag.is_set():
            await update_status_message(client, message.chat.id, status_message_id, "❌ Operación cancelada durante la subida.", user_id, remove_buttons=True)
            if os.path.exists(file_path):
                os.remove(file_path)
            active_operations.pop(user_id, None)
            # Procesar siguiente en cola
            await process_queue(client)
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
            logger.info(f"Operación cancelada por el usuario {user_id}")
        else:
            logger.error(f"Error en handle_video_from_queue para {user_id}: {e}")
            status_message_id = active_operations.get(user_id, {}).get('status_message_id')
            if status_message_id:
                await update_status_message(client, message.chat.id, status_message_id, f"❌ Ocurrió un error: {str(e)}", user_id, remove_buttons=True)
            try:
                if 'file_path' in locals() and os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass
    finally:
        # Limpiar operación activa
        active_operations.pop(user_id, None)
        logger.info(f"Finalizado procesamiento para usuario {user_id}")
        # Intentar procesar el siguiente elemento en la cola
        await process_queue(client)

# --- Función auxiliar para actualizar mensajes de estado ---
async def update_status_message(client: Client, chat_id: int, message_id: int, text: str, user_id: int, remove_buttons: bool = False):
    try:
        if remove_buttons:
            await client.edit_message_text(chat_id, message_id, text, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)
        else:
            cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(cancel_button)
            await client.edit_message_text(chat_id, message_id, text, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"Error actualizando mensaje: {e}")

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
            # Nota: process_queue se llamará en el finally de handle_video_from_queue
        # Si está en cola, también se puede cancelar (opcional, requiere más lógica)
        # elif any(item[0] == user_id for item in upload_queue):
        #     # Eliminar de la cola
        #     global upload_queue
        #     upload_queue = deque([item for item in upload_queue if item[0] != user_id])
        #     queue_status_messages.pop(user_id, None)
        #     await callback_query.answer("Operación en cola cancelada.")
        #     await update_queue_messages(client) # Actualizar posiciones
        else:
            await callback_query.answer("❌ No hay operación activa para cancelar.", show_alert=True)

    else:
        await callback_query.answer("❌ Acción no reconocida.", show_alert=True)

# --- Comandos restantes (sin cambios) ---
@app_telegram.on_message(filters.command("drive_login"))
async def drive_login_command(client: Client, message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or message.from_user.username or "Usuario"

    if is_user_authenticated(user_id):
        await message.reply_text("✅ Tu cuenta de Google Drive ya está conectada.")
        return

    if user_id == ADMIN_TELEGRAM_ID:
        await message.reply_text(
            f"✅ ¡Hola Administrador {user_name}!\n"
            f"Asegúrate de que tu correo (`{ADMIN_EMAIL}`) esté en 'Usuarios de prueba'."
        )
        state = secrets.token_urlsafe(32)
        login_states[state] = user_id
        creds_data = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        # CORRECCIÓN AQUÍ
        if not creds_: # <-- Corrección aquí
            await message.reply_text("❌ Error: Credenciales de Google no configuradas.")
            return
        try:
            async with aiofiles.open('credentials_temp.json', 'w') as f:
                await f.write(creds_data)
            flow = Flow.from_client_secrets_file(
                'credentials_temp.json', scopes=SCOPES,
                redirect_uri=RENDER_REDIRECT_URI)
            authorization_url, _ = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='true',
                state=state)
            login_url = authorization_url
            await message.reply_text(
                f"**Haz clic aquí para autenticarte:**\n{login_url}"
            )
        except Exception as e:
            logger.error(f"Error login admin {user_id}: {e}")
            await message.reply_text("❌ Error al iniciar login.")
        finally:
            if os.path.exists('credentials_temp.json'):
                os.remove('credentials_temp.json')
        return

    if user_id not in approved_users:
        if user_id in pending_emails:
            await message.reply_text(
                f"Hola {user_name}!\n"
                "Ya enviaste tu correo. Espera a que el admin te apruebe.\n"
                "Te avisaremos cuando puedas continuar."
            )
        else:
            await message.reply_text(
                f"Hola {user_name}!\n\n"
                "1️⃣ Envíame tu correo de Google (ej: `tu@gmail.com`)\n"
                "2️⃣ El admin te agregará como 'Usuario de prueba'\n"
                "3️⃣ El admin te notificará cuando estés listo\n"
                "**Importante:** No uses `/drive_login` hasta la notificación."
            )
        return

    state = secrets.token_urlsafe(32)
    login_states[state] = user_id
    creds_data = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    # CORRECCIÓN AQUÍ
    if not creds_: # <-- Corrección aquí
        await message.reply_text("❌ Error del servidor: Credenciales no configuradas.")
        if ADMIN_TELEGRAM_ID:
            try:
                await client.send_message(ADMIN_TELEGRAM_ID, f"❌ Error en /drive_login: GOOGLE_CREDENTIALS_JSON no configuradas.")
            except: pass
        return

    try:
        async with aiofiles.open('credentials_temp.json', 'w') as f:
            await f.write(creds_data)
        flow = Flow.from_client_secrets_file(
            'credentials_temp.json', scopes=SCOPES,
            redirect_uri=RENDER_REDIRECT_URI)
        authorization_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            state=state)
        login_url = authorization_url
        await message.reply_text(
            f"✅ ¡Hola {user_name}! Has sido aprobado.\n\n"
            f"**Haz clic aquí para autenticarte:**\n{login_url}"
        )
    except Exception as e:
        logger.error(f"Error login usuario {user_id}: {e}")
        await message.reply_text("❌ Error al iniciar login.")
        if ADMIN_TELEGRAM_ID:
            try:
                await client.send_message(ADMIN_TELEGRAM_ID, f"❌ Error en /drive_login para {user_id} ({user_name}): {e}")
            except: pass
    finally:
        if os.path.exists('credentials_temp.json'):
            os.remove('credentials_temp.json')

@app_telegram.on_message(filters.command("ver_nube"))
async def ver_nube_command(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_user_authenticated(user_id):
        await message.reply_text("❌ Conecta tu cuenta de Google Drive primero con /drive_login.")
        return
    service = get_user_drive_service(user_id)
    if not service:
        await message.reply_text("❌ Problema de conexión con tu Drive. Intenta desconectarte y reconectarte.")
        return
    status_message = await message.reply_text("🔍 Buscando videos...")
    videos = list_drive_videos(user_id)
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

@app_telegram.on_message(filters.regex(r"^/delete_([a-zA-Z0-9_-]+)$"))
async def delete_file(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_user_authenticated(user_id):
        await message.reply_text("❌ Conecta tu cuenta de Google Drive primero con /drive_login.")
        return
    service = get_user_drive_service(user_id)
    if not service:
        await message.reply_text("❌ Problema de conexión con tu Drive.")
        return
    match = message.matches[0] if message.matches else None
    if not match:
        await message.reply_text("❌ Comando no válido.")
        return
    file_id = match.group(1)
    status_message = await message.reply_text("🗑️ Eliminando video...")
    if delete_from_drive(file_id, user_id):
        await status_message.edit_text("✅ Video eliminado exitosamente de tu Google Drive.")
    else:
        await status_message.edit_text("❌ Error al eliminar el video de tu Google Drive.")

@app_telegram.on_message(filters.text & filters.private & ~filters.me & ~filters.regex(r"^/"))
async def handle_user_email(client: Client, message: Message):
    user_id = message.from_user.id
    if is_user_authenticated(user_id) or user_id in approved_users:
        return

    user_name = message.from_user.first_name or message.from_user.username or "Usuario"
    text = message.text.strip()

    if "@" in text and "." in text and " " not in text:
        email = text
        pending_emails[user_id] = email

        # --- Almacenar información del usuario ---
        user_mention = message.from_user.username
        user_display = f"@{user_mention}" if user_mention else "Sin @username"
        # Guardar nombre y username en el nuevo diccionario
        user_info[user_id] = {
            'name': user_name,
            'username': user_display
        }
        # --- Fin almacenamiento ---

        if ADMIN_TELEGRAM_ID:
            try:
                # Usar la información almacenada
                admin_msg = (
                    f"📧 **Nuevo correo para aprobación:**\n"
                    f"**Nombre:** {user_name}\n"
                    f"**Usuario:** {user_display}\n"
                    f"**ID:** `{user_id}`\n"
                    f"**Correo:** `{email}`\n\n"
                    f"**Acción:** Agrega el correo a 'Usuarios de prueba' en Google Cloud Console "
                    f"y luego usa `/aprobar_usuario {user_id}`."
                )
                await client.send_message(ADMIN_TELEGRAM_ID, admin_msg, parse_mode=enums.ParseMode.MARKDOWN)
                await message.reply_text("✅ Correo recibido. El administrador ha sido notificado.")
            except Exception as e:
                logger.error(f"Error notificando admin: {e}")
                await message.reply_text("❌ Error al procesar tu correo.")
        else:
             await message.reply_text("⚠️ El administrador no ha configurado su ID.")
    else:
         await message.reply_text("Por favor, envíame únicamente tu correo de Google. Ej: `tu@gmail.com`", parse_mode=enums.ParseMode.MARKDOWN)

@app_telegram.on_message(filters.command("aprobar_usuario") & filters.private)
async def approve_user_command(client: Client, message: Message):
    logger.info(f"✅ /aprobar_usuario recibido de {message.from_user.id}")

    if message.from_user.id != ADMIN_TELEGRAM_ID:
        logger.warning(f"❌ Acceso denegado a /aprobar_usuario para {message.from_user.id}. ADMIN_TELEGRAM_ID={ADMIN_TELEGRAM_ID}")
        await message.reply_text("❌ No tienes permiso para ejecutar este comando.")
        return

    command_parts = message.text.strip().split()
    if len(command_parts) < 2:
        await message.reply_text("Uso: `/aprobar_usuario <user_id>`", parse_mode=enums.ParseMode.MARKDOWN)
        logger.info("❌ /aprobar_usuario usado sin argumentos")
        return

    try:
        target_user_id_str = command_parts[1]
        if not target_user_id_str.isdigit():
             raise ValueError("El ID de usuario debe ser un número.")
        target_user_id = int(target_user_id_str)
        logger.info(f"✅ user_id objetivo parseado: {target_user_id}")
    except (ValueError, IndexError) as e:
        logger.error(f"❌ Error parseando user_id: {e}")
        await message.reply_text("❌ El ID de usuario debe ser un número válido.")
        return
    except Exception as e:
        logger.error(f"❌ Error inesperado parseando user_id: {e}")
        await message.reply_text("❌ Error al procesar el ID de usuario.")
        return

    try:
        approved_users.add(target_user_id)
        logger.info(f"✅ Usuario {target_user_id} añadido a approved_users. Total aprobados: {len(approved_users)}")

        await message.reply_text(f"✅ Usuario `{target_user_id}` ha sido aprobado.", parse_mode=enums.ParseMode.MARKDOWN)
        logger.info(f"✅ Confirmación de aprobación enviada al admin {ADMIN_TELEGRAM_ID}")

        try:
            user_msg = (
                f"🎉 ¡Hola! El administrador ha aprobado tu solicitud.\n\n"
                f"Ahora puedes continuar con el proceso de autenticación.\n"
                f"Por favor, usa el comando `/drive_login` nuevamente para obtener el enlace de autenticación con Google."
            )
            await client.send_message(target_user_id, user_msg)
            logger.info(f"✅ Notificación de aprobación enviada al usuario {target_user_id}")
        except Exception as notify_e:
            error_msg = f"⚠️ Usuario {target_user_id} aprobado, pero no se pudo notificar: {notify_e}"
            logger.error(error_msg)
            await message.reply_text(error_msg)

    except Exception as e:
        logger.error(f"❌ Error en lógica de aprobación para {target_user_id}: {e}", exc_info=True)
        await message.reply_text(f"⚠️ Ocurrió un error al aprobar al usuario: {e}")

@app_telegram.on_message(filters.command("desaprobar_usuario") & filters.private)
async def revoke_user_command(client: Client, message: Message):
    logger.info(f"✅ /desaprobar_usuario recibido de {message.from_user.id}")

    if message.from_user.id != ADMIN_TELEGRAM_ID:
        logger.warning(f"❌ Acceso denegado a /desaprobar_usuario para {message.from_user.id}. ADMIN_TELEGRAM_ID={ADMIN_TELEGRAM_ID}")
        await message.reply_text("❌ No tienes permiso para ejecutar este comando.")
        return

    command_parts = message.text.strip().split()
    if len(command_parts) < 2:
        await message.reply_text("Uso: `/desaprobar_usuario <user_id>`", parse_mode=enums.ParseMode.MARKDOWN)
        logger.info("❌ /desaprobar_usuario usado sin argumentos")
        return

    try:
        target_user_id_str = command_parts[1]
        if not target_user_id_str.isdigit():
             raise ValueError("El ID de usuario debe ser un número.")
        target_user_id = int(target_user_id_str)
        logger.info(f"✅ user_id objetivo para desaprobación: {target_user_id}")
    except (ValueError, IndexError) as e:
        logger.error(f"❌ Error parseando user_id para desaprobación: {e}")
        await message.reply_text("❌ El ID de usuario debe ser un número válido.")
        return
    except Exception as e:
        logger.error(f"❌ Error inesperado parseando user_id para desaprobación: {e}")
        await message.reply_text("❌ Error al procesar el ID de usuario.")
        return

    try:
        if target_user_id not in approved_users:
            await message.reply_text(f"⚠️ El usuario `{target_user_id}` no está en la lista de usuarios aprobados.", parse_mode=enums.ParseMode.MARKDOWN)
            logger.info(f"⚠️ Intento de desaprobar usuario no aprobado: {target_user_id}")
            return

        approved_users.discard(target_user_id)
        # Opcional: También eliminar la información del usuario almacenada
        user_info.pop(target_user_id, None)
        logger.info(f"✅ Usuario {target_user_id} eliminado de approved_users. Total aprobados: {len(approved_users)}")

        pending_email = pending_emails.pop(target_user_id, None)
        if pending_email:
            logger.info(f"ℹ️ Correo pendiente eliminado para {target_user_id}: {pending_email}")

        user_credentials.pop(target_user_id, None)
        logger.info(f"ℹ️ Credenciales eliminadas para {target_user_id} (si existían).")

        await message.reply_text(
            f"✅ Usuario `{target_user_id}` ha sido **desaprobado**.\n"
            f"Ahora deberá enviar su correo nuevamente y ser aprobado para poder autenticarse.",
            parse_mode=enums.ParseMode.MARKDOWN
        )
        logger.info(f"✅ Confirmación de desaprobación enviada al admin {ADMIN_TELEGRAM_ID}")

    except Exception as e:
        logger.error(f"❌ Error en lógica de desaprobación para {target_user_id}: {e}", exc_info=True)
        await message.reply_text(f"⚠️ Ocurrió un error al desaprobar al usuario: {e}")

@app_telegram.on_message(filters.command("lista_aprobados") & filters.private)
async def list_approved_users_command(client: Client, message: Message):
    logger.info(f"✅ /lista_aprobados recibido de {message.from_user.id}")

    # Verificación estricta de admin
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        logger.warning(f"❌ Acceso denegado a /lista_aprobados para {message.from_user.id}. ADMIN_TELEGRAM_ID={ADMIN_TELEGRAM_ID}")
        await message.reply_text("❌ No tienes permiso para ejecutar este comando.")
        return

    if not approved_users:
        await message.reply_text("ℹ️ La lista de usuarios aprobados está vacía.")
        return

    response_text = f"**Lista de usuarios aprobados ({len(approved_users)}):**\n"
    for user_id in approved_users:
        # Usar la información almacenada en user_info
        info = user_info.get(user_id, {})
        name = info.get('name', 'Desconocido')
        username = info.get('username', 'Sin @')

        response_text += f"- **{name}** ({username}) - `{user_id}`\n"

    await message.reply_text(response_text, parse_mode=enums.ParseMode.MARKDOWN)
    logger.info(f"✅ Lista de aprobados enviada al admin {ADMIN_TELEGRAM_ID}")

# --- Rutas Web OAuth ---
@app_quart.route('/')
async def index():
    return '<h1>Bot Listo</h1><p>El bot está en funcionamiento.</p>'

@app_quart.route('/oauth2callback')
async def oauth2callback():
    code = request.args.get('code')
    state = request.args.get('state')
    if not code:
        return 'Error: No se recibió el código de autorización.', 400
    if not state or state not in login_states:
         return 'Error: Estado de autenticación no válido.', 400

    user_id = login_states.pop(state, None)
    if not user_id:
        return 'Error: No se pudo asociar el código con un usuario.', 400

    creds_data = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    # CORRECCIÓN AQUÍ
    if not creds_: # <-- Corrección aquí
        return "Error: GOOGLE_CREDENTIALS_JSON no está configurado.", 500

    try:
        async with aiofiles.open('credentials_temp.json', 'w') as f:
            await f.write(creds_data)
        flow = Flow.from_client_secrets_file(
            'credentials_temp.json', scopes=SCOPES,
            redirect_uri=RENDER_REDIRECT_URI)
        flow.fetch_token(code=code)
        creds = flow.credentials
        user_credentials[user_id] = creds
        if os.path.exists('credentials_temp.json'):
            os.remove('credentials_temp.json')
        return """
        <h1>¡Autenticación Exitosa!</h1>
        <p>Tu cuenta de Google Drive ha sido conectada.</p>
        <p>Puedes cerrar esta ventana y usar el bot en Telegram.</p>
        <script>setTimeout(function() { window.close(); }, 3000);</script>
        """
    except Exception as e:
        logger.error(f"Error en oauth2callback para {user_id}: {e}")
        if os.path.exists('credentials_temp.json'):
            os.remove('credentials_temp.json')
        return f'Error durante la autenticación: {e}', 500

# --- Punto de Entrada ---
if __name__ == "__main__":
    async def run_bot():
        await app_telegram.start()
        logger.info("Bot de Telegram iniciado.")
        await set_bot_commands(app_telegram)

    async def run_quart():
        await app_quart.run_task(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
    loop.run_until_complete(run_quart())
