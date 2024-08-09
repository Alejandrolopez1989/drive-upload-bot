import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Variables de entorno
TELEGRAM_TOKEN = os.getenv('7227893240:AAH-lq8p9H9PbawMmhymXcHGKhNInafwmJs')
UPLOAD_URL = 'http://up.hydrax.net/aabe07df18b06d673d7c5ee1f91a6d40'
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

def upload_video(file_path: str):
    file_name = os.path.basename(file_path)
    file_type = 'video/mp4'
    with open(file_path, 'rb') as f:
        files = {'file': (file_name, f, file_type)}
        response = requests.post(UPLOAD_URL, files=files)
    return response.text

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if 'message' in update and 'video' in update['message']:
        video = update['message']['video']
        file_id = video['file_id']
        file_url = f'https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_id}'
        file_path = f'./{file_id}.mp4'
        response = requests.get(file_url)
        with open(file_path, 'wb') as f:
            f.write(response.content)
        response_text = upload_video(file_path)
        os.remove(file_path)
        return jsonify({'status': 'success', 'response': response_text})
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
    app.run()
