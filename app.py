import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from binance.client import Client

app = Flask(__name__)

# שליפת מפתחות ממשתני הסביבה
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '').strip()

# התחברות ל-Binance
client = None
try:
    if BINANCE_API_KEY and BINANCE_SECRET_KEY:
        client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
        # שינוי ה-URL לכתובת חלופית שלא חסומה בארה"ב
        client.API_URL = 'https://api1.binance.com/api'
except Exception as e:
    print(f"Error initializing Binance client: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    incoming_msg = request.values.get('Body', '').strip().lower()
    resp = MessagingResponse()
    msg = resp.message()

    if not client:
        msg.body("❌ שגיאה: החיבור ל-Binance לא מוגדר כראוי בשרת.")
        return str(resp)

    # פקודת STATUS
    if incoming_msg == 'status':
        try:
            account = client.get_account()
            usdt_balance = next((item['free'] for item in account['balances'] if item['asset'] == 'USDT'), '0')
            msg.body(f"✅ הבוט פעיל ומחובר ל-Binance!\n💰 יתרה פנויה ב-USDT: {float(usdt_balance):.2f}")
        except Exception as e:
            msg.body(f"⚠️ הבוט באוויר אך יש שגיאה מול Binance:\n{str(e)}")

    # פקודת BALANCE
    elif incoming_msg == 'balance':
        try:
            account = client.get_account()
            balances = [f"{b['asset']}: {float(b['free']):.4f}" for b in account['balances'] if float(b['free']) > 0]
            balance_text = "\n".join(balances) if balances else "אין יתרות חיוביות."
            msg.body(f"📊 יתרות בחשבון:\n{balance_text}")
        except Exception as e:
            msg.body(f"❌ שגיאה בשליפת יתרות:\n{str(e)}")

    # פקודת BUY
    elif incoming_msg == 'buy':
        msg.body("🛒 פקודת קנייה התקבלה (ניתן להגדיר לוגיקת מסחר לפי צורך).")

    # פקודת SELL
    elif incoming_msg == 'sell':
        msg.body("🏷️ פקודת מכירה התקבלה (ניתן להגדיר לוגיקת מסחר לפי צורך).")

    # פקודת HELP
    elif incoming_msg == 'help':
        msg.body("📌 פקודות זמינות:\n• status - בדיקת חיבור ויתרת USDT\n• balance - פירוט יתרות\n• buy - ביצוע קנייה\n• sell - ביצוע מכירה")

    else:
        msg.body("הודעה לא מוכרת. שלח 'help' לרשימת הפקודות.")

    return str(resp)

@app.route('/health', methods=['GET'])
def health():
    return {"status": "ok"}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
