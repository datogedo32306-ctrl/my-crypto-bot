# פקודה חדשה לבדיקת IP של השרת
    if raw_msg in ['ip', 'אייפי', 'מה ה ip']:
        try:
            my_ip = requests.get('https://api.ipify.org', timeout=5).text.strip()
            msg.body(f"🌐 כתובת ה-IP של השרת ב-Render היא:\n`{my_ip}`\n\nהעתק אותה והדבק ב-Binance!")
        except Exception as e:
            msg.body(f"❌ שגיאה בשליפת IP: {e}")
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
BASE_USDT_SIZE = 20.0        # גודל עסקה בדולרים
STOP_LOSS_PCT = 0.02         # עצירת הפסד ב-2%
TRAILING_STOP_PCT = 0.015    # Trailing Stop דינמי של 1.5%
MIN_DAILY_VOLUME = 500000    # נפח מסחר יומי מינימלי בדולרים (מסנן מטבעות זבל ללא נזילות)

# מעקב אחרי פוזיציה בלייב
position = {
    'in_trade': False,
    'symbol': None,           # המטבע שנבחר לעסקה
    'side': None,             # 'LONG' או 'SHORT'
    'entry_price': 0.0,
    'highest_price': 0.0,
    'lowest_price': 0.0,
    'amount': 0.0
}

# ------------------------------------------------------------------
# פונקציות סריקה וזיהוי מטבעות חדשים/פורצים
# ------------------------------------------------------------------
def get_crypto_price(symbol):
    """שליפת מחיר בלייב מ-Binance"""
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get("https://api1.binance.com/api/v3/ticker/price", params={'symbol': symbol}, headers=headers, timeout=5)
        if res.status_code == 200:
            return float(res.json()['price'])
    except Exception as e:
        print(f"Error fetching price for {symbol}: {e}")
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

def get_top_volume_candidates():
    """שליפת כל צמדי ה-USDT הפעילים ב-Binance וסינון לפי נזילות"""
    headers = {'User-Agent': 'Mozilla/5.0'}
    candidates = []
    try:
        res = requests.get("https://api1.binance.com/api/v3/ticker/24hr", headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            for ticker in data:
                symbol = ticker['symbol']
                # מעקב אחרי כל מטבע שמסתיים ב-USDT, לא ממונף (UP/DOWN), ובעל נפח מסחר מספק
                if symbol.endswith('USDT') and not ('UPUSDT' in symbol or 'DOWNUSDT' in symbol or 'BEAR' in symbol or 'BULL' in symbol):
                    quote_volume = float(ticker['quoteVolume'])  # נפח מסחר בדולרים
                    if quote_volume >= MIN_DAILY_VOLUME:
                        candidates.append(symbol)
    except Exception as e:
        print(f"Error fetching candidates: {e}")
    
    # אם הייתה שגיאה, מניחים רשימת ברירת מחדל
    return candidates if candidates else ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'PEPEUSDT', 'NEARUSDT', 'FETUSDT']

def get_volume_spike_ratio(symbol):
    """חישוב יחס קפיצת נפח המסחר (5 דקות אחרונות מול ממוצע)"""
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get("https://api1.binance.com/api/v3/klines", params={'symbol': symbol, 'interval': '5m', 'limit': 6}, headers=headers, timeout=5)
        if res.status_code == 200:
            klines = res.json()
            recent_volumes = [float(k[5]) for k in klines[:-1]]
            avg_volume = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0
            latest_volume = float(klines[-1][5])
            if avg_volume > 0:
                return latest_volume / avg_volume
    except Exception:
        pass
    return 1.0

# ------------------------------------------------------------------
# לולאת המסחר האוטומטית
# ------------------------------------------------------------------
def auto_trading_loop():
    global TRADING_ACTIVE, position
    while True:
        try:
            if TRADING_ACTIVE:
                # 1. ניהול פוזיציה פתוחה
                if position['in_trade']:
                    sym = position['symbol']
                    current_price = get_crypto_price(sym)

                    if current_price:
                        entry = position['entry_price']
                        side = position['side']

                        if side == 'LONG':
                            if current_price > position['highest_price']:
                                position['highest_price'] = current_price
                            trailing_stop = position['highest_price'] * (1 - TRAILING_STOP_PCT)
                            hard_stop = entry * (1 - STOP_LOSS_PCT)

                            if current_price <= hard_stop or (current_price <= trailing_stop and position['highest_price'] > entry):
                                position['in_trade'] = False
                                print(f"🛑 LONG CLOSED on {sym} at {current_price}")

                        elif side == 'SHORT':
                            if current_price < position['lowest_price']:
                                position['lowest_price'] = current_price
                            trailing_stop = position['lowest_price'] * (1 + TRAILING_STOP_PCT)
                            hard_stop = entry * (1 + STOP_LOSS_PCT)

                            if current_price >= hard_stop or (current_price >= trailing_stop and position['lowest_price'] < entry):
                                position['in_trade'] = False
                                print(f"🛑 SHORT CLOSED on {sym} at {current_price}")

                # 2. סריקת השוק לזיהוי פריצות במטבעות חדשים/קיימים
                else:
                    sentiment_val, sentiment_text = get_market_sentiment()
                    candidates = get_top_volume_candidates()

                    best_symbol = None
                    max_spike = 1.0

                    # סורק את המועמדים ומוצא את הזינוק ההרסני ביותר
                    for sym in candidates[:50]:  # סורק את 50 המועמדים המובילים בלולאה
                        spike_ratio = get_volume_spike_ratio(sym)
                        if spike_ratio > max_spike:
                            max_spike = spike_ratio
                            best_symbol = sym

                    # תנאי כניסה: קפיצת נפח של לפחות x2.0 מהממוצע
                    if best_symbol and max_spike >= 2.0:
                        current_price = get_crypto_price(best_symbol)
                        if current_price:
                            trade_size = BASE_USDT_SIZE * 1.3

                            if sentiment_val < 35:
                                position['in_trade'] = True
                                position['symbol'] = best_symbol
                                position['side'] = 'SHORT'
                                position['entry_price'] = current_price
                                position['lowest_price'] = current_price
                                position['amount'] = round(trade_size / current_price, 5)
                                print(f"📉 SHORT OPENED on {best_symbol} (Spike: {max_spike:.1f}x) at {current_price}")

                            elif sentiment_val > 65:
                                position['in_trade'] = True
                                position['symbol'] = best_symbol
                                position['side'] = 'LONG'
                                position['entry_price'] = current_price
                                position['highest_price'] = current_price
                                position['amount'] = round(trade_size / current_price, 5)
                                print(f"📈 LONG OPENED on {best_symbol} (Spike: {max_spike:.1f}x) at {current_price}")

        except Exception as e:
            print(f"Error in trading loop: {e}")

        time.sleep(12)

# הפעלת לולאת הרקע
thread = Thread(target=auto_trading_loop, daemon=True)
thread.start()

# ------------------------------------------------------------------
# ממשק WhatsApp
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def home():
    return "Dynamic Crypto Algo Bot is Live and Running!"

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
        msg.body("🟢 הבוט הופעל מחדש ומנטר את כל המטבעות בשוק!")

    elif incoming_msg in ['סטטוס', 'status', 'מה המצב']:
        state = "🟢 פעיל" if TRADING_ACTIVE else "🔴 מוקפא"
        val, text = get_market_sentiment()
        
        btc_p = get_crypto_price('BTCUSDT')
        eth_p = get_crypto_price('ETHUSDT')
        sol_p = get_crypto_price('SOLUSDT')
        
        btc_str = f"${btc_p:,.2f}" if btc_p else "N/A"
        eth_str = f"${eth_p:,.2f}" if eth_p else "N/A"
        sol_str = f"${sol_p:,.2f}" if sol_p else "N/A"

        pos_info = "🔍 סורק בלייב מאות מטבעות לזיהוי פריצה/Volume Spike"
        if position['in_trade']:
            peak_info = f"שיא: ${position['highest_price']}" if position['side'] == 'LONG' else f"שפל: ${position['lowest_price']}"
            pos_info = f"🔥 **בעסקה מועדפת:** {position['side']} על **{position['symbol']}**\n• מחיר כניסה: ${position['entry_price']}\n• {peak_info}"

        msg.body(f"🧠 *סטטוס סורק מטבעות אלגוריתמי:*\n• מצב: {state}\n• סנטימנט שוק: {text} ({val}/100)\n\n📊 *אינדיקטורים ראשיים:*\n• BTC: {btc_str}\n• ETH: {eth_str}\n• SOL: {sol_str}\n\n📌 *פוזיציה נוכחית:*\n{pos_info}")

    elif incoming_msg in ['סגור הכל', 'close']:
        if position['in_trade']:
            sym = position['symbol']
            position['in_trade'] = False
            msg.body(f"⚠️ הפוזיציה על {sym} נסגרה ידנית מ-WhatsApp.")
        else:
            msg.body("אין פוזיציה פתוחה כרגע.")

    else:
        msg.body("📌 פקודות WhatsApp זמינות:\n• `סטטוס` - בדיקת סורק המטבעות ופוזיציות\n• `עצור` - הקפאת הבוט\n• `הפעל` - הפעלת הבוט\n• `סגור הכל` - סגירת פוזיציה קיימת")

    return str(resp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
