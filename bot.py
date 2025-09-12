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
import uuid
from collections import deque
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
RENDER_REDIRECT_URI = "https://google-drive-vip.onrender.com/oauth2callback")

# --- Inicialización ---
app_quart = Quart(__name__)
app_telegram = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Diccionarios y Colas en memoria ---
active_operations = {} # {task_id: {...}} - Operaciones ACTIVAS (en proceso de descarga/subida)
user_credentials = {}
login_states = {}
pending_emails = {}
approved_users = set()
user_info = {} # {user_id: {'name': '...', 'username': '...'}}

# --- NUEVO: Sistema de Cola Mejorado ---
upload_queue = asyncio.Queue()
queued_tasks = {} # {task_id: {'user_id': ..., 'message_id': ..., 'file_name': ..., 'position': ...}}
total_uploads_queued = 0 # Contador global de uploads encolados

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Funciones auxiliares para Google Drive ---
# ... (Sin cambios en estas funciones, copia tu código original aquí) ...
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

class ProgressMediaUpload(MediaIoBaseUpload):
    # ... (Sin cambios en esta clase, copia tu código original aquí) ...
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
    # ... (Sin cambios en esta función, copia tu código original aquí) ...
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
    # ... (Sin cambios en esta función, copia tu código original aquí) ...
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
                display_name = parts[2] if len(parts) == 3 else drive_name
            else:
                 display_name = drive_name
            item['display_name'] = display_name
            processed_items.append(item)
        return processed_items
    except Exception as e:
        logger.error(f"Error listando videos para {user_id}: {e}")
        return []

def delete_from_drive(file_id, user_id):
    # ... (Sin cambios en esta función, copia tu código original aquí) ...
    service = get_user_drive_service(user_id)
    if not service:
        return False
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error eliminando de Drive para {user_id}: {e}")
        return False

# --- Función auxiliar para actualizar mensajes de estado ---
async def update_status_message(client: Client, chat_id: int, message_id: int, text: str, user_id: int, remove_buttons: bool = False):
    # ... (Sin cambios en esta función, copia tu código original aquí) ...
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

# --- NUEVA: Función para procesar la cola de subidas ---
async def process_upload_queue(client: Client):
    """Función asíncrona continua que procesa videos de la cola."""
    global total_uploads_queued # Acceder a la variable global
    while True:
        try:
            queue_item = await upload_queue.get()
            task_id = queue_item['task_id']
            
            if task_id not in queued_tasks:
                logger.info(f"Tarea {task_id} fue cancelada mientras estaba en cola.")
                upload_queue.task_done()
                continue

            user_id = queue_item['user_id']
            message: Message = queue_item['message']
            file_name = queue_item.get('file_name', 'video.mp4')
            
            logger.info(f"Iniciando procesamiento de video en cola para user {user_id}, tarea {task_id}")

            # Mover la tarea de 'en cola' a 'activa'
            queued_tasks.pop(task_id, None)
            
            # --- NUEVO: Actualizar posiciones de las tareas restantes en cola ---
            # Decrementar el contador global
            total_uploads_queued -= 1
            # Actualizar la posición almacenada de las tareas restantes en queued_tasks
            tasks_to_update = list(queued_tasks.keys()) # Crear una lista para evitar errores de modificación durante la iteración
            for tid in tasks_to_update:
                 if tid in queued_tasks: # Verificar nuevamente dentro del bucle
                    old_pos = queued_tasks[tid].get('position', 0)
                    if old_pos > 0:
                        queued_tasks[tid]['position'] = old_pos - 1
            # --- FIN NUEVO ---

            if not is_user_authenticated(user_id):
                 await message.reply_text("❌ Tu cuenta de Google Drive ya no está conectada. Por favor, vuelve a autenticarte con /drive_login.")
                 upload_queue.task_done()
                 continue

            service = get_user_drive_service(user_id)
            if not service:
                await message.reply_text("❌ Problema de conexión con tu Drive. Intenta desconectarte y reconectarte.")
                upload_queue.task_done()
                continue

            # --- Lógica de descarga y subida (extraída de handle_video) ---
            try:
                cancel_flag = asyncio.Event()
                # Usar task_id para cancelar
                cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]]
                reply_markup = InlineKeyboardMarkup(cancel_button)
                status_message = await message.reply_text("📥 Descargando el video... 0%", reply_markup=reply_markup)
                status_message_id = status_message.id
                # Registrar como operación activa usando task_id
                active_operations[task_id] = {
                    'task': asyncio.current_task(),
                    'file_path': None,
                    'status_message_id': status_message_id,
                    'cancel_flag': cancel_flag,
                    'user_id': user_id,
                    'message': message
                }
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
                    active_operations.pop(task_id, None)
                    upload_queue.task_done()
                    continue
                
                await update_status_message(client, message.chat.id, status_message_id, "📥 Descargando el video... 100%", user_id)
                await asyncio.sleep(0.5)
                active_operations[task_id]['file_path'] = file_path
                await update_status_message(client, message.chat.id, status_message_id, "☁️ Subiendo a tu Google Drive... 0%", user_id)
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

                final_file_name = f"video_{message.video.file_unique_id}_{file_name}"
                file_id = await upload_to_drive_with_progress(user_id, file_path, final_file_name, update_upload_progress, cancel_flag)
                if cancel_flag.is_set():
                    await update_status_message(client, message.chat.id, status_message_id, "❌ Operación cancelada durante la subida.", user_id, remove_buttons=True)
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    active_operations.pop(task_id, None)
                    upload_queue.task_done()
                    continue
                
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
                    logger.error(f"Error en process_upload_queue para tarea {task_id} (user {user_id}): {e}")
                    status_message_id = active_operations.get(task_id, {}).get('status_message_id')
                    if status_message_id:
                        await update_status_message(client, message.chat.id, status_message_id, f"❌ Ocurrió un error: {str(e)}", user_id, remove_buttons=True)
                    try:
                        if 'file_path' in locals() and os.path.exists(file_path):
                            os.remove(file_path)
                    except: pass
            finally:
                active_operations.pop(task_id, None)
                upload_queue.task_done()
                
        except asyncio.CancelledError:
            logger.info("Tarea de procesamiento de cola cancelada.")
            break
        except Exception as e:
            logger.error(f"Error inesperado en process_upload_queue: {e}")
            upload_queue.task_done()

# --- Manejadores de Pyrogram ---
@app_telegram.on_message(filters.command("start"))
# ... (Sin cambios en este manejador, copia tu código original aquí) ...
async def start_command(client: Client, message: Message):
    welcome_text = (
        "¡Hola! 👋\n\n"
        "Antes de usar el bot, necesitas conectar tu cuenta de Google Drive.\n"
        "Usa el comando /drive_login para autenticarte.\n\n"
        "Después de autenticarte, envíame un video para subirlo a tu Google Drive.\n"
        "Los videos se procesan en orden de llegada (cola).\n\n"
        "Usa los comandos del menú para interactuar conmigo.\n"
    )
    await message.reply_text(welcome_text)

async def set_bot_commands(client: Client):
    # ... (Sin cambios en este manejador, copia tu código original aquí) ...
    commands = [
        BotCommand("start", "Mostrar mensaje de inicio"),
        BotCommand("drive_login", "Conectar tu cuenta de Google Drive"),
        BotCommand("ver_nube", "Ver tus videos en la nube"),
        BotCommand("lista_aprobados", "🔐 Ver lista de usuarios aprobados (Admin)"),
        BotCommand("desaprobar_usuario", "🔐 Desaprobar un usuario (Admin)"),
    ]
    try:
        await client.set_bot_commands(commands)
        logger.info("✅ Menú de comandos establecido.")
    except Exception as e:
        logger.error(f"Error estableciendo comandos: {e}")

@app_telegram.on_message(filters.command("drive_login"))
# ... (Sin cambios en este manejador, copia tu código original aquí) ...
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
        if not creds_data:
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
    if not creds_data:
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
# ... (Sin cambios en este manejador, copia tu código original aquí) ...
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

# --- MODIFICADO: handle_video con lógica de cola corregida ---
@app_telegram.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_user_authenticated(user_id):
        await message.reply_text("❌ Conecta tu cuenta de Google Drive primero con /drive_login.")
        return

    global total_uploads_queued # Acceder a la variable global

    task_id = str(uuid.uuid4())
    file_name = message.video.file_name or 'video.mp4'
    
    # --- NUEVO: Calcular posición antes de incrementar el contador ---
    # La posición del nuevo elemento es el número total de elementos en cola + 1
    # (ya que el elemento aún no se ha agregado)
    current_queue_size = upload_queue.qsize()
    new_position = current_queue_size + 1 # Posición 1-indexed
    
    # Incrementar el contador global *después* de calcular la posición
    total_uploads_queued += 1
    # --- FIN NUEVO ---
    
    queue_item = {
        'task_id': task_id,
        'user_id': user_id,
        'message': message,
        'file_name': file_name
    }
    
    await upload_queue.put(queue_item)
    
    # --- NUEVO: Almacenar la posición en queued_tasks ---
    queued_tasks[task_id] = {
        'user_id': user_id,
        'message_id': message.id,
        'file_name': file_name,
        'position': new_position # Almacenar la posición calculada
    }
    # --- FIN NUEVO ---
    
    # --- NUEVO: Informar al usuario con la posición correcta ---
    # Si hay 0 elementos en la cola, significa que este es el único, y se está procesando.
    # Si hay > 0 elementos en la cola, este está esperando.
    if current_queue_size == 0 and new_position == 1:
        # Caso especial: si no hay otros en cola, este podría ser el que se está procesando.
        # Pero como acabamos de ponerlo, y process_upload_queue lo toma uno por uno,
        # si la cola estaba vacía, este será el próximo en procesarse.
        # Mejor: simplemente decirle que está en la posición 1.
        await message.reply_text("⏳ Su video está en cola. Posición: 1.")
    else:
        # Hay otros en cola, o este es el primero pero hay uno procesándose.
        # La posición calculada es correcta.
        await message.reply_text(f"⏳ Su video está en cola. Posición: {new_position}.")
    # --- FIN NUEVO ---

    logger.info(f"Video de user {user_id} agregado a la cola. Tarea ID: {task_id}. Posición: {new_position}")

# ... (Los manejadores restantes como on_callback_query, delete_file, handle_user_email, 
# approve_user_command, revoke_user_command, list_approved_users_command, oauth2callback 
# y el Punto de Entrada se mantienen igual o con cambios menores como en la respuesta anterior.
# Copia tu código original para estas secciones o usa el código proporcionado en la respuesta anterior) ...

# --- Ejemplo de cómo podría quedar on_callback_query (solo cambios relevantes) ---
@app_telegram.on_callback_query()
async def on_callback_query(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if data.startswith("cancel_"):
        identifier = data.split("_", 1)[1]
        
        if identifier in queued_tasks:
            task_info = queued_tasks.pop(identifier)
            # --- NUEVO: Actualizar posiciones al cancelar ---
            global total_uploads_queued
            total_uploads_queued -= 1
            cancelled_position = task_info.get('position', 0)
            if cancelled_position > 0:
                 tasks_to_update = list(queued_tasks.keys())
                 for tid in tasks_to_update:
                     if tid in queued_tasks:
                        old_pos = queued_tasks[tid].get('position', 0)
                        if old_pos > cancelled_position: # Solo actualizar tareas que estaban detrás
                            queued_tasks[tid]['position'] = old_pos - 1
            # --- FIN NUEVO ---
            logger.info(f"Tarea en cola {identifier} cancelada por el usuario {user_id}")
            await callback_query.answer("Operación cancelada mientras estaba en cola.", show_alert=True)
            return
            
        elif identifier in active_operations:
            task_id_to_cancel = identifier
            operation = active_operations[task_id_to_cancel]
            if operation['user_id'] != user_id and user_id != ADMIN_TELEGRAM_ID:
                 await callback_query.answer("❌ No puedes cancelar la operación de otro usuario.", show_alert=True)
                 return
            operation['cancel_flag'].set()
            status_message_id = operation['status_message_id']
            await update_status_message(client, callback_query.message.chat.id, status_message_id, "⏳ Cancelando operación...", operation['user_id'], remove_buttons=True)
            await callback_query.answer("Operación cancelada.")
            return
        else:
             await callback_query.answer("❌ No se encontró la operación para cancelar.", show_alert=True)
             return
    else:
        await callback_query.answer("❌ Acción no reconocida.", show_alert=True)

# --- Punto de Entrada (solo cambios relevantes) ---
if __name__ == "__main__":
    async def run_bot():
        await app_telegram.start()
        logger.info("Bot de Telegram iniciado.")
        await set_bot_commands(app_telegram)
        
        queue_processor_task = asyncio.create_task(process_upload_queue(app_telegram))
        logger.info("Procesador de cola iniciado.")

    async def run_quart():
        await app_quart.run_task(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

    loop = asyncio.get_event_loop()
    bot_task = loop.create_task(run_bot())
    quart_task = loop.run_until_complete(run_quart())
