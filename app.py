import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from binance.client import Client

app = Flask(__name__)

# טעינת מפתחות האבטחה ממשתני הסביבה ב-Render
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY')

# אתחול חיבור ל-Binance
binance_client = None
if BINANCE_API_KEY and BINANCE_SECRET_KEY:
    try:
        binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    except Exception as e:
        print(f"Error connecting to Binance: {e}")

@app.route('/', methods=['GET'])
def home():
    return "Crypto Trading & Sentiment Bot is Running Successfully!"

@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip().lower()
    resp = MessagingResponse()
    msg = resp.message()

    if 'status' in incoming_msg or 'מצב' in incoming_msg:
        reply_text = "🟢 הבוט פעיל ומחובר לשרת Render."
        if binance_client:
            try:
                # בדיקת חיבור ל-Binance והצגת יתרת USDT
                account = binance_client.get_account()
                usdt_balance = next((b['free'] for b in account['balances'] if b['asset'] == 'USDT'), '0')
                reply_text += f"\n💰 יתרת USDT ב-Binance: ${float(usdt_balance):,.2f}"
            except Exception as e:
                reply_text += "\n⚠️ מחובר ל-Binance במצב קריאה (Paper Trading)."
        msg.body(reply_text)

    elif 'price' in incoming_msg or 'מחיר' in incoming_msg:
        if binance_client:
            try:
                btc_price = binance_client.get_symbol_ticker(symbol="BTCUSDT")
                eth_price = binance_client.get_symbol_ticker(symbol="ETHUSDT")
                msg.body(f"📊 מחירי שוק נוכחיים:\n• BTC: ${float(btc_price['price']):,.2f}\n• ETH: ${float(eth_price['price']):,.2f}")
            except Exception as e:
                msg.body("שגיאה במשיכת נתוני שוק מ-Binance.")
        else:
            msg.body("מפתחות Binance לא הוגדרו עדיין ב-Render.")

    else:
        msg.body("שלום! אני בוט המסחר שלך.\nפקודות זמינות:\n• 'מצב' או 'status' - בדיקת חיבור ויתרה\n• 'מחיר' או 'price' - בדיקת מחירי ביטקוין ואתריום")

    return str(resp)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
