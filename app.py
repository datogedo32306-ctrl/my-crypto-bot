import os
import time
import hmac
import hashlib
import requests
import numpy as np
from datetime import datetime, timedelta
from threading import Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client

app = Flask(__name__)

# ==============================================================================
# הגדרות ומשתני סביבה
# ==============================================================================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '').strip()
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
MY_PHONE_NUMBER = os.environ.get('MY_PHONE_NUMBER', '').strip()

# פרמטרים של מסחר וניהול סיכונים
TRADING_ACTIVE = True
MAX_DAILY_LOSS_PCT = 0.03          # מקסימום הפסד יומי (3%)
BASE_USDT_SIZE = 25.0              # גודל עסקה בדולרים
LEVERAGE = 5                        # מנוף X5 ב-Futures
MIN_DAILY_VOLUME = 5000000          # מינימום 5M$ נפח יומי
MAX_PARALLEL_THREADS = 20          # כמות מטבעות שנסרקים במקביל

account_guard = {
    'start_balance': 0.0,
    'daily_pnl': 0.0,
    'lock_until': None
}

# ניהול פוזיציות נפרד ל-Spot ול-Futures
positions = {
    'FUTURES': {
        'in_trade': False, 'symbol': None, 'side': None, 'entry_price': 0.0,
        'highest_price': 0.0, 'lowest_price': 0.0, 'quantity': 0.0,
        'stop_loss_price': 0.0, 'stop_order_id': None
    },
    'SPOT': {
        'in_trade': False, 'symbol': None, 'side': 'LONG', 'entry_price': 0.0,
        'highest_price': 0.0, 'lowest_price': 0.0, 'quantity': 0.0,
        'stop_loss_price': 0.0, 'stop_order_id': None
    }
}

HEADERS = {
    'X-MBX-APIKEY': BINANCE_API_KEY,
    'User-Agent': 'QuantBot/4.0'
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
# תקשורת מול BINANCE (Spot & Futures)
# ==============================================================================
def generate_signature(params):
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    return hmac.new(BINANCE_SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def binance_request(method, endpoint, params=None, is_futures=True):
    if params is None:
        params = {}
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = generate_signature(params)
    
    base_url = "https://fapi.binance.com" if is_futures else "https://api.binance.com"
    url = f"{base_url}{endpoint}"
    
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
    fut_bal = 0.0
    spot_bal = 0.0
    
    res_fut = binance_request('GET', '/fapi/v2/balance', is_futures=True)
    if res_fut and isinstance(res_fut, list):
        for asset in res_fut:
            if asset.get('asset') == 'USDT':
                fut_bal = float(asset.get('balance', 0.0))

    res_spot = binance_request('GET', '/api/v3/account', is_futures=False)
    if res_spot and 'balances' in res_spot:
        for asset in res_spot['balances']:
            if asset.get('asset') == 'USDT':
                spot_bal = float(asset.get('free', 0.0))

    return fut_bal + spot_bal

# ==============================================================================
# חישוב אינדיקטורים טכניים
# ==============================================================================
def calculate_indicators(symbol, is_futures=True):
    endpoint = "/fapi/v1/klines" if is_futures else "/api/v3/klines"
    base_url = "https://fapi.binance.com" if is_futures else "https://api.binance.com"
    
    try:
        res = requests.get(f"{base_url}{endpoint}", 
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

    avg_vol = np.mean(volumes[-7:-1])
    vol_spike = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

    ema20 = np.mean(closes[-20:])
    ema200 = np.mean(closes[-200:])

    deltas = np.diff(closes)
    seed = deltas[:14]
    up = seed[seed >= 0].sum() / 14
    down = -seed[seed < 0].sum() / 14
    rs = up / down if down != 0 else 0
    rsi = 100. - (100. / (1. + rs))

    tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    atr = np.mean(tr[-14:])

    return {
        'symbol': symbol,
        'is_futures': is_futures,
        'price': closes[-1],
        'vol_spike': vol_spike,
        'ema20': ema20,
        'ema200': ema200,
        'rsi': rsi,
        'atr': atr
    }

def analyze_single_ticker(ticker_data):
    sym = ticker_data['symbol']
    is_futures = ticker_data['is_futures']

    if not sym.endswith('USDT') or any(x in sym for x in ['UPUSDT', 'DOWNUSDT', 'USDCUSDT', 'BUSDUSDT']):
        return None

    if float(ticker_data.get('quoteVolume', 0)) < MIN_DAILY_VOLUME:
        return None

    data = calculate_indicators(sym, is_futures=is_futures)
    if not data:
        return None

    price = data['price']
    spike = data['vol_spike']
    ema20 = data['ema20']
    ema200 = data['ema200']
    rsi = data['rsi']

    if spike >= 2.0 and price > ema200 and price > ema20 and (52 <= rsi <= 68):
        data['signal'] = 'LONG'
        return data

    elif is_futures and spike >= 2.0 and price < ema200 and price < ema20 and (32 <= rsi <= 48):
        data['signal'] = 'SHORT'
        return data

    return None

# ==============================================================================
# לולאת המסחר הראשית (איפשר עבודה במקביל ב-Futures וב-Spot + כניסה חוזרת)
# ==============================================================================
def auto_trading_loop():
    global TRADING_ACTIVE, positions, account_guard

    account_guard['start_balance'] = get_account_balance()

    while True:
        try:
            now = datetime.now()

            if account_guard['lock_until']:
                if now < account_guard['lock_until']:
                    time.sleep(30)
                    continue
                else:
                    account_guard['lock_until'] = None
                    account_guard['start_balance'] = get_account_balance()
                    account_guard['daily_pnl'] = 0.0

            if TRADING_ACTIVE:

                # 1. ניהול בלייב של שני השווקים במקביל
                for m_type in ['FUTURES', 'SPOT']:
                    pos = positions[m_type]
                    is_fut = (m_type == 'FUTURES')

                    if pos['in_trade']:
                        sym = pos['symbol']
                        data = calculate_indicators(sym, is_futures=is_fut)

                        if data:
                            current_price = data['price']
                            entry = pos['entry_price']
                            side = pos['side']
                            atr = data['atr']

                            close_trade = False
                            reason = ""

                            if side == 'LONG':
                                if current_price > pos['highest_price']:
                                    pos['highest_price'] = current_price

                                trailing_stop = pos['highest_price'] - (atr * 1.5)
                                if current_price <= pos['stop_loss_price']:
                                    close_trade = True
                                    reason = "Stop Loss Hit"
                                elif current_price <= trailing_stop and pos['highest_price'] > (entry + (atr * 1.2)):
                                    close_trade = True
                                    reason = "Trailing Stop Hit (Profit Secured)"

                            elif side == 'SHORT':
                                if current_price < pos['lowest_price']:
                                    pos['lowest_price'] = current_price

                                trailing_stop = pos['lowest_price'] + (atr * 1.5)
                                if current_price >= pos['stop_loss_price']:
                                    close_trade = True
                                    reason = "Stop Loss Hit"
                                elif current_price >= trailing_stop and pos['lowest_price'] < (entry - (atr * 1.2)):
                                    close_trade = True
                                    reason = "Trailing Stop Hit (Profit Secured)"

                            if close_trade:
                                if is_fut:
                                    if pos['stop_order_id']:
                                        binance_request('DELETE', '/fapi/v1/order', {'symbol': sym, 'orderId': pos['stop_order_id']}, is_futures=True)
                                    close_side = 'SELL' if side == 'LONG' else 'BUY'
                                    binance_request('POST', '/fapi/v1/order', {
                                        'symbol': sym, 'side': close_side, 'type': 'MARKET',
                                        'quantity': str(pos['quantity']), 'reduceOnly': 'true'
                                    }, is_futures=True)
                                else:
                                    binance_request('POST', '/api/v3/order', {
                                        'symbol': sym, 'side': 'SELL', 'type': 'MARKET',
                                        'quantity': str(pos['quantity'])
                                    }, is_futures=False)

                                pnl = (current_price - entry) * pos['quantity'] if side == 'LONG' else (entry - current_price) * pos['quantity']
                                account_guard['daily_pnl'] += pnl

                                send_whatsapp_alert(f"🏁 *עסקה נסגרה ב-{m_type}!*\n\n• מטבע: {sym}\n• סוג: {side}\n• מחיר יציאה: ${current_price}\n• PnL מוערך: ${pnl:.2f}\n• סיבה: {reason}")
                                
                                # איפוס הפוזיציה כדי לאפשר כניסה חוזרת מיידית (Re-entry) במידה ויש איתות חדש!
                                pos['in_trade'] = False

                # 2. סורק הזדמנויות חדשות (במידה ואחד השווקים פנוי)
                need_futures = not positions['FUTURES']['in_trade']
                need_spot = not positions['SPOT']['in_trade']

                if need_futures or need_spot:
                    all_tickers = []
                    
                    if need_futures:
                        try:
                            fut_tickers = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", headers=HEADERS, timeout=5).json()
                            for t in fut_tickers:
                                t['is_futures'] = True
                                all_tickers.append(t)
                        except Exception:
                            pass

                    if need_spot:
                        try:
                            spot_tickers = requests.get("https://api.binance.com/api/v3/ticker/24hr", headers=HEADERS, timeout=5).json()
                            fut_symbols = {t['symbol'] for t in all_tickers if t.get('is_futures')}
                            for t in spot_tickers:
                                if t['symbol'] not in fut_symbols:
                                    t['is_futures'] = False
                                    all_tickers.append(t)
                        except Exception:
                            pass

                    # סריקה מקבילית
                    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_THREADS) as executor:
                        future_to_ticker = {executor.submit(analyze_single_ticker, ticker): ticker for ticker in all_tickers}
                        for future in as_completed(future_to_ticker):
                            res = future.result()
                            if res and res.get('signal'):
                                is_fut = res['is_futures']
                                m_key = 'FUTURES' if is_fut else 'SPOT'

                                if not positions[m_key]['in_trade']:
                                    sym = res['symbol']
                                    price = res['price']
                                    signal = res['signal']
                                    spike = res['vol_spike']
                                    atr = res['atr']

                                    if is_fut:
                                        qty = round((BASE_USDT_SIZE * LEVERAGE) / price, 3)
                                        binance_request('POST', '/fapi/v1/leverage', {'symbol': sym, 'leverage': LEVERAGE}, is_futures=True)
                                        order_res = binance_request('POST', '/fapi/v1/order', {
                                            'symbol': sym, 'side': ('BUY' if signal == 'LONG' else 'SELL'), 
                                            'type': 'MARKET', 'quantity': str(qty)
                                        }, is_futures=True)

                                        if order_res and 'orderId' in order_res:
                                            sl_price = round(price - (atr * 2.0) if signal == 'LONG' else price + (atr * 2.0), 2)
                                            sl_res = binance_request('POST', '/fapi/v1/order', {
                                                'symbol': sym, 'side': ('SELL' if signal == 'LONG' else 'BUY'), 
                                                'type': 'STOP_MARKET', 'stopPrice': str(sl_price), 'closePosition': 'true'
                                            }, is_futures=True)

                                            positions['FUTURES'].update({
                                                'in_trade': True, 'symbol': sym, 'side': signal,
                                                'entry_price': price, 'highest_price': price, 'lowest_price': price,
                                                'quantity': qty, 'stop_loss_price': sl_price,
                                                'stop_order_id': sl_res.get('orderId') if sl_res else None
                                            })
                                            send_whatsapp_alert(f"🚀 *עסקת FUTURES נפתחה!*\n\n• מטבע: *{sym}*\n• סוג: {signal}\n• מחיר כניסה: ${price}\n• קפיצת נפח: פי {spike:.1f}\n• Stop Loss: ${sl_price}")

                                    else:
                                        qty = round(BASE_USDT_SIZE / price, 3)
                                        order_res = binance_request('POST', '/api/v3/order', {
                                            'symbol': sym, 'side': 'BUY', 'type': 'MARKET', 'quantity': str(qty)
                                        }, is_futures=False)

                                        if order_res and 'orderId' in order_res:
                                            sl_price = round(price - (atr * 2.0), 2)
                                            positions['SPOT'].update({
                                                'in_trade': True, 'symbol': sym, 'side': 'LONG',
                                                'entry_price': price, 'highest_price': price, 'lowest_price': price,
                                                'quantity': qty, 'stop_loss_price': sl_price, 'stop_order_id': None
                                            })
                                            send_whatsapp_alert(f"🚀 *עסקת SPOT נפתחה!*\n\n• מטבע: *{sym}*\n• סוג: LONG\n• מחיר כניסה: ${price}\n• קפיצת נפח: פי {spike:.1f}\n• Stop Loss: ${sl_price}")

        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(5)

# הפעלת הלולאה ברקע
thread = Thread(target=auto_trading_loop, daemon=True)
thread.start()

# ==============================================================================
# Webhook
# ==============================================================================
@app.route('/', methods=['GET'])
def home():
    return "Multi-Market Parallel Quant Bot Running!"

@app.route('/webhook', methods=['POST'])
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global TRADING_ACTIVE, positions
    raw_msg = request.values.get('Body', '').strip().lower()
    resp = MessagingResponse()
    msg = resp.message()

    if raw_msg in ['סטטוס', 'status']:
        state = "🟢 פעיל (Spot + Futures)" if TRADING_ACTIVE else "🔴 מוקפא"
        
        fut_info = f"🔥 FUTURES: {positions['FUTURES']['side']} על {positions['FUTURES']['symbol']} (${positions['FUTURES']['entry_price']})" if positions['FUTURES']['in_trade'] else "🔍 FUTURES: סורק..."
        spot_info = f"🔥 SPOT: LONG על {positions['SPOT']['symbol']} (${positions['SPOT']['entry_price']})" if positions['SPOT']['in_trade'] else "🔍 SPOT: סורק..."

        msg.body(f"📊 *סטטוס הבוט:* {state}\n• PnL יומי: ${account_guard['daily_pnl']:.2f}\n\n📌 *מצב נוכחי:*\n• {fut_info}\n• {spot_info}")

    elif raw_msg in ['עצור', 'stop']:
        TRADING_ACTIVE = False
        msg.body("🛑 המסחר הוקפא!")

    elif raw_msg in ['הפעל', 'start']:
        TRADING_ACTIVE = True
        msg.body("🟢 הבוט הופעל מחדש!")

    return str(resp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
