import os
import time
import requests
from threading import Thread
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# שליפת מפתחות ממשתני הסביבה
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '').strip()

# הגדרות מסחר
TRADING_ACTIVE = True        # מצב פעיל / מוקפא
SYMBOL = 'BTCUSDT'           # המטבע הראשי למסחר
BASE_USDT_SIZE = 20.0        # גודל עסקה בדולרים
STOP_LOSS_PCT = 0.02         # עצירת הפסד ב-2%
TRAILING_STOP_PCT = 0.015    # Trailing Stop דינמי של 1.5%

# כתובת API עוקפת חסימת US
BINANCE_BASE_URL = 'https://api1.binance.com'

# מעקב אחרי פוזיציה בלייב
position = {
    'in_trade': False,
    'side': None,             # 'LONG' או 'SHORT'
    'entry_price': 0.0,
    'highest_price': 0.0,
    'lowest_price': 0.0,
    'amount': 0.0
}

# ------------------------------------------------------------------
# פונקציות פנימיות לעבודה ישירה מול Binance (ללא חסימה!)
# ------------------------------------------------------------------
def get_binance_price(symbol):
    """שליפת מחיר בלייב משרת עוקף"""
    try:
        res = requests.get(f"{BINANCE_BASE_URL}/api/v3/ticker/price", params={'symbol': symbol}, timeout=5)
        if res.status_code == 200:
            return float(res.json()['price'])
    except Exception as e:
        print(f"Price fetch error: {e}")
    return None

def get_market_sentiment():
    """שליפת סנטימנט השוק (Fear & Greed Index)"""
    try:
        res = requests.get('https://api.alternative.me/fng/', timeout=5).json()
        val = int(res['data'][0]['value'])
        txt = res['data'][0]['value_classification']
        return val, txt
    except Exception:
        return 50, "Neutral"

def get_volume_spike():
    """זיהוי נפח מסחר חריג"""
    try:
        res = requests.get(f"{BINANCE_BASE_URL}/api/v3/klines", params={'symbol': SYMBOL, 'interval': '5m', 'limit': 6}, timeout=5)
        if res.status_code == 200:
            klines = res.json()
            recent_volumes = [float(k[5]) for k in klines[:-1]]
            avg_volume = sum(recent_volumes) / len(recent_volumes)
            latest_volume = float(klines[-1][5])
            return latest_volume > (avg_volume * 1.8)
    except Exception:
        pass
    return False

# ------------------------------------------------------------------
# לולאת המסחר האוטומטית
# ------------------------------------------------------------------
def auto_trading_loop():
    global TRADING_ACTIVE, position
    while True:
        try:
            if TRADING_ACTIVE:
                current_price = get_binance_price(SYMBOL)
                if current_price:
                    # 1. חיפוש כניסה לעסקה
                    if not position['in_trade']:
                        sentiment_val, sentiment_text = get_market_sentiment()
                        is_volume_spike = get_volume_spike()
                        trade_size = BASE_USDT_SIZE * 1.3 if is_volume_spike else BASE_USDT_SIZE

                        if sentiment_val < 35 and is_volume_spike:
                            position['in_trade'] = True
                            position['side'] = 'SHORT'
                            position['entry_price'] = current_price
                            position['lowest_price'] = current_price
                            position['amount'] = round(trade_size / current_price, 5)
                            print(f"📉 SHORT OPENED at {current_price}")

                        elif sentiment_val > 65 and is_volume_spike:
                            position['in_trade'] = True
                            position['side'] = 'LONG'
                            position['entry_price'] = current_price
                            position['highest_price'] = current_price
                            position['amount'] = round(trade_size / current_price, 5)
                            print(f"📈 LONG OPENED at {current_price}")

                    # 2. ניהול פוזיציה (Trailing Stop & Stop-Loss)
                    elif position['in_trade']:
                        entry = position['entry_price']
                        side = position['side']

                        if side == 'LONG':
                            if current_price > position['highest_price']:
                                position['highest_price'] = current_price
                            trailing_stop = position['highest_price'] * (1 - TRAILING_STOP_PCT)
                            hard_stop = entry * (1 - STOP_LOSS_PCT)

                            if current_price <= hard_stop or (current_price <= trailing_stop and position['highest_price'] > entry):
                                position['in_trade'] = False

                        elif side == 'SHORT':
                            if current_price < position['lowest_price']:
                                position['lowest_price'] = current_price
                            trailing_stop = position['lowest_price'] * (1 + TRAILING_STOP_PCT)
                            hard_stop = entry * (1 + STOP_LOSS_PCT)

                            if current_price >= hard_stop or (current_price >= trailing_stop and position['lowest_price'] < entry):
                                position['in_trade'] = False

        except Exception as e:
            print(f"Error in trading loop: {e}")
            
        time.sleep(10)

# הפעלת לולאת הרקע
thread = Thread(target=auto_trading_loop, daemon=True)
thread.start()

# ------------------------------------------------------------------
# ממשק WhatsApp
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def home():
    return "Crypto Algo Bot is Live and Running!"

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
        msg.body("🟢 הבוט הופעל מחדש ומנטר את השוק!")

    elif incoming_msg in ['סטטוס', 'status', 'מה המצב']:
        state = "🟢 פעיל" if TRADING_ACTIVE else "🔴 מוקפא"
        val, text = get_market_sentiment()
        price = get_binance_price(SYMBOL)
        price_str = f"${price:,.2f}" if price else "שגיאה בשליפה"

        pos_info = "מחכה לזיהוי פריצה/סנטימנט בשוק"
        if position['in_trade']:
            peak_info = f"שיא: ${position['highest_price']}" if position['side'] == 'LONG' else f"שפל: ${position['lowest_price']}"
            pos_info = f"בעסקה: {position['side']} על {SYMBOL}\n• מחיר כניסה: ${position['entry_price']}\n• {peak_info}"

        msg.body(f"🧠 **סטטוס בוט אלגוריתמי:**\n• מצב: {state}\n• מחיר BTC נוכחי: {price_str}\n• סנטימנט שוק: {text} ({val}/100)\n\n📌 **פוזיציה נוכחית:**\n{pos_info}")

    elif incoming_msg in ['סגור הכל', 'close']:
        if position['in_trade']:
            position['in_trade'] = False
            msg.body("⚠️ הפוזיציה נסגרה ידנית מ-WhatsApp.")
        else:
            msg.body("אין פוזיציה פתוחה כרגע.")

    else:
        msg.body("📌 פקודות WhatsApp זמינות:\n• `סטטוס` - בדיקת פוזיציה ומחירים בלייב\n• `עצור` - הקפאת הבוט\n• `הפעל` - הפעלת הבוט\n• `סגור הכל` - סגירת פוזיציה קיימת")

    return str(resp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
