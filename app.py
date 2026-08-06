import os
import time
import hmac
import math
import hashlib
import requests
import numpy as np
from datetime import datetime
from threading import Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request

app = Flask(__name__)

# ==============================================================================
# הגדרות ומשתני סביבה (Telegram & Binance)
# ==============================================================================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '').strip()
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()

# פרמטרים של מסחר וניהול סיכונים
TRADING_ACTIVE = True
MAX_DAILY_LOSS_PCT = 0.03          # מקסימום הפסד יומי (3%)
BASE_USDT_SIZE = 25.0              # גודל עסקה בדולרים
LEVERAGE = 5                        # מנוף X5 ב-Futures
MIN_DAILY_VOLUME = 5000000          # מינימום 5M$ נפח יומי
MAX_PARALLEL_THREADS = 20          # כמות מטבעות שנסרקים במקביל

symbol_info_cache = {}

account_guard = {
    'start_balance': 0.0,
    'daily_pnl': 0.0,
    'lock_until': None
}

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
    'User-Agent': 'QuantBot/5.0'
}

# ==============================================================================
# התראות Telegram
# ==============================================================================
def send_telegram_alert(message_text):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message_text,
                'parse_mode': 'Markdown'
            }
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"❌ Telegram Error: {e}")

# ==============================================================================
# תקשורת מול BINANCE
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
# בדיקת פילטרים ואימות גודל פוזיציה
# ==============================================================================
def get_symbol_filters(symbol, is_futures=True):
    cache_key = f"{symbol}_{'FUT' if is_futures else 'SPOT'}"
    if cache_key in symbol_info_cache:
        return symbol_info_cache[cache_key]

    endpoint = "/fapi/v1/exchangeInfo" if is_futures else "/api/v3/exchangeInfo"
    base_url = "https://fapi.binance.com" if is_futures else "https://api.binance.com"
    
    step_size, min_qty, min_notional = 0.001, 0.0, (5.0 if is_futures else 10.0)
    
    try:
        res = requests.get(f"{base_url}{endpoint}", headers=HEADERS, timeout=5).json()
        for s in res.get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        min_qty = float(f['minQty'])
                    elif f['filterType'] in ['MIN_NOTIONAL', 'NOTIONAL']:
                        min_notional = float(f.get('minNotional', f.get('notional', 5.0 if is_futures else 10.0)))
                
                symbol_info_cache[cache_key] = (step_size, min_qty, min_notional)
                return step_size, min_qty, min_notional
    except Exception as e:
        print(f"❌ שגיאה בשליפת פילטרים עבור {symbol}: {e}")
        
    return step_size, min_qty, min_notional

def validate_and_format_trade(symbol, current_price, target_usdt_size, is_futures=True, auto_bump=True):
    step_size, min_qty, min_notional = get_symbol_filters(symbol, is_futures)
    notional_value = target_usdt_size
    
    if notional_value < min_notional:
        if auto_bump:
            notional_value = min_notional + 0.1
        else:
            return 0.0, False

    raw_qty = notional_value / current_price
    if raw_qty < min_qty:
        return 0.0, False

    precision = int(round(-math.log10(step_size))) if step_size > 0 else 3
    if precision <= 0:
        qty = float(math.floor(raw_qty))
    else:
        factor = 10 ** precision
        qty = round(math.floor(raw_qty * factor) / factor, precision)

    if (qty * current_price) < min_notional:
        qty = round(qty + step_size, precision)

    return qty, True

def safe_close_spot_position(symbol, quantity, current_price):
    step_size, min_qty, min_notional = get_symbol_filters(symbol, is_futures=False)
    current_notional = quantity * current_price

    if current_notional < min_notional:
        needed_usdt = (min_notional - current_notional) + 1.0
        buy_qty, is_valid = validate_and_format_trade(symbol, current_price, needed_usdt, is_futures=False)
        
        if is_valid and buy_qty > 0:
            binance_request('POST', '/api/v3/order', {
                'symbol': symbol, 'side': 'BUY', 'type': 'MARKET', 'quantity': str(buy_qty)
            }, is_futures=False)
            quantity = round(quantity + buy_qty, 8)

    precision = int(round(-math.log10(step_size))) if step_size > 0 else 3
    if precision <= 0:
        final_qty = float(math.floor(quantity))
    else:
        factor = 10 ** precision
        final_qty = round(math.floor(quantity * factor) / factor, precision)

    return binance_request('POST', '/api/v3/order', {
        'symbol': symbol, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(final_qty)
    }, is_futures=False)

# ==============================================================================
# אינדיקטורים
# ==============================================================================
def calculate_rsi_wilder(closes, period=14):
    closes = np.array(closes, dtype=float)
    if len(closes) < period + 1:
        return 50.0

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)

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
    rsi = calculate_rsi_wilder(closes, period=14)

    tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    atr = np.mean(tr[-14:])

    return {
        'symbol': symbol, 'is_futures': is_futures, 'price': closes[-1],
        'vol_spike': vol_spike, 'ema20': ema20, 'ema200': ema200, 'rsi': rsi, 'atr': atr
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

    if spike >= 1.5 and price > ema200 and price > ema20 and (50 <= rsi <= 70):
        data['signal'] = 'LONG'
        return data
    elif is_futures and spike >= 1.5 and price < ema200 and price < ema20 and (30 <= rsi <= 50):
        data['signal'] = 'SHORT'
        return data

    return None

# ==============================================================================
# לולאת המסחר
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
                                    safe_close_spot_position(sym, pos['quantity'], current_price)

                                pnl = (current_price - entry) * pos['quantity'] if side == 'LONG' else (entry - current_price) * pos['quantity']
                                account_guard['daily_pnl'] += pnl

                                send_telegram_alert(f"🏁 *עסקה נסגרה ב-{m_type}!*\n\n• מטבע: {sym}\n• סוג: {side}\n• מחיר יציאה: ${current_price}\n• PnL מוערך: ${pnl:.2f}\n• סיבה: {reason}")
                                pos['in_trade'] = False

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
                                        target_usdt = BASE_USDT_SIZE * LEVERAGE
                                        qty, is_valid = validate_and_format_trade(sym, price, target_usdt, is_futures=True)
                                        
                                        if is_valid and qty > 0:
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
                                                send_telegram_alert(f"🚀 *עסקת FUTURES נפתחה!*\n\n• מטבע: *{sym}*\n• סוג: {signal}\n• מחיר כניסה: ${price}\n• קפיצת נפח: פי {spike:.1f}\n• Stop Loss: ${sl_price}")

                                    else:
                                        target_usdt = BASE_USDT_SIZE
                                        qty, is_valid = validate_and_format_trade(sym, price, target_usdt, is_futures=False)
                                        
                                        if is_valid and qty > 0:
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
                                                send_telegram_alert(f"🚀 *עסקת SPOT נפתחה!*\n\n• מטבע: *{sym}*\n• סוג: LONG\n• מחיר כניסה: ${price}\n• קפיצת נפח: פי {spike:.1f}\n• Stop Loss: ${sl_price}")

        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(5)

thread = Thread(target=auto_trading_loop, daemon=True)
thread.start()

# ==============================================================================
# Telegram Webhook (פקודות בעברית ובאנגלית)
# ==============================================================================
@app.route('/', methods=['GET'])
def home():
    return "Telegram Quant Bot Active!"

@app.route('/telegram', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    global TRADING_ACTIVE, positions
    data = request.get_json()

    if data and 'message' in data:
        raw_msg = data['message'].get('text', '').strip().lower()

        # פקודת סטטוס / status
        if raw_msg in ['סטטוס', 'status', '/status', 'מה המצב', 'מצב']:
            state = "🟢 פעיל (Spot + Futures)" if TRADING_ACTIVE else "🔴 מוקפא"
            fut_info = f"🔥 FUTURES: {positions['FUTURES']['side']} על {positions['FUTURES']['symbol']} (${positions['FUTURES']['entry_price']})" if positions['FUTURES']['in_trade'] else "🔍 FUTURES: סורק את השוק..."
            spot_info = f"🔥 SPOT: LONG על {positions['SPOT']['symbol']} (${positions['SPOT']['entry_price']})" if positions['SPOT']['in_trade'] else "🔍 SPOT: סורק את השוק..."

            send_telegram_alert(f"📊 *סטטוס הבוט:* {state}\n• PnL יומי מוערך: ${account_guard['daily_pnl']:.2f}\n\n📌 *מצב עסקאות:*\n• {fut_info}\n• {spot_info}")

        # פקודת עצירה / stop
        elif raw_msg in ['עצור', 'stop', '/stop', 'הקפא', 'עצור מסחר']:
            TRADING_ACTIVE = False
            send_telegram_alert("🛑 *המסחר הוקפא!* הבוט לא יפתח עסקאות חדשות.")

        # פקודת הפעלה / start
        elif raw_msg in ['הפעל', 'start', '/start', 'המשך', 'פתח מסחר']:
            TRADING_ACTIVE = True
            send_telegram_alert("🟢 *הבוט הופעל מחדש!* סריקת השוק פעילה.")

        # פקודת עזרה / help
        elif raw_msg in ['עזרה', 'help', '/help', 'פקודות']:
            send_telegram_alert("💡 *פקודות זמינות בטלגרם:*\n\n• *סטטוס* / *status*: הצגת מצב הבוט והעסקאות הפעילות\n• *עצור* / *stop*: הקפאת פתיחת עסקאות חדשות\n• *הפעל* / *start*: חידוש סריקת השוק והמסחר\n• *עזרה* / *help*: הצגת תפריט זה")

    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
