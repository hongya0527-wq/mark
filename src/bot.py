import sys
import os

# 💡 強制讓 Python 輸出即時顯示在 Render Logs 上，不會被卡住
sys.stdout.reconfigure(line_buffering=True)

from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque

# =========================================================
# Render 專用：假網頁伺服器（解決免費 Web Service 的 Port 逾時問題）
# =========================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Feidudu Zuoweimon Bybit Bot is running!")

    def log_message(self, format, *args):
        return


def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


server_thread = threading.Thread(
    target=run_server,
    daemon=True
)
server_thread.start()


# =========================================================
# Telegram
# =========================================================

TELEGRAM_BOT_TOKEN = "8834096490:AAGvSHCC8TNC_q4CXJZ4-fK-zyXlmHCPoKA"
TELEGRAM_CHAT_ID = "6273931436"


# =========================================================
# Bybit V5 API
# =========================================================

BYBIT_BASE_URL = "https://api.bybit.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


# =========================================================
# 篩選設定
# =========================================================

MIN_24H_VOLUME = 15_000_000

MAX_ALLOWABLE_FR = 0.0015

NEAR_RESISTANCE_PCT = 0.015

BREAKOUT_MIN_PCT = 0.003


# =========================================================
# 成交量
# =========================================================

VOL_NORMAL = 1.10
VOL_STRONG = 1.30
VOL_VERY_STRONG = 1.60


# =========================================================
# OI
# =========================================================

OI_STRONG = 2.0
OI_NORMAL = 0.5

OI_HISTORY_HOURS = 12


# =========================================================
# 1H 動能
# =========================================================

MOMENTUM_STRONG_PCT = 1.0
MOMENTUM_MEDIUM_PCT = 0.35

MOMENTUM_VOL_STRONG = 1.30
MOMENTUM_VOL_MEDIUM = 1.05


# =========================================================
# 風控
# =========================================================

TARGET_RR = 2.5

MAX_STOP_DISTANCE_PCT = 3.0

MIN_STOP_DISTANCE_PCT = 0.30


# =========================================================
# Session
# =========================================================

session = requests.Session()
session.headers.update(HEADERS)

adapter = HTTPAdapter(
    pool_connections=40,
    pool_maxsize=40
)

session.mount("https://", adapter)
session.mount("http://", adapter)


# =========================================================
# 全域狀態
# =========================================================

notified_signals = set()

oi_history = defaultdict(
    lambda: deque(maxlen=OI_HISTORY_HOURS + 5)
)

tracked_symbols = {}

momentum_states = {}


# =========================================================
# Bybit GET (V5 實作)
# =========================================================

def bybit_get(endpoint, params=None, retries=2):

    url = f"{BYBIT_BASE_URL}{endpoint}"

    for attempt in range(retries):

        try:

            response = session.get(
                url,
                params=params,
                timeout=7
            )

            if response.status_code == 429:
                time.sleep(1 + attempt)
                continue

            response.raise_for_status()

            data = response.json()

            if data.get("retCode") != 0:
                return []

            return data.get("result", {}).get("list", [])

        except Exception:

            if attempt < retries - 1:
                time.sleep(0.5)

    return []


# =========================================================
# Telegram
# =========================================================

def send_telegram_message(message):

    if not TELEGRAM_BOT_TOKEN:
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:

        response = session.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=7
        )

        return response.status_code == 200

    except Exception:

        return False


# =========================================================
# 價格格式
# =========================================================

def format_price(price):

    if price >= 100:
        return f"{price:,.2f}"

    if price >= 1:
        return f"{price:,.4f}"

    if price >= 0.01:
        return f"{price:,.5f}"

    if price >= 0.0001:
        return f"{price:,.6f}"

    return f"{price:,.8f}"


# =========================================================
# K線 (Bybit V5 轉換)
# =========================================================

def get_candles(symbol, bar, limit=100):
    interval_map = {
        "1H": "60",
        "4H": "240",
        "1D": "D"
    }
    bybit_interval = interval_map.get(bar, "60")

    data = bybit_get(
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": symbol,
            "interval": bybit_interval,
            "limit": limit
        }
    )

    if not data:
        return None

    candles = []

    for item in reversed(data):

        try:

            if len(item) < 7:
                continue

            candles.append({
                "timestamp": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "confirm": "1"
            })

        except Exception:
            continue

    return candles


# =========================================================
# Funding Rate (Bybit V5)
# =========================================================

def get_funding_rate(symbol):

    data = bybit_get(
        "/v5/market/funding/history",
        {
            "category": "linear",
            "symbol": symbol,
            "limit": 1
        }
    )

    if not data:
        return 0.0

    try:
        return float(
            data[0].get(
                "fundingRate",
                0
            )
        )

    except Exception:
        return 0.0


# =========================================================
# 一次取得全市場 OI (Bybit V5)
# =========================================================

def get_all_open_interest():

    data = bybit_get(
        "/v5/market/tickers",
        {
            "category": "linear"
        }
    )

    result = {}

    for item in data:

        try:

            symbol = item.get("symbol")

            if not symbol:
                continue

            if not symbol.endswith("USDT"):
                continue

            oi = float(
                item.get(
                    "openInterest",
                    0
                )
            )

            if oi > 0:
                result[symbol] = oi

        except Exception:
            continue

    return result


# =========================================================
# 更新 OI 歷史
# =========================================================

def update_oi_history():

    all_oi = get_all_open_interest()

    if not all_oi:
        return

    now_ts = int(
        time.time()
    )

    for symbol, oi in all_oi.items():

        oi_history[symbol].append({
            "time": now_ts,
            "oi": oi
        })


# =========================================================
# 取得 4H OI 變化
# =========================================================

def get_4h_oi_change(symbol):

    history = oi_history.get(symbol)

    if not history or len(history) < 2:
        return None

    now_ts = int(
        time.time()
    )

    target_time = (
        now_ts
        - 4 * 60 * 60
    )

    best = None
    best_distance = None

    for item in history:

        distance = abs(
            item["time"]
            - target_time
        )

        if distance <= 90 * 60:

            if (
                best_distance is None
                or
                distance < best_distance
            ):
                best = item
                best_distance = distance

    if best is None:
        return None

    old_oi = best["oi"]
    latest_oi = history[-1]["oi"]

    if old_oi <= 0:
        return None

    return (
        (latest_oi - old_oi)
        / old_oi
    ) * 100


# =========================================================
# OI 是否適合
# =========================================================

def oi_status(oi_change):

    if oi_change is None:
        return "⚪未知"

    if oi_change >= OI_STRONG:
        return "🟢適合"

    if oi_change >= OI_NORMAL:
        return "🟡普通"

    return "🔴不佳"


# =========================================================
# BTC 市場環境
# =========================================================

def check_btc_market_regime():

    candles = get_candles(
        "BTCUSDT",
        "4H",
        50
    )

    if not candles or len(candles) < 25:
        return "neutral"

    closes = [
        c["close"]
        for c in candles
    ]

    ema = sum(
        closes[:20]
    ) / 20

    multiplier = 2 / 21

    for price in closes[20:]:

        ema = (
            price - ema
        ) * multiplier + ema

    current = closes[-1]

    if current > ema * 1.01:
        return "bull"

    if current < ema * 0.99:
        return "bear"

    return "neutral"


# =========================================================
# 取得所有 USDT 永續合約清單 (改用 instruments-info 避開 403)
# =========================================================

def get_top_hot_symbols():
    url = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
    params = {"category": "linear"}
    
    custom_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    try:
        response = session.get(url, params=params, headers=custom_headers, timeout=10)
        if response.status_code != 200:
            print(f"⚠️ Bybit Instruments API 回傳狀態碼錯誤: {response.status_code}")
            return []
        
        data = response.json()
        if data.get("retCode") != 0:
            print(f"⚠️ Bybit API 錯誤代碼: {data.get('retMsg')}")
            return []
            
        instruments = data.get("result", {}).get("list", [])
    except Exception as e:
        print(f"⚠️ 取得 Instruments 發生例外錯誤: {e}")
        return []

    if not instruments:
        return []

    exclude = ["SPX", "NDX", "QQQ", "AAPL", "TSLA", "NVDA", "GOLD", "OIL"]
    symbols = []

    for item in instruments:
        symbol = item.get("symbol")
        if not symbol or not symbol.endswith("USDT"):
            continue
        if any(x in symbol for x in exclude):
            continue
        if item.get("status") != "Trading":
            continue
            
        symbols.append(symbol)

    print(f"✅ 成功載入合約清單，共 {len(symbols)} 檔幣種，開始深度掃描...")
    return symbols


# =========================================================
# 陽包陰
# =========================================================

def bullish_engulfing(
    candles,
    lookback=4
):

    if len(candles) < 2:
        return False

    start = max(
        1,
        len(candles) - lookback
    )

    for i in range(
        start,
        len(candles)
    ):

        prev = candles[i - 1]
        curr = candles[i]

        if (
            prev["close"] < prev["open"]
            and
            curr["close"] > curr["open"]
            and
            curr["open"] <= prev["close"]
            and
            curr["close"] >= prev["open"]
        ):
            return True

    return False


# =========================================================
# 底底高
# =========================================================

def higher_lows(candles):

    if len(candles) < 8:
        return False

    lows = [
        c["low"]
        for c in candles[-12:]
    ]

    swing_lows = []

    for i in range(
        1,
        len(lows) - 1
    ):

        if (
            lows[i] < lows[i - 1]
            and
            lows[i] <= lows[i + 1]
        ):
            swing_lows.append(
                lows[i]
            )

    if len(swing_lows) < 2:
        return False

    return (
        swing_lows[-1]
        >
        swing_lows[-2]
    )


# =========================================================
# 成交量倍率
# =========================================================

def volume_ratio(candles):

    if len(candles) < 7:
        return 0

    current = candles[-1][
        "volume"
    ]

    previous = [
        c["volume"]
        for c in candles[-6:-1]
    ]

    avg = (
        sum(previous)
        / len(previous)
    )

    return (
        current / avg
        if avg > 0
        else 0
    )


# =========================================================
# 壓力
# =========================================================

def get_resistance(
    candles,
    lookback
):

    if len(candles) < (
        lookback + 3
    ):
        return None

    data = candles[
        -lookback - 2:-2
    ]

    if not data:
        return None

    return max(
        c["high"]
        for c in data
    )


# =========================================================
# 突破狀態
# =========================================================

def breakout_status(
    candles,
    resistance
):

    if not resistance:
        return "none"

    close = candles[-1][
        "close"
    ]

    if close > resistance:

        pct = (
            close - resistance
        ) / resistance

        if pct >= BREAKOUT_MIN_PCT:
            return "breakout"

        return "weak_breakout"

    distance = (
        resistance - close
    ) / resistance

    if distance <= NEAR_RESISTANCE_PCT:
        return "near"

    return "none"


# =========================================================
# 假突破
# =========================================================

def bad_breakout(
    candle,
    resistance
):

    if resistance <= 0:
        return True

    body = abs(
        candle["close"]
        - candle["open"]
    )

    upper_wick = (
        candle["high"]
        -
        max(
            candle["open"],
            candle["close"]
        )
    )

    full_range = (
        candle["high"]
        - candle["low"]
    )

    if body <= 0:
        return True

    if upper_wick > body * 1.5:
        return True

    if full_range > 0:

        body_ratio = (
            body / full_range
        )

        if body_ratio < 0.35:
            return True

    if (
        candle["high"] > resistance
        and
        candle["close"] < resistance
    ):
        return True

    return False


# =========================================================
# 回踩確認
# =========================================================

def retest_confirmation(
    candles,
    resistance
):

    if len(candles) < 3:
        return False

    current = candles[-1]
    previous = candles[-2]

    return (
        previous["close"] > resistance
        and
        current["low"]
        <= resistance * 1.005
        and
        current["close"] > resistance
    )


# =========================================================
# 1H 動能
# =========================================================

def calculate_momentum(
    candles
):

    if not candles or len(candles) < 7:
        return None

    current = candles[-1]
    previous = candles[-2]

    price_change = (
        (
            current["close"]
            - previous["close"]
        )
        /
        previous["close"]
    ) * 100

    vol_ratio = volume_ratio(
        candles
    )

    score = 0

    if price_change >= MOMENTUM_STRONG_PCT:
        score += 2

    elif price_change >= MOMENTUM_MEDIUM_PCT:
        score += 1

    if vol_ratio >= MOMENTUM_VOL_STRONG:
        score += 2

    elif vol_ratio >= MOMENTUM_VOL_MEDIUM:
        score += 1

    if score >= 3:
        state = "強"

    elif score >= 1:
        state = "中"

    else:
        state = "弱"

    return {
        "state": state,
        "price_change": price_change,
        "vol_ratio": vol_ratio
    }


# =========================================================
# 動能變化訊息
# =========================================================

def send_momentum_change(
    symbol,
    old_state,
    new_state,
    momentum
):

    if new_state == "強":

        icon = "🔥"
        title = "動能加速"

    else:

        icon = "⚠️"
        title = "動能轉弱"

    oi_change = get_4h_oi_change(
        symbol
    )

    if oi_change is None:

        oi_text = "OI N/A"

    else:

        oi_text = (
            f"OI "
            f"{'+' if oi_change >= 0 else ''}"
            f"{oi_change:.1f}% "
            f"{oi_status(oi_change)}"
        )

    message = (
        f"{icon} *{title}*｜`{symbol}`\n\n"
        f"⚡ 動能：`{old_state} → {new_state}`\n"
        f"📈 量：`{momentum['vol_ratio']:.1f}x`"
        f"｜{oi_text}"
    )

    send_telegram_message(
        message
    )


# =========================================================
# 更新已追蹤幣的動能
# =========================================================

def update_momentum_tracking():

    if not tracked_symbols:
        return

    remove_list = []

    for symbol in list(tracked_symbols.keys()):

        try:

            candles_1h = get_candles(
                symbol,
                "1H",
                30
            )

            if not candles_1h:
                continue

            momentum = calculate_momentum(
                candles_1h
            )

            if not momentum:
                continue

            new_state = momentum["state"]
            old_state = momentum_states.get(symbol)

            if old_state is None:
                momentum_states[symbol] = new_state
                continue

            if new_state == "弱":
                remove_list.append(symbol)
                continue

            if new_state == old_state:
                continue

            if old_state in ["中", "弱"] and new_state == "強":
                send_momentum_change(
                    symbol,
                    old_state,
                    new_state,
                    momentum
                )

            momentum_states[symbol] = new_state

        except Exception:
            continue

    for symbol in remove_list:
        tracked_symbols.pop(symbol, None)
        momentum_states.pop(symbol, None)
        print(f"🧹 幣種 {symbol} 動能轉弱，已從追蹤清單中移除。")


# =========================================================
# 止損
# =========================================================

def calculate_stop_loss(
    candles,
    current_price,
    resistance,
    is_retest
):

    if not candles:
        return None

    if is_retest:

        recent = candles[-5:-1]

        if recent:

            recent_low = min(
                c["low"]
                for c in recent
            )

            stop = (
                recent_low * 0.997
            )

        else:

            stop = (
                resistance * 0.992
            )

    else:

        stop = (
            resistance * 0.992
        )

    if stop >= current_price:
        return None

    distance_pct = (
        (
            current_price
            - stop
        )
        /
        current_price
    ) * 100

    if (
        distance_pct
        > MAX_STOP_DISTANCE_PCT
    ):
        return None

    if (
        distance_pct
        < MIN_STOP_DISTANCE_PCT
    ):
        return None

    return stop


# =========================================================
# 目標
# =========================================================

def calculate_target(
    current_price,
    stop_loss
):

    risk = (
        current_price
        - stop_loss
    )

    if risk <= 0:
        return None

    return (
        current_price
        + risk * TARGET_RR
    )


# =========================================================
# 分析單一幣
# =========================================================

def analyze_symbol(
    symbol,
    btc_regime
):

    try:

        funding = get_funding_rate(
            symbol
        )

        if (
            abs(funding)
            > MAX_ALLOWABLE_FR
        ):
            return None

        candles_4h = get_candles(
            symbol,
            "4H",
            80
        )

        candles_1d = get_candles(
            symbol,
            "1D",
            60
        )

        candles_1h = get_candles(
            symbol,
            "1H",
            40
        )

        if (
            not candles_4h
            or
            not candles_1d
            or
            not candles_1h
        ):
            return None

        if (
            len(candles_4h) < 35
            or
            len(candles_1d) < 25
            or
            len(candles_1h) < 15
        ):
            return None

        price = candles_4h[-1]["close"]

        resistance_4h = get_resistance(
            candles_4h,
            30
        )

        resistance_1d = get_resistance(
            candles_1d,
            20
        )

        if not resistance_4h:
            return None

        hl_4h = higher_lows(candles_4h)
        hl_1d = higher_lows(candles_1d)

        engulf_1h = bullish_engulfing(candles_1h, 4)
        engulf_4h = bullish_engulfing(candles_4h, 4)
        engulf_1d = bullish_engulfing(candles_1d, 4)

        vol_ratio_1h = volume_ratio(
            candles_1h
        )

        oi_change = get_4h_oi_change(
            symbol
        )

        status_4h = breakout_status(
            candles_4h,
            resistance_4h
        )

        retest = retest_confirmation(
            candles_4h,
            resistance_4h
        )

        if (
            status_4h
            in [
                "breakout",
                "weak_breakout"
            ]
            and
            bad_breakout(
                candles_4h[-1],
                resistance_4h
            )
        ):
            return None

        momentum = calculate_momentum(
            candles_1h
        )

        if not momentum:
            return None

        momentum_state = momentum[
            "state"
        ]

        score = 0

        if hl_4h:
            score += 2

        if hl_1d:
            score += 1

        if engulf_1h:
            score += 2

        if engulf_4h:
            score += 1

        if engulf_1d:
            score += 2

        if status_4h in ["breakout", "near"]:
            score += 1

        if vol_ratio_1h >= VOL_STRONG:
            score += 2

        elif vol_ratio_1h >= VOL_NORMAL:
            score += 1

        if oi_change is not None and oi_change >= OI_NORMAL:
            score += 1

        if momentum_state == "強":
            score += 2

        elif momentum_state == "中":
            score += 1

        if btc_regime == "bull":
            score += 1

        elif btc_regime == "bear":
            score -= 1

        if retest:
            score += 2

        score = max(
            1,
            min(score, 10)
        )

        is_valid_setup = (
            engulf_1h
            or
            engulf_4h
            or
            engulf_1d
            or
            retest
            or
            (
                status_4h == "breakout"
                and
                vol_ratio_1h >= 1.20
            )
        ) and momentum_state != "弱" and score >= 7

        if not is_valid_setup:
            return None

        stop = calculate_stop_loss(
            candles_4h,
            price,
            resistance_4h,
            retest
        )

        if stop is None:
            return None

        target = calculate_target(
            price,
            stop
        )

        if target is None:
            return None

        if engulf_1d:
            signal_type = "🔥 A級｜日線級別陽包陰突擊"
        elif engulf_1h:
            signal_type = "🔥 A級｜1H陽包陰突擊"
        else:
            signal_type = "🚀 A級｜4H趨勢突破"

        if oi_change is None:

            oi_text = "OI N/A"

        else:

            oi_text = (
                f"OI "
                f"{'+' if oi_change >= 0 else ''}"
                f"{oi_change:.1f}% "
                f"{oi_status(oi_change)}"
            )

        pattern_list = []

        if engulf_1d:
            pattern_list.append("日線陽包陰")
        if engulf_1h:
            pattern_list.append("1H陽包陰")
        if engulf_4h:
            pattern_list.append("4H陽包陰")
        if hl_4h:
            pattern_list.append("4H底底高")
        if retest:
            pattern_list.append("回踩不破")

        pattern = (
            "＋".join(
                pattern_list[:3]
            )
            if pattern_list
            else
            "技術突破"
        )

        signal_id = (
            f"{symbol}_"
            f"{candles_4h[-1]['timestamp']}_"
            f"{signal_type}"
        )

        return {

            "symbol": symbol,

            "price": price,

            "stop": stop,

            "target": target,

            "score": score,

            "signal_type": signal_type,

            "vol_ratio": vol_ratio_1h,

            "oi_change": oi_change,

            "oi_text": oi_text,

            "momentum": momentum_state,

            "pattern": pattern,

            "signal_id": signal_id
        }

    except Exception:

        return None


# =========================================================
# A級訊息
# =========================================================

def build_message(r):

    return (
        f"{r['signal_type']}｜`{r['symbol']}`\n\n"

        f"💰 進場：`{format_price(r['price'])}`\n"
        f"🎯 目標：`{format_price(r['target'])}`\n"
        f"🛑 止損：`{format_price(r['stop'])}`\n\n"

        f"⚡ 動能：`{r['momentum']}`\n"

        f"📈 1H量：`{r['vol_ratio']:.1f}x`"
        f"｜{r['oi_text']}\n"

        f"🕯 {r['pattern']}\n"

        f"⭐ 評分：`{r['score']}/10`"
    )


# =========================================================
# 加入動能追蹤
# =========================================================

def add_to_tracking(result):

    symbol = result[
        "symbol"
    ]

    tracked_symbols[
        symbol
    ] = {
        "added_time": int(
            time.time()
        ),
        "signal_id": result[
            "signal_id"
        ]
    }

    momentum_states[
        symbol
    ] = result[
        "momentum"
    ]


# =========================================================
# 清理追蹤
# =========================================================

def cleanup_tracking():

    now_ts = int(
        time.time()
    )

    remove_list = []

    for symbol, data in (
        tracked_symbols.items()
    ):

        age = (
            now_ts
            - data["added_time"]
        )

        if age > (
            48 * 60 * 60
        ):

            remove_list.append(
                symbol
            )

    for symbol in remove_list:

        tracked_symbols.pop(
            symbol,
            None
        )

        momentum_states.pop(
            symbol,
            None
        )


# =========================================================
# 掃描市場
# =========================================================

def scan_market():

    now = datetime.now(
        timezone(
            timedelta(hours=8)
        )
    )

    print(
        f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"⚡ 肥嘟嘟左衛門 Bybit 高速掃描中..."
    )

    print(
        "📊 更新全市場 OI..."
    )

    update_oi_history()

    btc_regime = (
        check_btc_market_regime()
    )

    print(
        f"₿ BTC環境：{btc_regime}"
    )

    symbols = (
        get_top_hot_symbols()
    )

    if not symbols:
        print("⚠️ 未能取得 Bybit 合約清單。")
        return

    found = 0
    sent = 0

    with ThreadPoolExecutor(
        max_workers=20
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                symbol,
                btc_regime
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(
            futures
        ):

            try:

                result = (
                    future.result()
                )

                if not result:
                    continue

                found += 1

                if (
                    result[
                        "signal_id"
                    ]
                    in notified_signals
                ):
                    continue

                if send_telegram_message(
                    build_message(result)
                ):

                    notified_signals.add(
                        result[
                            "signal_id"
                        ]
                    )

                    sent += 1

                    add_to_tracking(
                        result
                    )

                    print(
                        f"🚨 推播 A級："
                        f"{result['symbol']} "
                        f"({result['score']}/10)"
                    )

            except Exception as e:

                print(
                    f"分析錯誤：{e}"
                )

    print(
        f"⚡ 持續追蹤："
        f"{len(tracked_symbols)} 顆"
    )

    update_momentum_tracking()

    cleanup_tracking()

    if found == 0:

        send_telegram_message(
            "🎲 肥嘟嘟左衛門回報："
            "現在沒幣幹，直接去打 21 點！"
        )

    print(
        f"✅ 掃描完成 | "
        f"A級合規機會 {found} | "
        f"已發送 {sent} | "
        f"追蹤 {len(tracked_symbols)}"
    )


# =========================================================
# 啟動
# =========================================================

if __name__ == "__main__":

    print(
        "🤖 肥嘟嘟左衛門 (Bybit版) 已啟動"
    )

    send_telegram_message(
        "🤖 肥嘟嘟左衛門 (全市場合約 + 多周期陽包陰) 已上線"
    )

    while True:

        try:

            scan_market()

        except Exception as e:

            print(
                f"❌ 錯誤：{e}"
            )

        now = datetime.now(
            timezone(
                timedelta(hours=8)
            )
        )

        seconds_to_next_hour = (
            (60 - now.minute) * 60
            - now.second
        )

        print(
            f"💤 等待至下一個整點，"
            f"大約 "
            f"{seconds_to_next_hour // 60} "
            f"分鐘後再次掃描..."
        )

        time.sleep(
            seconds_to_next_hour
        )
