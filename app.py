import os
import time
import hmac
import hashlib
import requests
import numpy as np
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

app = Flask(__name__)

# ==============================================================================
# הגדרות ומשתני סביבה (נלקחים מ-Render/Environment Variables)
# ==============================================================================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '').strip()
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
MY_PHONE_NUMBER = os.environ.get('MY_PHONE_NUMBER', '').strip()

# פרמטרים של ניהול סיכונים
TRADING_ACTIVE = True               # פעיל / מוקפא
MAX_DAILY_LOSS_PCT = 0.03          # מקסימום הפסד יומי מותר (3%)
BASE_USDT_SIZE = 25.0              # גודל עסקה בדולרים
LEVERAGE = 5                        # מנוף X5
MIN_DAILY_VOLUME = 5000000          # מינימום 5 מיליון דולר נפח יומי למניעת מניפולציות

# מעקב סיכונים ופוזיציות
account_guard = {
    'start_balance': 0.0,
    'daily_pnl': 0.0,
    'lock_until': None
}

position = {
    'in_trade': False,
    'symbol': None,
    'side': None,
    'entry_price': 0.0,
    'highest_price': 0.0,
    'lowest_price': 0.0,
    'quantity': 0.0,
    'stop_loss_price': 0.0,
    'stop_order_id': None
}

HEADERS = {
    'X-MBX-APIKEY': BINANCE_API_KEY,
    'User-Agent': 'QuantBot/2.0'
}

# ==============================================================================
# התראות WhatsApp
# ==============================================================================
def send_whatsapp_alert(message_text):
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and MY_PHONE_NUMBER:
        try:
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.messages.create(
                from_='whatsapp:+14155238886',
                body=message_text,
                to=MY_PHONE_NUMBER
            )
        except Exception as e:
            print(f"❌ WhatsApp Error: {e}")

# ==============================================================================
# תקשורת מול BINANCE Futures API
# ==============================================================================
def generate_signature(params):
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    return hmac.new(BINANCE_SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def binance_futures_request(method, endpoint, params=None):
    if params is None:
        params = {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = generate_signature(params)
    url = f"https://fapi.binance.com{endpoint}"
    
    try:
        if method == 'GET':
            res = requests.get(url, headers=HEADERS, params=params, timeout=5)
        elif method == 'POST':
            res = requests.post(url, headers=HEADERS, params=params, timeout=5)
        elif method == 'DELETE':
            res = requests.delete(url, headers=HEADERS, params=params, timeout=5)
        return res.json()
    except Exception as e:
        print(f"Binance API Request Error: {e}")
        return None

def get_account_balance():
    """שליפת יתרת USDT זמינה בחוזים עתידיים"""
    res = binance_futures_request('GET', '/fapi/v2/balance')
    if res and isinstance(res, list):
        for asset in res:
            if asset.get('asset') == 'USDT':
                return float(asset.get('balance', 0.0))
    return 0.0

def get_symbol_precision(symbol):
    try:
        res = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=5).json()
        for s in res['symbols']:
            if s['symbol'] == symbol:
                return int(s['quantityPrecision']), int(s['pricePrecision'])
    except Exception:
        pass
    return 3, 2

# ==============================================================================
# ניתוח נזילות ומשטר שוק (Market Regime & Order Book)
# ==============================================================================
def get_market_regime():
    """בדיקת המגמה הכללית של הביטקוין (BTCUSDT) להגדרת כיוון השוק"""
    try:
        res = requests.get("https://fapi.binance.com/fapi/v1/klines", 
                           params={'symbol': 'BTCUSDT', 'interval': '1h', 'limit': 50}, timeout=5).json()
        closes = np.array([float(k[4]) for k in res])
        ema20 = np.mean(closes[-20:])
        current_btc = closes[-1]
        
        if current_btc > ema20 * 1.002:
            return 'BULLISH'
        elif current_btc < ema20 * 0.998:
            return 'BEARISH'
        else:
            return 'NEUTRAL'
    except Exception:
        return 'NEUTRAL'

def check_order_book_depth(symbol, side, amount_usdt):
    """בדיקה שספר הפקודות מספיק עמוק כדי למנוע Slippage (החלקה של המחיר)"""
    try:
        res = requests.get("https://fapi.binance.com/fapi/v1/depth", 
                           params={'symbol': symbol, 'limit': 10}, timeout=5).json()
        bids = res.get('bids', [])
        asks = res.get('asks', [])
        
        target_list = asks if side == 'BUY' else bids
        available_depth = sum([float(item[0]) * float(item[1]) for item in target_list])
        
        return available_depth >= (amount_usdt * 5)
    except Exception:
        return False

# ==============================================================================
# חישוב אינדיקטורים טכניים (EMA 200, EMA 20, RSI, ATR, Volume Spike)
# ==============================================================================
def calculate_indicators(symbol):
    try:
        res = requests.get("https://fapi.binance.com/fapi/v1/klines", 
                           params={'symbol': symbol, 'interval': '5m', 'limit': 250}, headers=HEADERS, timeout=5)
        if res.status_code != 200:
            return None
        klines = res.json()
    except Exception:
        return None

    if not klines or len(klines) < 200:
        return None

    closes = np.array([float(k[4]) for k in klines])
    highs = np.array([float(k[2]) for k in klines])
    lows = np.array([float(k[3]) for k in klines])
    volumes = np.array([float(k[5]) for k in klines])

    # 1. קפיצת נפח (Volume Spike)
    avg_vol = np.mean(volumes[-7:-1])
    vol_spike = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

    # 2. ממוצעים נעים (EMA 20 + EMA 200)
    ema20 = np.mean(closes[-20:])
    ema200 = np.mean(closes[-200:])

    # 3. מדד עוצמה יחסית (RSI 14)
    deltas = np.diff(closes)
    seed = deltas[:14]
    up = seed[seed >= 0].sum() / 14
    down = -seed[seed < 0].sum() / 14
    rs = up / down if down != 0 else 0
    rsi = 100. - (100. / (1. + rs))

    # 4. מדד תנודתיות (ATR 14)
    tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    atr = np.mean(tr[-14:])

    return {
        'price': closes[-1],
        'vol_spike': vol_spike,
        'ema20': ema20,
        'ema200': ema200,
        'rsi': rsi,
        'atr': atr
    }

# ==============================================================================
# לולאת המסחר הראשית (Quantitative Autonomous Loop)
# ==============================================================================
def auto_trading_loop():
    global TRADING_ACTIVE, position, account_guard

    account_guard['start_balance'] = get_account_balance()

    while True:
        try:
            now = datetime.now()

            # 1. בדיקת מנגנון נעילת הפסד יומי
            if account_guard['lock_until']:
                if now < account_guard['lock_until']:
                    time.sleep(30)
                    continue
                else:
                    account_guard['lock_until'] = None
                    account_guard['start_balance'] = get_account_balance()
                    account_guard['daily_pnl'] = 0.0

            if TRADING_ACTIVE:

                # 2. מעקב וניהול פוזיציה פתוחה בלייב
                if position['in_trade']:
                    sym = position['symbol']
                    data = calculate_indicators(sym)

                    if data:
                        current_price = data['price']
                        entry = position['entry_price']
                        side = position['side']
                        atr = data['atr']

                        close_trade = False
                        reason = ""

                        if side == 'LONG':
                            if current_price > position['highest_price']:
                                position['highest_price'] = current_price

                            trailing_stop = position['highest_price'] - (atr * 1.5)
                            hard_stop = position['stop_loss_price']

                            if current_price <= hard_stop:
                                close_trade = True
                                reason = "Stop Loss Hit (ATR Protected)"
                            elif current_price <= trailing_stop and position['highest_price'] > (entry + (atr * 1.2)):
                                close_trade = True
                                reason = "Trailing Stop Hit (Profit Secured)"

                        elif side == 'SHORT':
                            if current_price < position['lowest_price']:
                                position['lowest_price'] = current_price

                            trailing_stop = position['lowest_price'] + (atr * 1.5)
                            hard_stop = position['stop_loss_price']

                            if current_price >= hard_stop:
                                close_trade = True
                                reason = "Stop Loss Hit (ATR Protected)"
                            elif current_price <= trailing_stop and position['lowest_price'] < (entry - (atr * 1.2)):
                                close_trade = True
                                reason = "Trailing Stop Hit (Profit Secured)"

                        if close_trade:
                            if position['stop_order_id']:
                                binance_futures_request('DELETE', '/fapi/v1/order', 
                                                        {'symbol': sym, 'orderId': position['stop_order_id']})

                            close_side = 'SELL' if side == 'LONG' else 'BUY'
                            binance_futures_request('POST', '/fapi/v1/order', {
                                'symbol': sym, 'side': close_side, 'type': 'MARKET',
                                'quantity': str(position['quantity']), 'reduceOnly': 'true'
                            })

                            pnl = (current_price - entry) * position['quantity'] if side == 'LONG' else (entry - current_price) * position['quantity']
                            account_guard['daily_pnl'] += pnl

                            if account_guard['start_balance'] > 0:
                                loss_ratio = abs(account_guard['daily_pnl']) / account_guard['start_balance']
                                if account_guard['daily_pnl'] < 0 and loss_ratio >= MAX_DAILY_LOSS_PCT:
                                    account_guard['lock_until'] = now + timedelta(hours=24)
                                    send_whatsapp_alert(f"🛑 *נעילת הגנה הופעלה!*\n\nהגעת להפסד יומי של {loss_ratio*100:.1f}%. המסחר ננעל ל-24 שעות הקרובות להגנה על החשבון.")

                            send_whatsapp_alert(f"🏁 *עסקה נסגרה ב-Binance!*\n\n• מטבע: {sym}\n• סוג: {side}\n• מחיר יציאה: ${current_price}\n• PnL מוערך: ${pnl:.2f}\n• סיבה: {reason}")
                            position['in_trade'] = False

                # 3. סריקת הזדמנויות כניסה חדשות
                else:
                    regime = get_market_regime()
                    
                    try:
                        tickers = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", headers=HEADERS, timeout=5).json()
                    except Exception:
                        tickers = []

                    for ticker in tickers:
                        sym = ticker['symbol']
                        if not sym.endswith('USDT') or any(x in sym for x in ['UPUSDT', 'DOWNUSDT', 'USDCUSDT']):
                            continue

                        if float(ticker['quoteVolume']) < MIN_DAILY_VOLUME:
                            continue

                        data = calculate_indicators(sym)
                        if not data:
                            continue

                        price = data['price']
                        spike = data['vol_spike']
                        ema20 = data['ema20']
                        ema200 = data['ema200']
                        rsi = data['rsi']
                        atr = data['atr']

                        # כניסה ל-LONG (מחיר מעל EMA 200 + EMA 20, RSI בריא, קפיצת נפח)
                        if regime != 'BEARISH' and spike >= 2.0 and price > ema200 and price > ema20 and (52 <= rsi <= 68):
                            if check_order_book_depth(sym, 'BUY', BASE_USDT_SIZE * LEVERAGE):
                                q_prec, p_prec = get_symbol_precision(sym)
                                qty = round((BASE_USDT_SIZE * LEVERAGE) / price, q_prec)

                                if qty > 0:
                                    binance_futures_request('POST', '/fapi/v1/leverage', {'symbol': sym, 'leverage': LEVERAGE})
                                    res = binance_futures_request('POST', '/fapi/v1/order', {
                                        'symbol': sym, 'side': 'BUY', 'type': 'MARKET', 'quantity': str(qty)
                                    })

                                    if res and 'orderId' in res:
                                        sl_price = round(price - (atr * 2.0), p_prec)
                                        
                                        sl_res = binance_futures_request('POST', '/fapi/v1/order', {
                                            'symbol': sym, 'side': 'SELL', 'type': 'STOP_MARKET',
                                            'stopPrice': str(sl_price), 'closePosition': 'true'
                                        })

                                        position['in_trade'] = True
                                        position['symbol'] = sym
                                        position['side'] = 'LONG'
                                        position['entry_price'] = price
                                        position['highest_price'] = price
                                        position['quantity'] = qty
                                        position['stop_loss_price'] = sl_price
                                        position['stop_order_id'] = sl_res.get('orderId') if sl_res else None

                                        send_whatsapp_alert(f"🚀 *עסקת LONG מוסדית נפתחה!*\n\n• מטבע: *{sym}*\n• מחיר כניסה: ${price}\n• קפיצת נפח: פי {spike:.1f}\n• מעל EMA200: כן\n• Stop Loss ב-Binance: ${sl_price}")
                                        break

                        # כניסה ל-SHORT (מחיר מתחת ל-EMA 200 + EMA 20, RSI בריא, קפיצת נפח)
                        elif regime != 'BULLISH' and spike >= 2.0 and price < ema200 and price < ema20 and (32 <= rsi <= 48):
                            if check_order_book_depth(sym, 'SELL', BASE_USDT_SIZE * LEVERAGE):
                                q_prec, p_prec = get_symbol_precision(sym)
                                qty = round((BASE_USDT_SIZE * LEVERAGE) / price, q_prec)

                                if qty > 0:
                                    binance_futures_request('POST', '/fapi/v1/leverage', {'symbol': sym, 'leverage': LEVERAGE})
                                    res = binance_futures_request('POST', '/fapi/v1/order', {
                                        'symbol': sym, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(qty)
                                    })

                                    if res and 'orderId' in res:
                                        sl_price = round(price + (atr * 2.0), p_prec)
                                        
                                        sl_res = binance_futures_request('POST', '/fapi/v1/order', {
                                            'symbol': sym, 'side': 'BUY', 'type': 'STOP_MARKET',
                                            'stopPrice': str(sl_price), 'closePosition': 'true'
                                        })

                                        position['in_trade'] = True
                                        position['symbol'] = sym
                                        position['side'] = 'SHORT'
                                        position['entry_price'] = price
                                        position['lowest_price'] = price
                                        position['quantity'] = qty
                                        position['stop_loss_price'] = sl_price
                                        position['stop_order_id'] = sl_res.get('orderId') if sl_res else None

                                        send_whatsapp_alert(f"📉 *עסקת SHORT מוסדית נפתחה!*\n\n• מטבע: *{sym}*\n• מחיר כניסה: ${price}\n• קפיצת נפח: פי {spike:.1f}\n• מתחת ל-EMA200: כן\n• Stop Loss ב-Binance: ${sl_price}")
                                        break

                        time.sleep(0.05)

        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(10)

# הפעלת הלולאה ברקע
thread = Thread(target=auto_trading_loop, daemon=True)
thread.start()

# ==============================================================================
# ממשק WhatsApp Webhook
# ==============================================================================
@app.route('/', methods=['GET'])
def home():
    return "Institutional Quantitative Bot Running!"

@app.route('/webhook', methods=['POST'])
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global TRADING_ACTIVE, position, account_guard
    raw_msg = request.values.get('Body', '').strip().lower()
    resp = MessagingResponse()
    msg = resp.message()

    if raw_msg in ['סטטוס', 'status']:
        state = "🟢 פעיל (סורק עצמאי)" if TRADING_ACTIVE else "🔴 מוקפא"
        if account_guard['lock_until']:
            state = f"🔒 ננעל להגנה עד {account_guard['lock_until'].strftime('%H:%M')}"

        pos_info = "🔍 סורק את השוק וממתין להזדמנות כניסה..."
        if position['in_trade']:
            pos_info = f"🔥 **פוזיציה בלייב:** {position['side']} על **{position['symbol']}**\n• מחיר כניסה: ${position['entry_price']}\n• כמות: {position['quantity']}\n• Stop Loss ב-Binance: ${position['stop_loss_price']}"

        msg.body(f"📊 *סטטוס הבוט:* {state}\n• PnL יומי: ${account_guard['daily_pnl']:.2f}\n\n📌 *מצב נוכחי:*\n{pos_info}")

    elif raw_msg in ['עצור', 'stop']:
        TRADING_ACTIVE = False
        msg.body("🛑 המסחר הוקפא! הבוט לא יפתח עסקאות חדשות.")

    elif raw_msg in ['הפעל', 'start']:
        TRADING_ACTIVE = True
        account_guard['lock_until'] = None
        msg.body("🟢 הבוט הופעל מחדש וחוזר לסרוק עצמאית!")

    elif raw_msg in ['סגור הכל', 'close']:
        if position['in_trade']:
            sym = position['symbol']
            close_side = 'SELL' if position['side'] == 'LONG' else 'BUY'
            binance_futures_request('POST', '/fapi/v1/order', {
                'symbol': sym, 'side': close_side, 'type': 'MARKET',
                'quantity': str(position['quantity']), 'reduceOnly': 'true'
            })
            position['in_trade'] = False
            TRADING_ACTIVE = False
            msg.body(f"⚠️ הפוזיציה על {sym} נסגרה בלייב והמסחר הופסק!")
        else:
            TRADING_ACTIVE = False
            msg.body("אין פוזיציה פתוחה. המסחר הופסק.")

    else:
        msg.body("📌 פקודות זמינות:\n• `סטטוס`\n• `עצור`\n• `הפעל`\n• `סגור הכל`")

    return str(resp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
