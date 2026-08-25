import os
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
        self.wfile.write(b"Feidudu Zuoweimon Bot is running!")

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
# OKX
# =========================================================

OKX_BASE_URL = "https://www.okx.com"
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
# OKX GET
# =========================================================

def okx_get(endpoint, params=None, retries=2):

    url = f"{OKX_BASE_URL}{endpoint}"

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

            if data.get("code") != "0":
                return []

            return data.get("data", [])

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
# K線
# =========================================================

def get_candles(symbol, bar, limit=100):

    data = okx_get(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        }
    )

    if not data:
        return None

    candles = []

    for item in reversed(data):

        try:

            if len(item) < 9:
                continue

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


# =========================================================
# Funding Rate
# =========================================================

def get_funding_rate(symbol):

    data = okx_get(
        "/api/v5/public/funding-rate",
        {"instId": symbol}
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
# 一次取得全市場 OI
# =========================================================

def get_all_open_interest():

    data = okx_get(
        "/api/v5/public/open-interest",
        {
            "instType": "SWAP"
        }
    )

    result = {}

    for item in data:

        try:

            symbol = item.get("instId")

            if not symbol:
                continue

            if not symbol.endswith("-USDT-SWAP"):
                continue

            oi = float(
                item.get(
                    "oi",
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
        "BTC-USDT-SWAP",
        "4H",
        50
    )

    if not candles:
        return "neutral"

    closed = [
        c
        for c in candles
        if c["confirm"] == "1"
    ]

    if len(closed) < 25:
        return "neutral"

    closes = [
        c["close"]
        for c in closed
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
# 熱門幣
# =========================================================

def get_top_hot_symbols():

    tickers = okx_get(
        "/api/v5/market/tickers",
        {
            "instType": "SWAP"
        }
    )

    if not tickers:
        return []

    exclude = [
        "SPX",
        "NDX",
        "QQQ",
        "AAPL",
        "TSLA",
        "NVDA",
        "GOLD",
        "OIL"
    ]

    volume_list = []

    for item in tickers:

        symbol = item.get(
            "instId"
        )

        if not symbol:
            continue

        if not symbol.endswith(
            "-USDT-SWAP"
        ):
            continue

        if any(
            x in symbol
            for x in exclude
        ):
            continue

        try:

            vol_ccy = float(
                item.get(
                    "volCcy24h",
                    0
                )
            )

            last_price = float(
                item.get(
                    "last",
                    0
                )
            )

            usdt_volume = (
                vol_ccy
                * last_price
            )

            if usdt_volume < MIN_24H_VOLUME:
                continue

            volume_list.append(
                (
                    symbol,
                    usdt_volume
                )
            )

        except Exception:
            continue

    top_volume = [
        x[0]
        for x in sorted(
            volume_list,
            key=lambda x: x[1],
            reverse=True
        )[:60]
    ]

    return top_volume


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

    if not candles:
        return None

    closed = [
        c
        for c in candles
        if c["confirm"] == "1"
    ]

    if len(closed) < 7:
        return None

    current = closed[-1]
    previous = closed[-2]

    price_change = (
        (
            current["close"]
            - previous["close"]
        )
        /
        previous["close"]
    ) * 100

    vol_ratio = volume_ratio(
        closed
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
# 更新已追蹤幣的動能（動能轉弱自動解除追蹤）
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

            # 🛑 如果動能變爛（變成「弱」），直接加入移除清單，不再追蹤
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

    # 執行清理：把動能變爛的幣從追蹤字典中清除
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
            30
        )

        if (
            not candles_4h
            or
            not candles_1d
            or
            not candles_1h
        ):
            return None

        c4 = [
            c
            for c in candles_4h
            if c["confirm"] == "1"
        ]

        c1 = [
            c
            for c in candles_1d
            if c["confirm"] == "1"
        ]

        if (
            len(c4) < 35
            or
            len(c1) < 25
        ):
            return None

        price = c4[-1]["close"]

        resistance_4h = get_resistance(
            c4,
            30
        )

        resistance_1d = get_resistance(
            c1,
            20
        )

        if not resistance_4h:
            return None

        hl_4h = higher_lows(c4)
        hl_1d = higher_lows(c1)

        engulf_4h = bullish_engulfing(
            c4,
            4
        )

        engulf_1d = bullish_engulfing(
            c1,
            4
        )

        vol_ratio_val = volume_ratio(
            c4
        )

        oi_change = get_4h_oi_change(
            symbol
        )

        status_4h = breakout_status(
            c4,
            resistance_4h
        )

        status_1d = breakout_status(
            c1,
            resistance_1d
        )

        retest = retest_confirmation(
            c4,
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
                c4[-1],
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

        if engulf_4h:
            score += 1

        if engulf_1d:
            score += 1

        if status_4h == "breakout":
            score += 2

        elif status_4h == "weak_breakout":
            score += 1

        elif status_4h == "near":
            score += 1

        if status_1d == "breakout":
            score += 1

        elif status_1d == "near":
            score += 1

        if (
            vol_ratio_val
            >= VOL_VERY_STRONG
        ):
            score += 2

        elif (
            vol_ratio_val
            >= VOL_STRONG
        ):
            score += 1

        elif (
            vol_ratio_val
            >= VOL_NORMAL
        ):
            score += 1

        if oi_change is not None:

            if (
                oi_change
                >= OI_STRONG
            ):
                score += 2

            elif (
                oi_change
                >= OI_NORMAL
            ):
                score += 1

        if momentum_state == "強":
            score += 1

        if btc_regime == "bull":
            score += 1

        elif btc_regime == "bear":
            score -= 1

        if (
            -0.01
            <= funding * 100
            <= 0.05
        ):
            score += 1

        if retest:
            score += 2

        score = max(
            1,
            min(score, 10)
        )

        is_a_grade_retest = (
            retest
            and
            momentum_state != "弱"
        )

        is_a_grade_breakout = (
            status_4h == "breakout"
            and
            vol_ratio_val >= 1.20
            and
            momentum_state != "弱"
            and
            (
                oi_change is None
                or
                oi_change >= 0
            )
        )

        if not (
            is_a_grade_retest
            or
            is_a_grade_breakout
        ):
            return None

        if score < 7:
            return None

        stop = calculate_stop_loss(
            c4,
            price,
            resistance_4h,
            is_a_grade_retest
        )

        if stop is None:
            return None

        target = calculate_target(
            price,
            stop
        )

        if target is None:
            return None

        if is_a_grade_retest:

            signal_type = (
                "🔥 A級｜回踩確認"
            )

        else:

            signal_type = (
                "🚀 A級｜突破確認"
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

        pattern_list = []

        if hl_4h:
            pattern_list.append(
                "4H底底高"
            )

        if hl_1d:
            pattern_list.append(
                "1D底底高"
            )

        if engulf_4h:
            pattern_list.append(
                "4H陽包陰"
            )

        if engulf_1d:
            pattern_list.append(
                "1D陽包陰"
            )

        if retest:
            pattern_list.append(
                "回踩不破"
            )

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
            f"{c4[-1]['timestamp']}_"
            f"{signal_type}"
        )

        return {

            "symbol": symbol,

            "price": price,

            "stop": stop,

            "target": target,

            "score": score,

            "signal_type": signal_type,

            "vol_ratio": vol_ratio_val,

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

        f"📈 量：`{r['vol_ratio']:.1f}x`"
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
# 清理追蹤 (最長保底 48 小時)
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
        f"⚡ 肥嘟嘟左衛門高速掃描中..."
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
        "🤖 肥嘟嘟左衛門已啟動"
    )

    send_telegram_message(
        "🤖 肥嘟嘟左衛門已上線"
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

