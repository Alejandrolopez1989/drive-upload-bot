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
RENDER_REDIRECT_URI = os.environ.get("RENDER_REDIRECT_URI", "https://google-drive-vip.onrender.com/oauth2callback")

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
queued_tasks = {} # {task_id: {'user_id': ..., 'message_id': ..., 'file_name': ..., 'position': ..., 'queue_status_message_id': ..., 'chat_id': ...}}
total_uploads_queued = 0 # Contador global de uploads encolados

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
    try:
        if remove_buttons:
            await client.edit_message_text(chat_id, message_id, text, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)
        else:
            cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(cancel_button)
            await client.edit_message_text(chat_id, message_id, text, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"Error actualizando mensaje de estado: {e}")

# --- NUEVA: Función para actualizar el mensaje de estado de cola ---
async def update_queue_status_message(client: Client, user_id: int, chat_id: int, message_id: int, position: int):
    """
    Edita el mensaje que indica la posición en la cola para un usuario.
    """
    try:
        if position <= 0:
            if position == 0:
                 await client.edit_message_text(chat_id, message_id, "⏳ Su video está próximo a ser procesado.", parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.edit_message_text(chat_id, message_id, f"⏳ Su video está en cola. Posición: {position}.", parse_mode=enums.ParseMode.MARKDOWN)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e) and "Message to edit not found" not in str(e):
            logger.warning(f"Error actualizando mensaje de cola para user {user_id}, msg_id {message_id}: {e}")

# --- CORREGIDO Y ROBUSTECIDO: Función para procesar la cola de subidas ---
async def process_upload_queue(client: Client):
    """Función asíncrona continua que procesa videos de la cola."""
    global total_uploads_queued
    while True:
        task_id = None # Variable para rastrear task_id en el bloque finally
        try:
            queue_item = await upload_queue.get()
            task_id = queue_item['task_id'] # Guardar task_id inmediatamente

            # --- VERIFICACIÓN Y EXTRACCIÓN CORRECTA ---
            if task_id not in queued_tasks:
                logger.info(f"Tarea {task_id} fue cancelada o eliminada mientras estaba en cola.")
                # NO llamamos task_done() aquí, porque upload_queue.get() ya lo sacó
                # task_done() se llamará en el finally
                continue # Pasar a la siguiente iteración del bucle

            # Si la tarea existe, extraemos la información y la eliminamos de queued_tasks
            task_info = queued_tasks.pop(task_id, None)
            if not task_info:
                 logger.warning(f"Tarea {task_id} desapareció de queued_tasks justo antes de procesarla.")
                 continue

            # Extraer información
            user_id = queue_item['user_id']
            message: Message = queue_item['message']
            file_name = queue_item.get('file_name', 'video.mp4')

            logger.info(f"Iniciando procesamiento de video en cola para user {user_id}, tarea {task_id}")

            # --- ACTUALIZAR POSICIONES Y MENSAJES DE LAS TAREAS RESTANTES EN COLA ---
            total_uploads_queued -= 1
            tasks_to_update = list(queued_tasks.keys())
            for tid in tasks_to_update:
                if tid in queued_tasks:
                    old_pos = queued_tasks[tid].get('position', 0)
                    if old_pos > 0:
                        new_pos = old_pos - 1
                        queued_tasks[tid]['position'] = new_pos

                        # Actualizar mensaje del usuario
                        queue_msg_id = queued_tasks[tid].get('queue_status_message_id')
                        target_user_id = queued_tasks[tid].get('user_id')
                        if queue_msg_id and target_user_id:
                            task_chat_id = queued_tasks[tid].get('chat_id', target_user_id)
                            asyncio.create_task(update_queue_status_message(client, target_user_id, task_chat_id, queue_msg_id, new_pos))
            # --- FIN ACTUALIZACIÓN ---

            # --- LÓGICA DE PROCESAMIENTO (DESCARGA Y SUBIDA) ---
            # Verificaciones iniciales
            if not is_user_authenticated(user_id):
                await message.reply_text("❌ Tu cuenta de Google Drive ya no está conectada. Por favor, vuelve a autenticarte con /drive_login.")
                # task_done() se llamará en el finally
                continue

            service = get_user_drive_service(user_id)
            if not service:
                await message.reply_text("❌ Problema de conexión con tu Drive. Intenta desconectarte y reconectarte.")
                # task_done() se llamará en el finally
                continue

            # Descarga y subida
            queue_status_message_id = task_info.get('queue_status_message_id')
            
            cancel_flag = asyncio.Event()
            cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]]
            reply_markup = InlineKeyboardMarkup(cancel_button)
            
            status_message_id = None
            if queue_status_message_id:
                # Si existe un mensaje de cola, lo editamos para mostrar "Descargando..."
                try:
                    await client.edit_message_text(
                        message.chat.id, 
                        queue_status_message_id, 
                        "📥 Descargando el video... 0%", 
                        reply_markup=reply_markup
                    )
                    status_message_id = queue_status_message_id # Reutilizamos el ID del mensaje de cola
                except Exception as e:
                    logger.warning(f"No se pudo editar el mensaje de cola {queue_status_message_id} para la tarea {task_id}: {e}")
                    # Si falla la edición, enviar un nuevo mensaje (fallback)
                    status_message = await message.reply_text("📥 Descargando el video... 0%", reply_markup=reply_markup)
                    status_message_id = status_message.id
            else:
                # Si no hay mensaje de cola (primer video sin mensaje), enviar uno nuevo
                status_message = await message.reply_text("📥 Descargando el video... 0%", reply_markup=reply_markup)
                status_message_id = status_message.id
            
            # Almacenar el message_id (ya sea del mensaje editado o del nuevo) en active_operations
            active_operations[task_id] = {
                'task': asyncio.current_task(),
                'file_path': None,
                'status_message_id': status_message_id, # <-- ID del mensaje reutilizado o nuevo
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
            # Si se cancela durante la descarga, se lanza una excepción y se maneja en el except general
            
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
            # Si se cancela durante la subida, se lanza una excepción y se maneja en el except general

            # --- RESULTADO FINAL ---
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
            
            # Limpiar archivo temporal
            if os.path.exists(file_path):
                os.remove(file_path)

        except asyncio.CancelledError:
            logger.info("Tarea de procesamiento de cola cancelada.")
            raise # Re-lanzar para que el manejador de cancelación lo capture correctamente si es necesario
        except Exception as e:
            # Manejo general de errores para cualquier excepción no capturada durante el procesamiento
            logger.error(f"Error en process_upload_queue para tarea {task_id}: {e}", exc_info=True)
            # Intentar notificar al usuario si es posible
            if task_id and task_id in active_operations:
                status_msg_id = active_operations[task_id].get('status_message_id')
                user_id_op = active_operations[task_id].get('user_id')
                chat_id_op = active_operations[task_id].get('message').chat.id if active_operations[task_id].get('message') else user_id_op
                if status_msg_id and user_id_op:
                    try:
                        await update_status_message(client, chat_id_op, status_msg_id, f"❌ Ocurrió un error: {str(e)}", user_id_op, remove_buttons=True)
                    except Exception as notify_e:
                        logger.error(f"Error notificando error al usuario {user_id_op}: {notify_e}")
            
            # Limpiar archivo temporal si existe en el contexto del error
            try:
                if 'file_path' in locals() and os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass # Ignorar errores al limpiar

        finally:
            # --- BLOQUE FINALLY CRÍTICO: Asegurar task_done y limpieza ---
            # Este bloque se ejecuta SIEMPRE después de un upload_queue.get(), haya error o no.
            if task_id:
                # Limpiar operaciones activas
                active_operations.pop(task_id, None)
                
            # LLAMAR task_done() EXACTAMENTE UNA VEZ por cada upload_queue.get()
            try:
                upload_queue.task_done()
                logger.debug(f"task_done() llamado para tarea {task_id}")
            except ValueError as ve:
                # Capturar específicamente el error de task_done() ya llamado
                logger.error(f"Error al llamar task_done() para tarea {task_id}: {ve}")
            except Exception as e:
                # Capturar cualquier otro error inesperado en task_done()
                logger.error(f"Error inesperado al llamar task_done() para tarea {task_id}: {e}")


# --- Manejadores de Pyrogram ---
@app_telegram.on_message(filters.command("start"))
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
        # --- CORREGIDO: 'creds_data' ---
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
    # --- CORREGIDO: 'creds_data' ---
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

# --- CORREGIDO: handle_video con manejo de errores y almacenamiento anticipado ---
@app_telegram.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_user_authenticated(user_id):
        # Responder directamente al video con error
        await message.reply_text("❌ Conecta tu cuenta de Google Drive primero con /drive_login.")
        return

    global total_uploads_queued

    task_id = str(uuid.uuid4())
    file_name = message.video.file_name or 'video.mp4'
    
    # --- Calcular posición ---
    # Considerar tanto videos en cola como videos activos (en proceso)
    current_queue_size = upload_queue.qsize()
    current_active_size = len(active_operations)
    new_position = current_queue_size + current_active_size + 1 # Posición 1-indexed
    
    # Incrementar el contador global
    total_uploads_queued += 1
    
    queue_item = {
        'task_id': task_id,
        'user_id': user_id,
        'message': message,
        'file_name': file_name
    }
    
    # --- Almacenar en queued_tasks primero ---
    queued_tasks[task_id] = {
        'user_id': user_id,
        'message_id': message.id,
        'file_name': file_name,
        'position': new_position,
        'queue_status_message_id': None, # Se actualizará si se envía mensaje
        'chat_id': message.chat.id
    }
    
    # Poner la tarea en la cola de procesamiento
    await upload_queue.put(queue_item)
    
    # --- MODIFICADO: Responder al video y considerar videos activos ---
    queue_status_message = None
    # Mostrar mensaje de cola si hay videos en cola O videos activos (en proceso)
    if (current_queue_size + current_active_size) > 0: 
        try:
            # --- MODIFICADO: Usar reply_to_message_id para responder al video ---
            # Enviar el mensaje de estado de cola como respuesta al mensaje de video
            queue_status_message = await message.reply_text(
                f"⏳ Su video está en cola. Posición: {new_position}.",
                reply_to_message_id=message.id # <-- Responder al video
            )
            # Actualizar queued_tasks con el message_id del mensaje enviado
            queued_tasks[task_id]['queue_status_message_id'] = queue_status_message.id
        except Exception as e:
            logger.error(f"Error enviando mensaje de cola al usuario {user_id} para tarea {task_id}: {e}")
            # Si falla, intentar enviar un mensaje normal (no como respuesta)
            try:
                fallback_message = await message.reply_text("⚠️ Hubo un error al notificarte sobre tu posición en la cola. El video se procesará igualmente.")
                # Opcionalmente, podríamos almacenar este fallback_message.id también
            except:
                pass # Ignorar errores al enviar mensaje de fallback
    # Si no hay videos en cola ni activos, no se envía mensaje de cola.
    # El mensaje "Descargando..." vendrá del process_upload_queue y se creará nuevo (o editará el de cola).

    logger.info(f"Video de user {user_id} agregado a la cola. Tarea ID: {task_id}. Posición: {new_position}.")

@app_telegram.on_callback_query()
async def on_callback_query(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if data.startswith("cancel_"):
        identifier = data.split("_", 1)[1]
        
        if identifier in queued_tasks:
            task_info = queued_tasks.pop(identifier)
            global total_uploads_queued
            total_uploads_queued -= 1
            cancelled_position = task_info.get('position', 0)
            cancelled_chat_id = task_info.get('chat_id', user_id) # Obtener chat_id
            cancelled_queue_msg_id = task_info.get('queue_status_message_id') # Obtener message_id del mensaje de cola
            
            if cancelled_position > 0:
                 tasks_to_update = list(queued_tasks.keys())
                 for tid in tasks_to_update:
                     if tid in queued_tasks:
                        old_pos = queued_tasks[tid].get('position', 0)
                        if old_pos > cancelled_position:
                            queued_tasks[tid]['position'] = old_pos - 1
                            # --- NUEVO: Actualizar mensaje del usuario al cancelar ---
                            queue_msg_id = queued_tasks[tid].get('queue_status_message_id')
                            target_user_id = queued_tasks[tid].get('user_id')
                            task_chat_id = queued_tasks[tid].get('chat_id', target_user_id)
                            if queue_msg_id and target_user_id:
                                asyncio.create_task(update_queue_status_message(client, target_user_id, task_chat_id, queue_msg_id, old_pos - 1))
                            # --- FIN NUEVO ---
            
            # Eliminar el mensaje de "en cola" del usuario cancelado o informarle
            if cancelled_queue_msg_id:
                try:
                    await client.edit_message_text(cancelled_chat_id, cancelled_queue_msg_id, "❌ Operación cancelada mientras estaba en cola.", parse_mode=enums.ParseMode.MARKDOWN)
                except Exception as e:
                    logger.warning(f"Error editando/borrando mensaje de cola cancelada: {e}")
            
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

# --- Manejador para correos de usuarios ---
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
        
        user_mention = message.from_user.username
        user_display = f"@{user_mention}" if user_mention else "Sin @username"
        user_info[user_id] = {
            'name': user_name,
            'username': user_display
        }

        if ADMIN_TELEGRAM_ID:
            try:
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

# --- Comando para aprobar usuarios ---
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

# --- Comando para desaprobar (revocar) usuarios ---
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

# --- Comando actualizado para listar usuarios aprobados ---
@app_telegram.on_message(filters.command("lista_aprobados") & filters.private)
async def list_approved_users_command(client: Client, message: Message):
    logger.info(f"✅ /lista_aprobados recibido de {message.from_user.id}")

    if message.from_user.id != ADMIN_TELEGRAM_ID:
        logger.warning(f"❌ Acceso denegado a /lista_aprobados para {message.from_user.id}. ADMIN_TELEGRAM_ID={ADMIN_TELEGRAM_ID}")
        await message.reply_text("❌ No tienes permiso para ejecutar este comando.")
        return

    if not approved_users:
        await message.reply_text("ℹ️ La lista de usuarios aprobados está vacía.")
        return

    response_text = f"**Lista de usuarios aprobados ({len(approved_users)}):**\n"
    for user_id in approved_users:
        info = user_info.get(user_id, {})
        name = info.get('name', 'Desconocido')
        username = info.get('username', 'Sin @')
        
        response_text += f"- **{name}** ({username}) - `{user_id}`\n"

    await message.reply_text(response_text, parse_mode=enums.ParseMode.MARKDOWN)
    logger.info(f"✅ Lista de aprobados enviada al admin {ADMIN_TELEGRAM_ID}")

# --- Rutas Web OAuth (Corregidas) ---
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
    # --- CORREGIDO: 'creds_data' ---
    if not creds_data:
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
        # --- CORREGIDO: Asegurar que se devuelve una tupla (respuesta, código) ---
        return f'Error durante la autenticación: {e}', 500
    # --- FIN CORREGIDO ---

# --- Punto de Entrada ---
if __name__ == "__main__":
    async def run_bot():
        await app_telegram.start()
        logger.info("Bot de Telegram iniciado.")
        await set_bot_commands(app_telegram)
        
        queue_processor_task = asyncio.create_task(process_upload_queue(app_telegram))
        logger.info("Procesador de cola iniciado.")

    async def run_quart():
        # --- CORREGIDO: Asegurar el binding al puerto correcto ---
        port = int(os.environ.get("PORT", 10000))
        logger.info(f"Iniciando servidor Quart en 0.0.0.0:{port}")
        await app_quart.run_task(host="0.0.0.0", port=port)

    loop = asyncio.get_event_loop()
    bot_task = loop.create_task(run_bot())
    quart_task = loop.run_until_complete(run_quart())
