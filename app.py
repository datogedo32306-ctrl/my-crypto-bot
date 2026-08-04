import os
import time
import hmac
import hashlib
import requests
from threading import Thread
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# שליפת מפתחות ממשתני הסביבה ב-Render
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '').strip()

# הגדרות מסחר
TRADING_ACTIVE = True        # מצב פעיל / מוקפא
BASE_USDT_SIZE = 20.0        # גודל עסקה בדולרים
LEVERAGE = 5                 # מנוף בחוזים עתידיים (X5)
STOP_LOSS_PCT = 0.02         # עצירת הפסד ב-2%
TRAILING_STOP_PCT = 0.015    # Trailing Stop דינמי של 1.5%
MIN_DAILY_VOLUME = 1000000   # נפח מסחר יומי מינימלי (מיליון דולר)

# מעקב אחרי פוזיציה בלייב
position = {
    'in_trade': False,
    'symbol': None,           
    'side': None,             
    'entry_price': 0.0,
    'highest_price': 0.0,
    'lowest_price': 0.0,
    'amount': 0.0
}

# מטמון מחירי מדד
price_cache = {
    'BTCUSDT': 0.0,
    'ETHUSDT': 0.0,
    'SOLUSDT': 0.0,
    'last_update': 0
}

HEADERS = {
    'X-MBX-APIKEY': BINANCE_API_KEY,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
}

# ------------------------------------------------------------------
# פונקציות חתימה ותקשורת מול BINANCE (Spot & Futures)
# ------------------------------------------------------------------
def generate_signature(params):
    """יצירת חתימת HMAC SHA256 אמינה לפקודות מסחר והעברות"""
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    return hmac.new(BINANCE_SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def transfer_funds(from_account, to_account, amount, asset="USDT"):
    """
    העברת כספים פנימית בתוך Binance בלבד (Spot ↔ Futures)
    from_account / to_account: 'MAIN' (Spot) או 'UMFUTURE' (Futures)
    """
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        print("⚠️ חסרים מפתחות API לביצוע העברה")
        return False

    url = "https://api.binance.com/sapi/v1/asset/transfer"
    type_map = {
        ('MAIN', 'UMFUTURE'): 'MAIN_UMFUTURE',
        ('UMFUTURE', 'MAIN'): 'UMFUTURE_MAIN'
    }
    
    transfer_type = type_map.get((from_account, to_account))
    if not transfer_type:
        return False

    params = {
        'type': transfer_type,
        'asset': asset,
        'amount': str(amount),
        'timestamp': int(time.time() * 1000)
    }
    params['signature'] = generate_signature(params)

    try:
        res = requests.post(url, headers=HEADERS, params=params, timeout=5)
        if res.status_code == 200:
            print(f"✅ הועברו {amount} {asset} מ-{from_account} ל-{to_account}")
            return True
        else:
            print(f"❌ שגיאה בהעברת כספים: {res.text}")
    except Exception as e:
        print(f"Error transferring funds: {e}")
    return False

def set_futures_leverage(symbol, leverage=LEVERAGE):
    """הגדרת המנוף ב-Futures"""
    url = "https://fapi.binance.com/fapi/v1/leverage"
    params = {
        'symbol': symbol,
        'leverage': leverage,
        'timestamp': int(time.time() * 1000)
    }
    params['signature'] = generate_signature(params)
    try:
        requests.post(url, headers=HEADERS, params=params, timeout=5)
    except Exception as e:
        print(f"Error setting leverage: {e}")

# ------------------------------------------------------------------
# פונקציות סריקה ומחירים
# ------------------------------------------------------------------
def update_main_prices():
    """שליפה מרוכזת של המחירים הראשיים למטמון"""
    now = time.time()
    if now - price_cache['last_update'] > 15:
        try:
            res = requests.get("https://api1.binance.com/api/v3/ticker/price", headers=HEADERS, timeout=5)
            if res.status_code == 200:
                data = {item['symbol']: float(item['price']) for item in res.json()}
                price_cache['BTCUSDT'] = data.get('BTCUSDT', price_cache['BTCUSDT'])
                price_cache['ETHUSDT'] = data.get('ETHUSDT', price_cache['ETHUSDT'])
                price_cache['SOLUSDT'] = data.get('SOLUSDT', price_cache['SOLUSDT'])
                price_cache['last_update'] = now
        except Exception as e:
            print(f"Cache update error: {e}")

def get_crypto_price(symbol):
    """שליפת מחיר יחיד מ-Futures API"""
    try:
        res = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", params={'symbol': symbol}, headers=HEADERS, timeout=4)
        if res.status_code == 200:
            return float(res.json()['price'])
    except Exception:
        pass
    return None

def get_market_sentiment():
    """שליפת סנטימנט השוק (Fear & Greed Index)"""
    try:
        res = requests.get('https://api.alternative.me/fng/', headers=HEADERS, timeout=4).json()
        val = int(res['data'][0]['value'])
        txt = res['data'][0]['value_classification']
        return val, txt
    except Exception:
        return 50, "Neutral"

def get_top_volume_candidates():
    """שליפת מועמדים בעלי נזילות גבוהה ב-Bulk"""
    candidates = []
    try:
        res = requests.get("https://api1.binance.com/api/v3/ticker/24hr", headers=HEADERS, timeout=8)
        if res.status_code == 200:
            data = res.json()
            for ticker in data:
                symbol = ticker['symbol']
                if symbol.endswith('USDT') and not any(x in symbol for x in ['UPUSDT', 'DOWNUSDT', 'BEAR', 'BULL', 'USDCUSDT']):
                    quote_volume = float(ticker['quoteVolume'])
                    if quote_volume >= MIN_DAILY_VOLUME:
                        candidates.append((symbol, quote_volume))
            
            candidates.sort(key=lambda x: x[1], reverse=True)
            return [c[0] for c in candidates]
    except Exception as e:
        print(f"Error fetching candidates: {e}")
    
    return ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'PEPEUSDT', 'NEARUSDT']

def get_volume_spike_ratio(symbol):
    """חישוב יחס קפיצת נפח המסחר ב-5 דקות האחרונות"""
    try:
        res = requests.get("https://api1.binance.com/api/v3/klines", params={'symbol': symbol, 'interval': '5m', 'limit': 6}, headers=HEADERS, timeout=4)
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
# לולאת המסחר האוטומטית (Spot & Futures)
# ------------------------------------------------------------------
def auto_trading_loop():
    global TRADING_ACTIVE, position
    while True:
        try:
            update_main_prices()

            if TRADING_ACTIVE:
                # 1. ניהול פוזיציה פתוחה
                if position['in_trade']:
                    sym = position['symbol']
                    current_price = get_crypto_price(sym)

                    if current_price:
                        entry = position['entry_price']
                        side = position['side']

                        close_trade = False
                        if side == 'LONG':
                            if current_price > position['highest_price']:
                                position['highest_price'] = current_price
                            trailing_stop = position['highest_price'] * (1 - TRAILING_STOP_PCT)
                            hard_stop = entry * (1 - STOP_LOSS_PCT)
                            if current_price <= hard_stop or (current_price <= trailing_stop and position['highest_price'] > entry):
                                close_trade = True

                        elif side == 'SHORT':
                            if current_price < position['lowest_price']:
                                position['lowest_price'] = current_price
                            trailing_stop = position['lowest_price'] * (1 + TRAILING_STOP_PCT)
                            hard_stop = entry * (1 + STOP_LOSS_PCT)
                            if current_price >= hard_stop or (current_price >= trailing_stop and position['lowest_price'] < entry):
                                close_trade = True

                        if close_trade:
                            position['in_trade'] = False
                            print(f"🛑 פוזיציית {side} נסגרה ב-Futures על {sym}")
                            # החזרת הכסף מ-Futures ל-Spot בסיום העסקה
                            transfer_funds('UMFUTURE', 'MAIN', BASE_USDT_SIZE)

                # 2. סריקת השוק ופתיחת עסקה ב-Futures
                else:
                    sentiment_val, sentiment_text = get_market_sentiment()
                    candidates = get_top_volume_candidates()

                    best_symbol = None
                    max_spike = 1.0

                    for sym in candidates[:25]:
                        spike_ratio = get_volume_spike_ratio(sym)
                        if spike_ratio > max_spike:
                            max_spike = spike_ratio
                            best_symbol = sym
                        time.sleep(0.1)

                    if best_symbol and max_spike >= 2.0:
                        current_price = get_crypto_price(best_symbol)
                        if current_price:
                            # העברת USDT מ-Spot ל-Futures עבור העסקה
                            transfer_funds('MAIN', 'UMFUTURE', BASE_USDT_SIZE)
                            set_futures_leverage(best_symbol, LEVERAGE)

                            side = 'SHORT' if sentiment_val < 35 else ('LONG' if sentiment_val > 65 else None)
                            
                            if side:
                                position['in_trade'] = True
                                position['symbol'] = best_symbol
                                position['side'] = side
                                position['entry_price'] = current_price
                                position['highest_price'] = current_price
                                position['lowest_price'] = current_price
                                position['amount'] = round((BASE_USDT_SIZE * LEVERAGE) / current_price, 5)
                                print(f"🚀 נפתחה פוזיציית Futures {side} על {best_symbol} (Spike: {max_spike:.1f}x)")

        except Exception as e:
            print(f"Error in trading loop: {e}")

        time.sleep(15)

# הפעלת לולאת הרקע
thread = Thread(target=auto_trading_loop, daemon=True)
thread.start()

# ------------------------------------------------------------------
# ממשק WhatsApp Webhook
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def home():
    return "Dynamic Futures Crypto Algo Bot is Live!"

@app.route('/webhook', methods=['POST'])
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global TRADING_ACTIVE, position
    raw_msg = request.values.get('Body', '').strip().lower()
    resp = MessagingResponse()
    msg = resp.message()

    # בדיקת IP מהירה ועמידה
    if raw_msg in ['ip', 'אייפי', 'מה ה ip', 'hp']:
        try:
            my_ip = requests.get('https://api.ipify.org', timeout=5).text.strip()
            msg.body(f"🌐 *כתובת ה-IP של השרת ב-Render היא:*\n`{my_ip}`\n\nהעתק אותה והדבק ב-Binance בתוך Restrict access to trusted IPs only!")
        except Exception as e:
            msg.body(f"❌ שגיאה בשליפת IP: {e}")

    elif raw_msg in ['סטטוס', 'status', 'מה המצב']:
        state = "🟢 פעיל" if TRADING_ACTIVE else "🔴 מוקפא"
        val, text = get_market_sentiment()
        
        update_main_prices()
        btc_p = price_cache['BTCUSDT']
        eth_p = price_cache['ETHUSDT']
        sol_p = price_cache['SOLUSDT']
        
        btc_str = f"${btc_p:,.2f}" if btc_p > 0 else "טוען..."
        eth_str = f"${eth_p:,.2f}" if eth_p > 0 else "טוען..."
        sol_str = f"${sol_p:,.2f}" if sol_p > 0 else "טוען..."

        pos_info = "🔍 סורק בלייב מאות מטבעות לזיהוי פריצה/Volume Spike"
        if position['in_trade']:
            peak_info = f"שיא: ${position['highest_price']}" if position['side'] == 'LONG' else f"שפל: ${position['lowest_price']}"
            pos_info = f"🔥 **בעסקה (Futures X{LEVERAGE}):** {position['side']} על **{position['symbol']}**\n• מחיר כניסה: ${position['entry_price']}\n• {peak_info}"

        msg.body(f"🧠 *סטטוס סורק מטבעות Futures:*\n• מצב: {state}\n• סנטימנט שוק: {text} ({val}/100)\n• מנוף מוגדר: X{LEVERAGE}\n\n📊 *אינדיקטורים ראשיים:*\n• BTC: {btc_str}\n• ETH: {eth_str}\n• SOL: {sol_str}\n\n📌 *פוזיציה נוכחית:*\n{pos_info}")

    elif raw_msg in ['עצור', 'stop', 'חרום']:
        TRADING_ACTIVE = False
        msg.body("🛑 המסחר האוטומטי הוקפא!")

    elif raw_msg in ['הפעל', 'start', 'רוץ']:
        TRADING_ACTIVE = True
        msg.body("🟢 הבוט הופעל מחדש ומנטר את כל המטבעות בשוק!")

    elif raw_msg in ['סגור הכל', 'close']:
        if position['in_trade']:
            sym = position['symbol']
            position['in_trade'] = False
            transfer_funds('UMFUTURE', 'MAIN', BASE_USDT_SIZE)
            msg.body(f"⚠️ הפוזיציה על {sym} נסגרה והכספים הוחזרו ל-Spot.")
        else:
            msg.body("אין פוזיציה פתוחה כרגע.")

    else:
        msg.body("📌 פקודות WhatsApp זמינות:\n• `ip` - שליפת כתובת ה-IP של השרת ל-Binance\n• `סטטוס` - בדיקת סורק המטבעות ופוזיציות\n• `עצור` - הקפאת הבוט\n• `הפעל` - הפעלת הבוט\n• `סגור הכל` - סגירת פוזיציה קיימת")

    return str(resp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
