import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Cargar variables de entorno desde un archivo .env
load_dotenv()

app = Flask(__name__)

# Variables de entorno
TELEGRAM_TOKEN = os.getenv('7227893240:AAH-lq8p9H9PbawMmhymXcHGKhNInafwmJs')
UPLOAD_URL = os.getenv('http://up.hydrax.net/aabe07df18b06d673d7c5ee1f91a6d40')
WEBHOOK_URL = os.getenv('https://upload-abyss-bot.vercel.app/webhook')

def upload_video(file_path: str):
    file_name = os.path.basename(file_path)
    file_type = 'video/mp4'
    try:
        with open(file_path, 'rb') as f:
            files = {'file': (file_name, f, file_type)}
            response = requests.post(UPLOAD_URL, files=files)
            response.raise_for_status()  # Verifica errores HTTP
    except requests.RequestException as e:
        print(f'Error uploading video: {e}')
        return str(e)
    return response.text

def get_file_url(file_id: str):
    try:
        response = requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}')
        response.raise_for_status()  # Verifica errores HTTP
        file_info = response.json()
        file_path = file_info['result']['file_path']
        return f'https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}'
    except requests.RequestException as e:
        print(f'Error getting file URL: {e}')
        return None

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if 'message' in update and 'video' in update['message']:
        video = update['message']['video']
        file_id = video['file_id']
        file_url = get_file_url(file_id)
        if file_url:
            file_path = f'./{file_id}.mp4'
            try:
                response = requests.get(file_url)
                response.raise_for_status()  # Verifica errores HTTP
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                response_text = upload_video(file_path)
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
            return jsonify({'status': 'success', 'response': response_text})
        return jsonify({'status': 'error', 'message': 'Failed to get file URL'})
    return jsonify({'status': 'no video found'})

@app.route('/set-webhook', methods=['GET'])
def set_webhook():
    response = requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}')
    return jsonify(response.json())

@app.route('/get-webhook-info', methods=['GET'])
def get_webhook_info():
    response = requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo')
    return jsonify(response.json())

if __name__ == '__main__':
    app.run(debug=True)
