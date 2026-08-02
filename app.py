import os
import time
import requests
from threading import Thread
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from binance.client import Client

app = Flask(__name__)

# שליפת מפתחות ממשתני הסביבה
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '').strip()

# הגדרות אלגוריתם המסחר
TRADING_ACTIVE = True        # מצב פעיל / מוקפא
SYMBOL = 'BTCUSDT'           # המטבע הראשי למסחר
BASE_USDT_SIZE = 20.0        # גודל עסקה בסיסי בדולרים
STOP_LOSS_PCT = 0.02         # עצירת הפסד קשיחה ב-2%
TRAILING_STOP_PCT = 0.015    # Trailing Stop דינמי של 1.5%

# מעקב אחרי פוזיציה פתוחה בלייב
position = {
    'in_trade': False,
    'side': None,             # 'LONG' או 'SHORT'
    'entry_price': 0.0,
    'highest_price': 0.0,     # למעקב Trailing Stop ב-LONG
    'lowest_price': 0.0,      # למעקב Trailing Stop ב-SHORT
    'amount': 0.0
}

# ------------------------------------------------------------------
# התחברות ל-Binance עם עקיפת ה-API_URL לפני ה-Ping הראשוני
# ------------------------------------------------------------------
binance_client = None
try:
    if BINANCE_API_KEY and BINANCE_SECRET_KEY:
        # דריסת כתובת ברירת המחדל של מחלקת Client לפני האתחול
        Client.API_URL = 'https://api1.binance.com/api'
        binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
        print("✅ Binance Client connected successfully via api1!")
except Exception as e:
    print(f"❌ Error initializing Binance client: {e}")

# ------------------------------------------------------------------
# פונקציות ניתוח שוק (סנטימנט + נפח מסחר)
# ------------------------------------------------------------------
def get_market_sentiment():
    """שליפת סנטימנט השוק בזמן אמת (Fear & Greed Index)"""
    try:
        res = requests.get('https://api.alternative.me/fng/').json()
        val = int(res['data'][0]['value'])
        txt = res['data'][0]['value_classification']
        return val, txt
    except Exception:
        return 50, "Neutral"

def get_volume_spike():
    """זיהוי פריצות ונפח מסחר חריג (Volume Spike)"""
    try:
        if not binance_client:
            return False
        klines = binance_client.get_klines(symbol=SYMBOL, interval=Client.KLINE_INTERVAL_5MINUTE, limit=6)
        recent_volumes = [float(k[5]) for k in klines[:-1]]
        avg_volume = sum(recent_volumes) / len(recent_volumes)
        latest_volume = float(klines[-1][5])
        return latest_volume > (avg_volume * 1.8)
    except Exception:
        return False

# ------------------------------------------------------------------
# לולאת המסחר האוטומטית (רצה ברקע 24/7)
# ------------------------------------------------------------------
def auto_trading_loop():
    global TRADING_ACTIVE, position
    while True:
        try:
            if TRADING_ACTIVE and binance_client:
                ticker = binance_client.get_symbol_ticker(symbol=SYMBOL)
                current_price = float(ticker['price'])
                
                # --- א. חיפוש הזדמנות כניסה (כשאין פוזיציה) ---
                if not position['in_trade']:
                    sentiment_val, sentiment_text = get_market_sentiment()
                    is_volume_spike = get_volume_spike()
                    
                    trade_size = BASE_USDT_SIZE * 1.3 if is_volume_spike else BASE_USDT_SIZE

                    # כניסה ל-SHORT
                    if sentiment_val < 35 and is_volume_spike:
                        position['in_trade'] = True
                        position['side'] = 'SHORT'
                        position['entry_price'] = current_price
                        position['lowest_price'] = current_price
                        position['amount'] = round(trade_size / current_price, 5)
                        print(f"📉 SHORT OPENED at {current_price} | Size: ${trade_size}")

                    # כניסה ל-LONG
                    elif sentiment_val > 65 and is_volume_spike:
                        position['in_trade'] = True
                        position['side'] = 'LONG'
                        position['entry_price'] = current_price
                        position['highest_price'] = current_price
                        position['amount'] = round(trade_size / current_price, 5)
                        print(f"📈 LONG OPENED at {current_price} | Size: ${trade_size}")

                # --- ב. ניהול פוזיציה קיימת (Trailing Stop & Hard Stop) ---
                elif position['in_trade']:
                    entry = position['entry_price']
                    side = position['side']

                    if side == 'LONG':
                        if current_price > position['highest_price']:
                            position['highest_price'] = current_price
                        
                        trailing_stop_price = position['highest_price'] * (1 - TRAILING_STOP_PCT)
                        hard_stop_price = entry * (1 - STOP_LOSS_PCT)

                        if current_price <= hard_stop_price or (current_price <= trailing_stop_price and position['highest_price'] > entry):
                            position['in_trade'] = False

                    elif side == 'SHORT':
                        if current_price < position['lowest_price']:
                            position['lowest_price'] = current_price
                        
                        trailing_stop_price = position['lowest_price'] * (1 + TRAILING_STOP_PCT)
                        hard_stop_price = entry * (1 + STOP_LOSS_PCT)

                        if current_price >= hard_stop_price or (current_price >= trailing_stop_price and position['lowest_price'] < entry):
                            position['in_trade'] = False

        except Exception as e:
            print(f"Error in trading loop: {e}")
            
        time.sleep(10)

# הפעלת תהליך הרקע
thread = Thread(target=auto_trading_loop, daemon=True)
thread.start()

# ------------------------------------------------------------------
# ממשק השרת והתממשקות ל-WhatsApp
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def home():
    return "Ultra-Smart Crypto Algo Bot is Active!"

@app.route('/webhook', methods=['POST'])
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global TRADING_ACTIVE, position
    raw_msg = request.values.get('Body', '').strip()
    incoming_msg = raw_msg.lower()
    resp = MessagingResponse()
    msg = resp.message()

    if incoming_msg in ['עצור', 'stop', 'חרום']:
        TRADING_ACTIVE = False
        msg.body("🛑 המסחר האוטומטי הוקפא!")

    elif incoming_msg in ['הפעל', 'start', 'רוץ']:
        TRADING_ACTIVE = True
        msg.body("🟢 הבוט החכם הופעל מחדש ומנטר פריצות בשוק!")

    elif incoming_msg in ['סטטוס', 'status', 'מה המצב']:
        state = "🟢 פעיל" if TRADING_ACTIVE else "🔴 מוקפא"
        val, text = get_market_sentiment()
        
        pos_info = "מחכה לזיהוי פריצה/סנטימנט בשוק"
        if position['in_trade']:
            peak_info = f"שיא: ${position['highest_price']}" if position['side'] == 'LONG' else f"שפל: ${position['lowest_price']}"
            pos_info = f"בעסקה: {position['side']} על {SYMBOL}\n• מחיר כניסה: ${position['entry_price']}\n• {peak_info}"

        usdt_bal = "0"
        if binance_client:
            try:
                acc = binance_client.get_account()
                usdt_bal = next((item['free'] for item in acc['balances'] if item['asset'] == 'USDT'), '0')
            except Exception as ex:
                usdt_bal = f"שגיאה שליפה: {ex}"
        else:
            usdt_bal = "לא מחובר ל-Binance"

        msg.body(f"🧠 **סטטוס בוט אלגוריתמי מתקדם:**\n• מצב: {state}\n• סנטימנט שוק: {text} ({val}/100)\n• יתרת USDT: {usdt_bal}\n\n📌 **פוזיציה נוכחית:**\n{pos_info}")

    elif incoming_msg in ['סגור הכל', 'close']:
        if position['in_trade']:
            position['in_trade'] = False
            msg.body("⚠️ הפוזיציה נסגרה ידנית מ-WhatsApp והבוט חזר לסרוק את השוק.")
        else:
            msg.body("אין פוזיציה פתוחה כרגע.")

    else:
        msg.body("📌 פקודות WhatsApp זמינות:\n• `סטטוס` - בדיקת פוזיציה, שיאי מחיר וסנטימנט\n• `עצור` - הקפאת הבוט\n• `הפעל` - הפעלת הבוט\n• `סגור הכל` - סגירת פוזיציה קיימת")

    return str(resp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
