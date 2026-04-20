#!/usr/bin/env python3
"""
Trading Signal Bot - Crypto + Indian Stocks
Auto-trade on Binance, Telegram alerts, Web dashboard
"""

import hashlib
import hmac
import json
import os
import time
import math
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
ALPHA_VANTAGE_KEY   = os.getenv("ALPHA_VANTAGE_KEY", "demo")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
SCAN_INTERVAL_MIN   = int(os.getenv("SCAN_INTERVAL_MIN", "10"))
BINANCE_API_KEY     = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY  = os.getenv("BINANCE_SECRET_KEY", "")

# Auto-trade settings
AUTO_TRADE_ENABLED  = bool(BINANCE_API_KEY and BINANCE_SECRET_KEY)
TRADE_AMOUNT_USDT   = 13.0  # ~₹1000 in USDT
MIN_CONFIDENCE      = 70    # Only trade if confidence >= 70%
BINANCE_BASE_URL    = "https://api.binance.com"

# ─── BINANCE AUTO-TRADE ───────────────────────────────────────────────────────

def binance_signature(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

def binance_request(method: str, endpoint: str, params: dict = {}) -> dict:
    """Make signed request to Binance API"""
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = binance_signature(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = BINANCE_BASE_URL + endpoint
    try:
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=10)
        elif method == "POST":
            r = requests.post(url, params=params, headers=headers, timeout=10)
        elif method == "DELETE":
            r = requests.delete(url, params=params, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        logger.error(f"Binance API error: {e}")
        return {}

def get_binance_price(symbol: str) -> Optional[float]:
    """Get current price from Binance"""
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=5)
        return float(r.json()["price"])
    except:
        return None

def get_symbol_info(symbol: str) -> dict:
    """Get min qty and step size for symbol"""
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/exchangeInfo",
                         params={"symbol": symbol}, timeout=10)
        data = r.json()
        filters = data["symbols"][0]["filters"]
        lot = next(f for f in filters if f["filterType"] == "LOT_SIZE")
        notional = next((f for f in filters if f["filterType"] == "MIN_NOTIONAL"), {})
        return {
            "minQty":   float(lot["minQty"]),
            "stepSize": float(lot["stepSize"]),
            "minNotional": float(notional.get("minNotional", 5.0))
        }
    except:
        return {"minQty": 0.001, "stepSize": 0.001, "minNotional": 5.0}

def round_step(qty: float, step: float) -> float:
    """Round quantity to valid step size"""
    precision = len(str(step).rstrip('0').split('.')[-1])
    return round(round(qty / step) * step, precision)

def place_binance_order(symbol: str, side: str, usdt_amount: float, signal: dict) -> dict:
    """Place market order + OCO (stop loss + target) on Binance"""
    if not AUTO_TRADE_ENABLED:
        logger.info(f"  Auto-trade disabled — skipping order for {symbol}")
        return {}

    price = get_binance_price(symbol)
    if not price:
        logger.error(f"  Could not get price for {symbol}")
        return {}

    info     = get_symbol_info(symbol)
    qty      = round_step(usdt_amount / price, info["stepSize"])

    if qty < info["minQty"]:
        logger.warning(f"  Qty {qty} below minQty {info['minQty']} for {symbol} — skipping")
        return {}

    logger.info(f"  🔄 Placing {side} order: {qty} {symbol} @ ~{price}")

    # Place market order
    order_params = {
        "symbol":   symbol,
        "side":     side,
        "type":     "MARKET",
        "quantity": qty,
    }
    order = binance_request("POST", "/api/v3/order", order_params)

    if "orderId" not in order:
        logger.error(f"  ❌ Order failed: {order}")
        return {}

    logger.info(f"  ✅ Order placed! ID: {order['orderId']}")

    # Place stop loss order
    targets = signal.get("targets", {})
    sl_price = targets.get("stop_loss")
    t1_price = targets.get("target1")

    if sl_price and t1_price and side == "BUY":
        try:
            sl_side = "SELL"
            sl_params = {
                "symbol":      symbol,
                "side":        sl_side,
                "type":        "STOP_LOSS_LIMIT",
                "quantity":    qty,
                "price":       round(sl_price * 0.999, 2),
                "stopPrice":   round(sl_price, 2),
                "timeInForce": "GTC",
            }
            sl_order = binance_request("POST", "/api/v3/order", sl_params)
            logger.info(f"  🛡️ Stop Loss set @ {sl_price}")
        except Exception as e:
            logger.warning(f"  Stop loss order failed: {e}")

    return order

def notify_trade_telegram(symbol: str, side: str, qty: float,
                           price: float, signal: dict, order: dict):
    """Send trade execution notification to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    emoji = "🟢" if side == "BUY" else "🔴"
    t = signal.get("targets", {})
    msg = f"""
⚡ <b>AUTO-TRADE EXECUTED!</b> ⚡

{emoji} <b>{side}</b> {symbol}
<b>Quantity:</b> {qty}
<b>Price:</b> ~{price:,.4f} USDT
<b>Amount:</b> ~₹1000

<b>📊 Levels:</b>
• Stop Loss: {t.get('stop_loss', 'N/A')}
• Target 1: {t.get('target1', 'N/A')}
• Target 2: {t.get('target2', 'N/A')}

<b>Order ID:</b> {order.get('orderId', 'N/A')}
<i>⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>
<i>⚠️ Auto-trade executed by bot</i>
""".strip()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        logger.error(f"Trade notification error: {e}")

# Symbols to scan
CRYPTO_SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","MATICUSDT"
]

# Nifty 50 stocks (Yahoo Finance NSE symbols)
INDIAN_SYMBOLS = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "HINDUNILVR","ITC","SBIN","BHARTIARTL","KOTAKBANK",
    "LT","AXISBANK","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","WIPRO","NESTLEIND","TECHM",
    "BAJFINANCE","HCLTECH","POWERGRID","NTPC","ONGC",
    "TATAMOTORS","TATASTEEL","JSWSTEEL","BAJAJFINSV","ADANIENT"
]

# Options trading index symbols (for Nifty/BankNifty options signals)
OPTIONS_SYMBOLS = [
    {"name": "NIFTY",     "yahoo": "^NSEI"},
    {"name": "BANKNIFTY", "yahoo": "^NSEBANK"},
    {"name": "SENSEX",    "yahoo": "^BSESN"},
]

# ─── DATA LAYER ───────────────────────────────────────────────────────────────

def get_crypto_candles(symbol: str, interval="1h", limit=100) -> list[dict]:
    """Fetch OHLCV from Binance public API (no key needed)"""
    try:
        url = f"https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        candles = []
        for c in data:
            candles.append({
                "time": c[0] / 1000,
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
                "volume":float(c[5]),
            })
        return candles
    except Exception as e:
        logger.error(f"Binance error {symbol}: {e}")
        return []


def get_crypto_price_coingecko(symbol: str) -> Optional[float]:
    """Fallback price from CoinGecko"""
    mapping = {
        "BTCUSDT":"bitcoin","ETHUSDT":"ethereum","BNBUSDT":"binancecoin",
        "SOLUSDT":"solana","XRPUSDT":"ripple","ADAUSDT":"cardano",
        "DOGEUSDT":"dogecoin","AVAXUSDT":"avalanche-2","DOTUSDT":"polkadot",
        "MATICUSDT":"matic-network"
    }
    cg_id = mapping.get(symbol)
    if not cg_id:
        return None
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price"
        r = requests.get(url, params={"ids": cg_id, "vs_currencies": "usd"}, timeout=10)
        return r.json()[cg_id]["usd"]
    except:
        return None


def get_indian_stock_candles(symbol: str) -> list[dict]:
    """Fetch daily OHLCV from Alpha Vantage"""
    try:
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": f"{symbol}.BSE",
            "outputsize": "compact",
            "apikey": ALPHA_VANTAGE_KEY,
        }
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        ts = data.get("Time Series (Daily)", {})
        candles = []
        for date_str in sorted(ts.keys())[-100:]:
            d = ts[date_str]
            candles.append({
                "time": datetime.strptime(date_str, "%Y-%m-%d").timestamp(),
                "open":  float(d["1. open"]),
                "high":  float(d["2. high"]),
                "low":   float(d["3. low"]),
                "close": float(d["4. close"]),
                "volume":float(d["5. volume"]),
            })
        return candles
    except Exception as e:
        logger.error(f"AlphaVantage error {symbol}: {e}")
        return []


def get_indian_stock_yahoo(symbol: str) -> list[dict]:
    """Yahoo Finance fallback for Indian stocks"""
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="3mo", interval="1d")
        candles = []
        for ts, row in hist.iterrows():
            candles.append({
                "time": ts.timestamp(),
                "open":  float(row["Open"]),
                "high":  float(row["High"]),
                "low":   float(row["Low"]),
                "close": float(row["Close"]),
                "volume":float(row["Volume"]),
            })
        return candles
    except Exception as e:
        logger.error(f"Yahoo Finance error {symbol}: {e}")
        return []

# ─── TECHNICAL INDICATORS ─────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period=14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_ema(closes: list[float], period: int) -> list[float]:
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def calc_macd(closes: list[float]) -> dict:
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26:
        return {"macd": 0, "signal": 0, "histogram": 0}
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[-(min_len-i)] - ema26[-(min_len-i)] for i in range(min_len)]
    signal_ema = calc_ema(macd_line, 9)
    if not signal_ema:
        return {"macd": macd_line[-1], "signal": 0, "histogram": 0}
    hist = macd_line[-1] - signal_ema[-1]
    return {
        "macd":      round(macd_line[-1], 6),
        "signal":    round(signal_ema[-1], 6),
        "histogram": round(hist, 6),
    }


def calc_bollinger(closes: list[float], period=20, std_dev=2) -> dict:
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "width": 0}
    recent = closes[-period:]
    middle = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std = math.sqrt(variance)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    width = (upper - lower) / middle * 100
    return {
        "upper":  round(upper, 4),
        "middle": round(middle, 4),
        "lower":  round(lower, 4),
        "width":  round(width, 2),
    }


def calc_volume_signal(volumes: list[float], closes: list[float]) -> str:
    if len(volumes) < 20:
        return "NEUTRAL"
    avg_vol = sum(volumes[-20:]) / 20
    last_vol = volumes[-1]
    price_change = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    if last_vol > avg_vol * 1.5 and price_change > 0:
        return "BULLISH"
    elif last_vol > avg_vol * 1.5 and price_change < 0:
        return "BEARISH"
    return "NEUTRAL"


def compute_indicators(candles: list[dict]) -> Optional[dict]:
    if len(candles) < 30:
        return None
    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]

    rsi    = calc_rsi(closes)
    macd   = calc_macd(closes)
    bb     = calc_bollinger(closes)
    ema9   = calc_ema(closes, 9)
    ema21  = calc_ema(closes, 21)
    ema50  = calc_ema(closes, 50)
    vol_sig= calc_volume_signal(volumes, closes)

    price = closes[-1]
    return {
        "price":       price,
        "rsi":         rsi,
        "macd":        macd,
        "bollinger":   bb,
        "ema9":        round(ema9[-1],  4) if ema9  else None,
        "ema21":       round(ema21[-1], 4) if ema21 else None,
        "ema50":       round(ema50[-1], 4) if ema50 else None,
        "volume_signal": vol_sig,
    }

# ─── SIGNAL ENGINE ────────────────────────────────────────────────────────────

def score_indicators(ind: dict) -> dict:
    """Returns score -10 to +10 and individual signals"""
    score = 0
    signals = []

    # RSI
    rsi = ind["rsi"]
    if rsi < 30:
        score += 3; signals.append(f"RSI={rsi} (OVERSOLD 🟢)")
    elif rsi < 45:
        score += 1; signals.append(f"RSI={rsi} (Bullish zone)")
    elif rsi > 70:
        score -= 3; signals.append(f"RSI={rsi} (OVERBOUGHT 🔴)")
    elif rsi > 55:
        score -= 1; signals.append(f"RSI={rsi} (Bearish zone)")
    else:
        signals.append(f"RSI={rsi} (Neutral)")

    # MACD
    macd = ind["macd"]
    if macd["histogram"] > 0 and macd["macd"] > macd["signal"]:
        score += 2; signals.append("MACD Bullish crossover 🟢")
    elif macd["histogram"] < 0 and macd["macd"] < macd["signal"]:
        score -= 2; signals.append("MACD Bearish crossover 🔴")
    else:
        signals.append("MACD Neutral")

    # EMA
    price = ind["price"]
    if ind["ema9"] and ind["ema21"] and ind["ema50"]:
        if price > ind["ema9"] > ind["ema21"] > ind["ema50"]:
            score += 3; signals.append("Price > EMA9 > EMA21 > EMA50 🟢")
        elif price < ind["ema9"] < ind["ema21"] < ind["ema50"]:
            score -= 3; signals.append("Price < EMA9 < EMA21 < EMA50 🔴")
        elif ind["ema9"] > ind["ema21"]:
            score += 1; signals.append("EMA9 > EMA21 (Bullish)")
        else:
            score -= 1; signals.append("EMA9 < EMA21 (Bearish)")

    # Bollinger
    bb = ind["bollinger"]
    if price < bb["lower"]:
        score += 2; signals.append(f"Price below BB lower (Oversold) 🟢")
    elif price > bb["upper"]:
        score -= 2; signals.append(f"Price above BB upper (Overbought) 🔴")
    else:
        signals.append(f"Price inside Bollinger Bands")

    # Volume
    vol = ind["volume_signal"]
    if vol == "BULLISH":
        score += 1; signals.append("Volume Bullish (high buy volume) 🟢")
    elif vol == "BEARISH":
        score -= 1; signals.append("Volume Bearish (high sell volume) 🔴")

    return {"score": score, "signals": signals}


def get_ai_analysis(symbol: str, market: str, ind: dict, score: int) -> str:
    """GPT analysis for confluence. Falls back to rule-based if no key."""
    if not OPENAI_API_KEY:
        return _rule_based_reasoning(symbol, ind, score)

    try:
        prompt = f"""You are an expert technical analyst. Analyze this market data and give a concise trading signal assessment.

Symbol: {symbol} ({market})
Current Price: {ind['price']}
RSI: {ind['rsi']}
MACD: {ind['macd']}
Bollinger Bands: {ind['bollinger']}
EMA9: {ind['ema9']}, EMA21: {ind['ema21']}, EMA50: {ind['ema50']}
Volume Signal: {ind['volume_signal']}
Technical Score: {score}/10

In 2-3 sentences, give your confluence analysis and whether this is a HIGH, MEDIUM, or LOW confidence signal. Be direct."""

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
        body = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 150,
            "temperature": 0.3
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          json=body, headers=headers, timeout=15)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"AI analysis error: {e}")
        return _rule_based_reasoning(symbol, ind, score)


def _rule_based_reasoning(symbol: str, ind: dict, score: int) -> str:
    rsi = ind["rsi"]
    macd = ind["macd"]
    direction = "BULLISH" if score > 0 else "BEARISH"
    confidence = "HIGH" if abs(score) >= 6 else "MEDIUM" if abs(score) >= 3 else "LOW"

    reasons = []
    if rsi < 35:
        reasons.append("RSI shows oversold conditions")
    elif rsi > 65:
        reasons.append("RSI shows overbought conditions")
    if macd["histogram"] > 0:
        reasons.append("MACD shows positive momentum")
    elif macd["histogram"] < 0:
        reasons.append("MACD shows negative momentum")

    return f"{direction} signal with {confidence} confidence. {'. '.join(reasons)}. Score: {score}/10."


def calculate_targets(price: float, direction: str, market: str) -> dict:
    """Calculate entry, stop loss, and targets"""
    is_crypto = market == "CRYPTO"
    if direction == "BUY":
        sl_pct   = 0.025 if is_crypto else 0.02
        t1_pct   = 0.04  if is_crypto else 0.03
        t2_pct   = 0.08  if is_crypto else 0.06
        return {
            "entry":  round(price, 4),
            "stop_loss": round(price * (1 - sl_pct), 4),
            "target1":   round(price * (1 + t1_pct), 4),
            "target2":   round(price * (1 + t2_pct), 4),
            "risk_reward": round(t1_pct / sl_pct, 1)
        }
    else:
        sl_pct   = 0.025 if is_crypto else 0.02
        t1_pct   = 0.04  if is_crypto else 0.03
        t2_pct   = 0.08  if is_crypto else 0.06
        return {
            "entry":  round(price, 4),
            "stop_loss": round(price * (1 + sl_pct), 4),
            "target1":   round(price * (1 - t1_pct), 4),
            "target2":   round(price * (1 - t2_pct), 4),
            "risk_reward": round(t1_pct / sl_pct, 1)
        }


def generate_signal(symbol: str, market: str, candles: list[dict]) -> Optional[dict]:
    """Full signal generation pipeline"""
    ind = compute_indicators(candles)
    if not ind:
        return None

    scored = score_indicators(ind)
    score  = scored["score"]

    # Only send HIGH CONFIDENCE signals (score >= 5 or <= -5)
    if abs(score) < 5:
        logger.info(f"  {symbol}: score={score} — below threshold, skipping")
        return None

    direction = "BUY" if score > 0 else "SELL"
    confidence_pct = min(int(abs(score) / 10 * 100), 95)
    ai_reason = get_ai_analysis(symbol, market, ind, score)
    targets   = calculate_targets(ind["price"], direction, market)

    signal = {
        "id":           f"{symbol}_{int(time.time())}",
        "symbol":       symbol,
        "market":       market,
        "direction":    direction,
        "price":        ind["price"],
        "confidence":   confidence_pct,
        "score":        score,
        "indicators":   ind,
        "signals_list": scored["signals"],
        "ai_reasoning": ai_reason,
        "targets":      targets,
        "timestamp":    datetime.now().isoformat(),
        "status":       "ACTIVE",
        "result":       None,
    }
    return signal

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(signal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return

    direction_emoji = "🟢 BUY" if signal["direction"] == "BUY" else "🔴 SELL"
    market_emoji    = "₿" if signal["market"] == "CRYPTO" else "📈"
    t = signal["targets"]

    msg = f"""
{market_emoji} <b>TRADING SIGNAL</b> {market_emoji}

<b>Symbol:</b> {signal['symbol']}
<b>Signal:</b> {direction_emoji}
<b>Confidence:</b> {signal['confidence']}%
<b>Score:</b> {signal['score']}/10

<b>📊 Price Levels:</b>
• Entry: {t['entry']}
• Stop Loss: {t['stop_loss']}
• Target 1: {t['target1']}
• Target 2: {t['target2']}
• Risk:Reward = 1:{t['risk_reward']}

<b>📉 Indicators:</b>
{chr(10).join('• ' + s for s in signal['signals_list'][:4])}

<b>🤖 AI Analysis:</b>
{signal['ai_reasoning']}

<i>⏰ {signal['timestamp'][:16]}</i>
<i>⚠️ Not financial advice. Do your own research.</i>
""".strip()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
        if r.status_code == 200:
            logger.info(f"  ✅ Telegram sent for {signal['symbol']}")
        else:
            logger.error(f"  ❌ Telegram error: {r.text}")
    except Exception as e:
        logger.error(f"  ❌ Telegram exception: {e}")

# ─── SIGNAL STORE ─────────────────────────────────────────────────────────────

SIGNALS_FILE = "signals.json"

def load_signals() -> list:
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    except:
        return []

def save_signals(signals: list):
    with open(SIGNALS_FILE, "w") as f:
        json.dump(signals[-200:], f, indent=2)  # keep last 200

def add_signal(signal: dict):
    signals = load_signals()
    signals.append(signal)
    save_signals(signals)

def get_win_rate() -> dict:
    signals = load_signals()
    closed  = [s for s in signals if s.get("result") in ("WIN","LOSS")]
    wins    = [s for s in closed if s["result"] == "WIN"]
    return {
        "total":    len(signals),
        "closed":   len(closed),
        "wins":     len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0
    }

# ─── MAIN SCAN LOOP ───────────────────────────────────────────────────────────

def scan_crypto():
    logger.info("🔍 Scanning Crypto markets...")
    new_signals = []
    for symbol in CRYPTO_SYMBOLS:
        logger.info(f"  Analyzing {symbol}...")
        candles = get_crypto_candles(symbol, interval="1h", limit=100)
        if not candles:
            continue
        signal = generate_signal(symbol, "CRYPTO", candles)
        if signal:
            logger.info(f"  ⚡ SIGNAL: {symbol} {signal['direction']} ({signal['confidence']}%)")
            add_signal(signal)
            send_telegram(signal)

            # AUTO-TRADE on Binance if confidence is high enough
            if AUTO_TRADE_ENABLED and signal["confidence"] >= MIN_CONFIDENCE:
                side = "BUY" if signal["direction"] == "BUY" else "SELL"
                price = get_binance_price(symbol)
                if price:
                    order = place_binance_order(symbol, side, TRADE_AMOUNT_USDT, signal)
                    if order:
                        notify_trade_telegram(symbol, side,
                            float(order.get("executedQty", 0)),
                            price, signal, order)

            new_signals.append(signal)
        time.sleep(0.5)
    return new_signals


def scan_indian_stocks():
    logger.info("🔍 Scanning Nifty 50 stocks...")
    new_signals = []
    for symbol in INDIAN_SYMBOLS:
        logger.info(f"  Analyzing {symbol}...")
        candles = get_indian_stock_yahoo(symbol)
        if not candles:
            candles = get_indian_stock_candles(symbol)
        if not candles:
            continue
        signal = generate_signal(symbol, "INDIA", candles)
        if signal:
            logger.info(f"  ⚡ SIGNAL: {symbol} {signal['direction']} ({signal['confidence']}%)")
            add_signal(signal)
            send_telegram(signal)
            new_signals.append(signal)
        time.sleep(0.5)
    return new_signals


def get_index_candles_yahoo(yahoo_symbol: str) -> list[dict]:
    """Fetch index data (Nifty/BankNifty/Sensex) from Yahoo Finance"""
    try:
        import yfinance as yf
        ticker = yf.Ticker(yahoo_symbol)
        hist = ticker.history(period="3mo", interval="1d")
        candles = []
        for ts, row in hist.iterrows():
            candles.append({
                "time":   ts.timestamp(),
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": float(row["Volume"]) if row["Volume"] > 0 else 1000000,
            })
        return candles
    except Exception as e:
        logger.error(f"Index fetch error {yahoo_symbol}: {e}")
        return []


def send_options_signal_telegram(name: str, direction: str, price: float,
                                  ind: dict, score: int, confidence: int):
    """Send formatted options trading signal to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    rsi  = ind["rsi"]
    bb   = ind["bollinger"]

    # Options recommendation based on direction
    if direction == "BUY":
        option_type = "CALL (CE) 📈"
        strike_hint = f"ATM or slightly OTM CE"
        emoji = "🟢"
    else:
        option_type = "PUT (PE) 📉"
        strike_hint = f"ATM or slightly OTM PE"
        emoji = "🔴"

    # Suggest expiry
    expiry_hint = "Weekly expiry (nearest Thursday)"

    msg = f"""
🎯 <b>OPTIONS SIGNAL</b> 🎯

<b>Index:</b> {name}
<b>Spot Price:</b> {price:,.2f}
<b>Signal:</b> {emoji} {option_type}
<b>Confidence:</b> {confidence}%
<b>Score:</b> {score}/10

<b>📋 Options Strategy:</b>
• Buy: <b>{strike_hint}</b>
• Expiry: {expiry_hint}
• Entry: At market open / current price

<b>📊 Key Levels:</b>
• RSI: {rsi} {'(Oversold)' if rsi < 35 else '(Overbought)' if rsi > 65 else '(Neutral)'}
• BB Upper: {bb['upper']:,.0f}
• BB Lower: {bb['lower']:,.0f}

<b>⚠️ Risk Management:</b>
• Max loss: 20-30% of premium
• Book partial profit at 50% gain
• Exit if spot crosses Stop Level

<i>⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>
<i>⚠️ Not financial advice. Options are high risk!</i>
""".strip()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
        if r.status_code == 200:
            logger.info(f"  ✅ Options signal sent for {name}")
    except Exception as e:
        logger.error(f"  ❌ Options telegram error: {e}")


def scan_options():
    """Scan Nifty/BankNifty/Sensex for options trading signals"""
    logger.info("🎯 Scanning Options (Nifty/BankNifty/Sensex)...")
    new_signals = []

    for opt in OPTIONS_SYMBOLS:
        name   = opt["name"]
        yahoo  = opt["yahoo"]
        logger.info(f"  Analyzing {name}...")

        candles = get_index_candles_yahoo(yahoo)
        if not candles:
            continue

        ind = compute_indicators(candles)
        if not ind:
            continue

        scored     = score_indicators(ind)
        score      = scored["score"]
        confidence = min(int(abs(score) / 10 * 100), 95)

        # Options signals: lower threshold (score >= 4) since indices move less
        if abs(score) < 4:
            logger.info(f"  {name}: score={score} — below threshold, skipping")
            continue

        direction = "BUY" if score > 0 else "SELL"
        price     = ind["price"]

        logger.info(f"  ⚡ OPTIONS SIGNAL: {name} {direction} CE/PE ({confidence}%)")

        # Save as signal
        signal = {
            "id":           f"{name}_OPT_{int(time.time())}",
            "symbol":       f"{name} OPTIONS",
            "market":       "OPTIONS",
            "direction":    direction,
            "price":        price,
            "confidence":   confidence,
            "score":        score,
            "indicators":   ind,
            "signals_list": scored["signals"],
            "ai_reasoning": f"{'CALL' if direction=='BUY' else 'PUT'} recommended based on technical score {score}/10",
            "targets":      {"entry": price, "stop_loss": 0, "target1": 0, "target2": 0, "risk_reward": 2},
            "timestamp":    datetime.now().isoformat(),
            "status":       "ACTIVE",
            "result":       None,
        }
        add_signal(signal)
        send_options_signal_telegram(name, direction, price, ind, score, confidence)
        new_signals.append(signal)
        time.sleep(0.5)

    return new_signals


def run_scan():
    logger.info(f"\n{'='*50}")
    logger.info(f"🚀 Starting scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*50}")

    crypto_signals  = scan_crypto()
    stock_signals   = scan_indian_stocks()
    options_signals = scan_options()

    total = len(crypto_signals) + len(stock_signals) + len(options_signals)
    stats = get_win_rate()
    logger.info(f"\n✅ Scan complete. {total} new signals generated.")
    logger.info(f"📊 Win rate: {stats['win_rate']}% ({stats['wins']}/{stats['closed']} closed)")
    return crypto_signals + stock_signals + options_signals


def main():
    logger.info("🤖 Trading Signal Bot started!")
    logger.info(f"⏱  Scan interval: every {SCAN_INTERVAL_MIN} minutes")
    logger.info(f"📱 Telegram: {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    logger.info(f"🔑 Alpha Vantage: {'✅ configured' if ALPHA_VANTAGE_KEY != 'demo' else '⚠️  demo key'}")
    logger.info(f"🤖 AI Analysis: {'✅ OpenAI' if OPENAI_API_KEY else '⚠️  rule-based fallback'}")
    logger.info(f"⚡ Auto-Trade: {'✅ ENABLED (Binance) — ₹1000/trade' if AUTO_TRADE_ENABLED else '❌ disabled (no Binance keys)'}")

    while True:
        try:
            run_scan()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"Scan error: {e}", exc_info=True)

        logger.info(f"😴 Sleeping {SCAN_INTERVAL_MIN} minutes...")
        time.sleep(SCAN_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
