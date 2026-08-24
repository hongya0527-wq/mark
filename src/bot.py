import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# Render 專用：假網頁伺服器（解決免費 Web Service 的 Port 逾時問題）
# =========================================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Feidudu Zuoweimon Bot is running!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

# 在背景獨立執行緒中啟動假伺服器
server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()


# =========================================================
# Telegram
# =========================================================

TELEGRAM_BOT_TOKEN = "8834096490:AAGvSHCC8TNC_q4CXJZ4-fK-zyXlmHCPoKA"
TELEGRAM_CHAT_ID = "6273931436"

# =========================================================
# OKX
# =========================================================

OKX_BASE_URL = "https://www.okx.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# =========================================================
# 篩選設定
# =========================================================

# 🎯 24 小時成交量門檻 15,000,000 (1,500萬 USDT)
MIN_24H_VOLUME = 15_000_000
MAX_ALLOWABLE_FR = 0.0015
NEAR_RESISTANCE_PCT = 0.015
BREAKOUT_MIN_PCT = 0.003

VOL_NORMAL = 1.10
VOL_STRONG = 1.30
VOL_VERY_STRONG = 1.60

OI_STRONG = 2.0
OI_VERY_STRONG = 4.0

# =========================================================
# 全域 Session
# =========================================================

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25)
session.mount("https://", adapter)
session.mount("http://", adapter)

notified_signals = set()
previous_oi = {}

# =========================================================
# OKX GET
# =========================================================

def okx_get(endpoint, params=None, retries=2):
    url = f"{OKX_BASE_URL}{endpoint}"
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=5)
            if response.status_code == 429:
                time.sleep(1 + attempt)
                continue
            response.raise_for_status()
            data = response.json()
            if data.get("code") != "0":
                return []
            return data.get("data", [])
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.5)
    return []

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = session.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=5
        )
        return response.status_code == 200
    except Exception:
        return False

def format_price(price):
    if price >= 100: return f"{price:,.2f}"
    if price >= 1: return f"{price:,.4f}"
    if price >= 0.01: return f"{price:,.5f}"
    if price >= 0.0001: return f"{price:,.6f}"
    return f"{price:,.8f}"

def get_candles(symbol, bar, limit=80):
    data = okx_get("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)})
    if not data: return None
    candles = []
    for item in reversed(data):
        try:
            if len(item) < 9: continue
            candles.append({
                "timestamp": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "confirm": item[8]
            })
        except Exception:
            continue
    return candles

def get_funding_rate(symbol):
    data = okx_get("/api/v5/public/funding-rate", {"instId": symbol})
    if not data: return 0.0
    try:
        return float(data[0].get("fundingRate", 0))
    except Exception:
        return 0.0

def get_open_interest(symbol):
    data = okx_get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": symbol})
    if not data: return None
    try:
        oi = float(data[0].get("oi", 0))
        if oi <= 0: return None
        return oi
    except Exception:
        return None

def get_oi_change(symbol):
    current_oi = get_open_interest(symbol)
    if current_oi is None: return None, None
    old_oi = previous_oi.get(symbol)
    previous_oi[symbol] = current_oi
    if old_oi is None or old_oi <= 0: return current_oi, None
    change = ((current_oi - old_oi) / old_oi) * 100
    return current_oi, change

def check_btc_market_regime():
    candles = get_candles("BTC-USDT-SWAP", "4H", 50)
    if not candles: return "neutral"
    closed = [c for c in candles if c["confirm"] == "1"]
    if len(closed) < 25: return "neutral"
    closes = [c["close"] for c in closed]
    ema = sum(closes[:20]) / 20
    multiplier = 2 / 21
    for price in closes[20:]:
        ema = (price - ema) * multiplier + ema
    current = closes[-1]
    if current > ema * 1.01: return "bull"
    if current < ema * 0.99: return "bear"
    return "neutral"

def get_top_hot_symbols():
    tickers = okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
    if not tickers: return []
    exclude = ["SPX", "NDX", "QQQ", "AAPL", "TSLA", "NVDA", "GOLD", "OIL"]
    volume_list = []
    for item in tickers:
        symbol = item.get("instId")
        if not symbol or not symbol.endswith("-USDT-SWAP") or any(x in symbol for x in exclude): continue
        try:
            # 🎯 修復點：將「幣種成交量」乘上「最新價格」，計算出真實的 USDT 金額
            vol_ccy = float(item.get("volCcy24h", 0))
            last_price = float(item.get("last", 0))
            usdt_volume = vol_ccy * last_price
            
            if usdt_volume < MIN_24H_VOLUME: continue
            volume_list.append((symbol, usdt_volume))
        except Exception:
            continue
    top_volume = [x[0] for x in sorted(volume_list, key=lambda x: x[1], reverse=True)[:60]]
    return top_volume

def bullish_engulfing(candles):
    if len(candles) < 2: return False
    prev, curr = candles[-2], candles[-1]
    return (prev["close"] < prev["open"]) and (curr["close"] > curr["open"]) and (curr["open"] <= prev["close"]) and (curr["close"] >= prev["open"])

def higher_lows(candles):
    if len(candles) < 8: return False
    lows = [c["low"] for c in candles[-12:]]
    swing_lows = [lows[i] for i in range(1, len(lows) - 1) if lows[i] < lows[i - 1] and lows[i] <= lows[i + 1]]
    if len(swing_lows) < 2: return False
    return swing_lows[-1] > swing_lows[-2]

def volume_ratio(candles):
    if len(candles) < 7: return 0
    current = candles[-1]["volume"]
    previous = [c["volume"] for c in candles[-6:-1]]
    avg = sum(previous) / len(previous)
    return current / avg if avg > 0 else 0

def get_resistance(candles, lookback):
    if len(candles) < lookback + 3: return None
    data = candles[-lookback-2:-2]
    return max(c["high"] for c in data) if data else None

def breakout_status(candles, resistance):
    if not resistance: return "none"
    close = candles[-1]["close"]
    if close > resistance:
        pct = ((close - resistance) / resistance) * 100
        return "breakout" if pct >= BREAKOUT_MIN_PCT * 100 else "weak_breakout"
    distance = (resistance - close) / resistance
    return "near" if distance <= NEAR_RESISTANCE_PCT else "none"

def bad_breakout(candle, resistance):
    if resistance <= 0: return True
    body = abs(candle["close"] - candle["open"])
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    if body <= 0 or upper_wick > body * 1.5: return True
    return candle["high"] > resistance and candle["close"] < resistance

def retest_confirmation(candles, resistance):
    if len(candles) < 3: return False
    current, previous = candles[-1], candles[-2]
    return (previous["close"] > resistance) and (current["low"] <= resistance * 1.005) and (current["close"] > resistance)

def calculate_stop_loss(candles, current_price, resistance):
    recent = candles[-8:-1]
    if not recent: return None
    recent_low = min(c["low"] for c in recent)
    stop = min(recent_low * 0.995, resistance * 0.995) if resistance else recent_low * 0.995
    return stop if stop < current_price else None

def calculate_target(current_price, stop_loss):
    risk = current_price - stop_loss
    if risk <= 0: return None, 0
    return current_price + risk * 2.5, 2.5

def analyze_symbol(symbol, btc_regime):
    try:
        funding = get_funding_rate(symbol)
        if abs(funding) > MAX_ALLOWABLE_FR: return None

        oi, oi_change = get_oi_change(symbol)
        candles_4h = get_candles(symbol, "4H", 70)
        candles_1d = get_candles(symbol, "1D", 50)
        if not candles_4h or not candles_1d: return None

        c4 = [c for c in candles_4h if c["confirm"] == "1"]
        c1 = [c for c in candles_1d if c["confirm"] == "1"]
        if len(c4) < 35 or len(c1) < 25: return None

        price = c4[-1]["close"]
        resistance_4h = get_resistance(c4, 30)
        resistance_1d = get_resistance(c1, 20)
        if not resistance_4h: return None

        hl_4h = higher_lows(c4)
        hl_1d = higher_lows(c1)
        engulf_4h = bullish_engulfing(c4)
        engulf_1d = bullish_engulfing(c1)
        vol_ratio_val = volume_ratio(c4)
        status_4h = breakout_status(c4, resistance_4h)
        status_1d = breakout_status(c1, resistance_1d)
        retest = retest_confirmation(c4, resistance_4h)

        if status_4h in ["breakout", "weak_breakout"] and bad_breakout(c4[-1], resistance_4h):
            return None

        score = 0
        if hl_4h: score += 2
        if hl_1d: score += 1
        if engulf_4h: score += 2
        if engulf_1d: score += 2
        if status_4h == "breakout": score += 2
        elif status_4h == "weak_breakout": score += 1
        elif status_4h == "near": score += 1
        if status_1d == "breakout": score += 2
        elif status_1d == "near": score += 1

        if vol_ratio_val >= VOL_VERY_STRONG: score += 2
        elif vol_ratio_val >= VOL_STRONG: score += 1
        elif vol_ratio_val >= VOL_NORMAL: score += 1

        if oi_change is not None:
            if oi_change >= OI_VERY_STRONG: score += 2
            elif oi_change >= OI_STRONG: score += 1

        if btc_regime == "bull": score += 1
        elif btc_regime == "bear": score -= 1

        if -0.01 <= funding * 100 <= 0.05: score += 1
        if retest: score += 2

        is_a_grade_retest = retest
        is_a_grade_breakout = (status_4h == "breakout") and (vol_ratio_val >= 1.2)

        if not (is_a_grade_retest or is_a_grade_breakout):
            return None
        if score < 7:
            return None

        stop = calculate_stop_loss(c4, price, resistance_4h)
        if stop is None: return None
        target, rr = calculate_target(price, stop)
        if target is None: return None

        if is_a_grade_retest:
            signal_type = "🔥 A級｜回踩確認"
        else:
            signal_type = "🚀 A級｜突破確認"

        oi_text = "OI N/A" if oi_change is None else f"OI {'+' if oi_change >= 0 else ''}{oi_change:.1f}%"
        funding_pct = funding * 100
        funding_text = f"FR {funding_pct:.3f}% ⚠️" if funding_pct > 0.05 else (f"FR {funding_pct:.3f}% 🔥" if funding_pct < -0.01 else f"FR {funding_pct:.3f}%")

        pattern_list = []
        if hl_4h: pattern_list.append("4H底底高")
        if hl_1d: pattern_list.append("1D底底高")
        if engulf_4h: pattern_list.append("4H陽包陰")
        if engulf_1d: pattern_list.append("1D陽包陰")
        if retest: pattern_list.append("回踩不破")

        return {
            "symbol": symbol,
            "price": price,
            "stop": stop,
            "target": target,
            "rr": rr,
            "score": score,
            "signal_type": signal_type,
            "vol_ratio": vol_ratio_val,
            "oi_text": oi_text,
            "funding_text": funding_text,
            "pattern": "＋".join(pattern_list[:3]) if pattern_list else "技術突破",
            "signal_id": f"{symbol}_{c4[-1]['timestamp']}_{signal_type}"
        }
    except Exception:
        return None

def build_message(r):
    return (
        f"{r['signal_type']}｜`{r['symbol']}`\n"
        f"💰 進場價: `{format_price(r['price'])}`\n"
        f"🎯 目標價: `{format_price(r['target'])}`\n"
        f"🛑 止損價: `{format_price(r['stop'])}`\n"
        f"📊 風險報酬比 (RR): `1:{r['rr']:.1f}`\n"
        f"📈 成交量: `{r['vol_ratio']:.1f}x`｜`{r['oi_text']}`\n"
        f"🟢 資金費率: {r['funding_text']}\n"
        f"🕯️ 型態: {r['pattern']}\n"
        f"⭐ 評分: `{r['score']}/10`"
    )

def scan_market():
    now = datetime.now(timezone(timedelta(hours=8)))
    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ⚡ 肥嘟嘟左衛門高速掃描中...")

    btc_regime = check_btc_market_regime()
    symbols = get_top_hot_symbols()
    if not symbols: return

    found, sent = 0, 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(analyze_symbol, s, btc_regime): s for s in symbols}
        for future in as_completed(futures):
            try:
                result = future.result()
                if not result: continue
                found += 1
                if result["signal_id"] in notified_signals: continue
                if send_telegram_message(build_message(result)):
                    notified_signals.add(result["signal_id"])
                    sent += 1
                    print(f"🚨 推播 A 級訊號: {result['symbol']} ({result['score']}/10)")
            except Exception:
                pass

    if found == 0:
        send_telegram_message("🎲 肥嘟嘟左衛門回報：現在沒幣幹，直接去打 21 點！")

    print(f"✅ 掃描完成 | A 級合規機會 {found} | 已發送 {sent}")

if __name__ == "__main__":
    print("🤖 肥嘟嘟左衛門已啟動（1500萬精準USDT門檻＋網頁伺服器防線）")
    send_telegram_message("🤖 肥嘟嘟左衛門已上線！精準剔除低量山寨，準備幫老闆盯盤。")
    
    while True:
        try:
            scan_market()
        except Exception as e:
            print(f"❌ 錯誤: {e}")
            
        now = datetime.now(timezone(timedelta(hours=8)))
        seconds_to_next_hour = (60 - now.minute) * 60 - now.second
        print(f"💤 等待至下一個整點，大約 {seconds_to_next_hour // 60} 分鐘後再次掃描...")
        time.sleep(seconds_to_next_hour)
