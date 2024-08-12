import requests

# Reemplaza con tu token de bot de Telegram
TELEGRAM_TOKEN = '7227893240:AAH-lq8p9H9PbawMmhymXcHGKhNInafwmJs'
# Reemplaza con la URL pública proporcionada por Vercel
VERCEL_URL = 'https://upload-abyss-bot.vercel.app'

# Configura el webhook
response = requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={VERCEL_URL}')
print(response.json())
