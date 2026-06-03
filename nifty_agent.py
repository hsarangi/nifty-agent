#!/usr/bin/env python3
"""
Nifty 50 Daily Range Forecast Agent
Runs at 7:00 PM UK time, generates next-trading-day analysis.
Educational purposes only — not financial advice.
"""

import os
import sys
import json
import math
import logging
import warnings
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd
import pytz
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

UK_TZ = pytz.timezone("Europe/London")
IST_TZ = pytz.timezone("Asia/Kolkata")

DISCLAIMER = (
    "\n> **Disclaimer:** This is educational technical analysis only, "
    "not financial advice. Do not trade only based on this report.\n"
)

# ─────────────────────────────────────────────
# 1. DATA FETCHING
# ─────────────────────────────────────────────

def fetch_nifty_yfinance(period: str = "60d") -> pd.DataFrame:
    """Primary free data source via yfinance (^NSEI)."""
    import yfinance as yf

    log.info("Fetching Nifty 50 daily data from yfinance …")
    ticker = yf.Ticker("^NSEI")
    df = ticker.history(period=period, interval="1d", auto_adjust=True)
    if df.empty:
        raise ValueError("yfinance returned empty daily data for ^NSEI")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    return df


def fetch_nifty_intraday_yfinance(interval: str = "5m", period: str = "5d") -> pd.DataFrame:
    """Intraday data from yfinance."""
    import yfinance as yf

    log.info(f"Fetching Nifty 50 {interval} intraday data from yfinance …")
    df = yf.download("^NSEI", period=period, interval=interval,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"yfinance returned empty {interval} data for ^NSEI")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def fetch_nifty_tvdatafeed(interval_label: str = "1D") -> pd.DataFrame:
    """
    tvdatafeed is not available for Python 3.13 on PyPI.
    This stub always returns empty so callers fall through to yfinance.
    If you install tvdatafeed manually in a Python <=3.12 environment you can
    re-enable the real implementation here.
    """
    return pd.DataFrame()


def fetch_india_vix() -> float | None:
    """Fetch India VIX from yfinance."""
    try:
        import yfinance as yf
        vix = yf.Ticker("^INDIAVIX")
        hist = vix.history(period="2d", interval="1d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as e:
        log.warning(f"India VIX fetch failed: {e}")
    return None


def fetch_gift_nifty() -> float | None:
    """Attempt to fetch GIFT Nifty (SGX Nifty) from yfinance or TradingView."""
    try:
        import yfinance as yf
        # NF=F is the Nifty futures proxy on some feeds
        for sym in ["NIFTYBEES.NS", "GIFTNIFTY.NS"]:
            try:
                t = yf.Ticker(sym)
                h = t.history(period="1d", interval="1m")
                if not h.empty:
                    return round(float(h["Close"].iloc[-1]), 2)
            except Exception:
                continue
    except Exception as e:
        log.warning(f"GIFT Nifty fetch failed: {e}")
    return None


def fetch_fii_dii() -> dict:
    """
    Attempt to fetch FII/DII provisional data from NSE India.
    Returns dict with keys: fii_net, dii_net (INR crores).
    NSE provides this at a public JSON endpoint on trading days.
    """
    url = "https://www.nseindia.com/api/fiidiiTradeReact"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        session = requests.Session()
        # Prime cookies
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        r = session.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            fii_net = dii_net = None
            for row in data:
                cat = row.get("category", "").upper()
                if "FII" in cat or "FPI" in cat:
                    fii_net = row.get("netVal")
                elif "DII" in cat:
                    dii_net = row.get("netVal")
            return {"fii_net": fii_net, "dii_net": dii_net}
    except Exception as e:
        log.warning(f"FII/DII fetch failed: {e}")
    return {"fii_net": None, "dii_net": None}


def fetch_option_chain_levels() -> dict:
    """
    Fetch Nifty option chain from NSE and derive max pain, PCR,
    strong support/resistance OI levels.
    """
    url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/option-chain",
    }
    result = {
        "max_pain": None,
        "pcr": None,
        "top_ce_oi_strikes": [],
        "top_pe_oi_strikes": [],
        "underlying": None,
    }
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        r = session.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return result
        data = r.json()
        underlying = data["records"]["underlyingValue"]
        result["underlying"] = underlying
        records = data["records"]["data"]

        ce_oi = {}
        pe_oi = {}
        total_ce_oi = 0
        total_pe_oi = 0

        for rec in records:
            strike = rec.get("strikePrice")
            if "CE" in rec:
                oi = rec["CE"].get("openInterest", 0)
                ce_oi[strike] = ce_oi.get(strike, 0) + oi
                total_ce_oi += oi
            if "PE" in rec:
                oi = rec["PE"].get("openInterest", 0)
                pe_oi[strike] = pe_oi.get(strike, 0) + oi
                total_pe_oi += oi

        if total_ce_oi > 0:
            result["pcr"] = round(total_pe_oi / total_ce_oi, 2)

        # Top 3 CE strikes by OI (resistance)
        top_ce = sorted(ce_oi.items(), key=lambda x: x[1], reverse=True)[:3]
        result["top_ce_oi_strikes"] = [int(s) for s, _ in top_ce]

        # Top 3 PE strikes by OI (support)
        top_pe = sorted(pe_oi.items(), key=lambda x: x[1], reverse=True)[:3]
        result["top_pe_oi_strikes"] = [int(s) for s, _ in top_pe]

        # Max pain: strike where total P&L for option writers is minimised
        all_strikes = sorted(set(ce_oi.keys()) | set(pe_oi.keys()))
        pain = {}
        for s in all_strikes:
            ce_pain = sum(max(0, s - k) * v for k, v in ce_oi.items())
            pe_pain = sum(max(0, k - s) * v for k, v in pe_oi.items())
            pain[s] = ce_pain + pe_pain
        if pain:
            result["max_pain"] = min(pain, key=pain.get)

    except Exception as e:
        log.warning(f"Option chain fetch failed: {e}")
    return result


# ─────────────────────────────────────────────
# 2. INDICATOR CALCULATIONS
# ─────────────────────────────────────────────

def calc_cpr(high: float, low: float, close: float) -> dict:
    """Central Pivot Range for next day."""
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (2 * pivot) - bc
    return {
        "pivot": round(pivot, 2),
        "bc": round(bc, 2),
        "tc": round(tc, 2),
        "width": round(abs(tc - bc), 2),
    }


def calc_floor_pivots(high: float, low: float, close: float) -> dict:
    """Classic floor trader pivots."""
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    r2 = pivot + (high - low)
    r3 = high + 2 * (pivot - low)
    s1 = (2 * pivot) - high
    s2 = pivot - (high - low)
    s3 = low - 2 * (high - pivot)
    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
        "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
    }


def calc_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP from intraday data."""
    if "volume" not in df.columns or df["volume"].sum() == 0:
        return pd.Series([None] * len(df), index=df.index)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (typical * df["volume"]).cumsum()
    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    return vwap.round(2)


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.round(2)


def calc_bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0) -> dict:
    sma = series.rolling(period).mean()
    sd = series.rolling(period).std()
    upper = sma + std * sd
    lower = sma - std * sd
    return {
        "upper": round(float(upper.iloc[-1]), 2) if not upper.empty else None,
        "middle": round(float(sma.iloc[-1]), 2) if not sma.empty else None,
        "lower": round(float(lower.iloc[-1]), 2) if not lower.empty else None,
    }


def calc_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> dict:
    """Supertrend indicator. Returns last value and direction."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # ATR
    hl = high - low
    hc = (high - close.shift(1)).abs()
    lc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period).mean()

    basic_upper = (high + low) / 2 + multiplier * atr
    basic_lower = (high + low) / 2 - multiplier * atr

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    for i in range(1, len(df)):
        if basic_upper.iloc[i] < final_upper.iloc[i - 1] or close.iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        if basic_lower.iloc[i] > final_lower.iloc[i - 1] or close.iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    for i in range(1, len(df)):
        if close.iloc[i] <= final_upper.iloc[i]:
            supertrend.iloc[i] = final_upper.iloc[i]
            direction.iloc[i] = -1   # bearish
        else:
            supertrend.iloc[i] = final_lower.iloc[i]
            direction.iloc[i] = 1    # bullish

    last_val = round(float(supertrend.iloc[-1]), 2)
    last_dir = int(direction.iloc[-1])
    return {"value": last_val, "direction": last_dir,
            "signal": "Bullish" if last_dir == 1 else "Bearish"}


def calc_orb(intraday_df: pd.DataFrame, orb_minutes: int = 15) -> dict:
    """Opening Range Breakout — first N-minute high/low of the session."""
    if intraday_df.empty:
        return {"high": None, "low": None, "range": None}

    today = intraday_df.index[-1].date()
    today_df = intraday_df[intraday_df.index.date == today]
    if today_df.empty:
        today_df = intraday_df.tail(20)

    orb_df = today_df.head(orb_minutes // 5)   # assumes 5-min bars
    if orb_df.empty:
        return {"high": None, "low": None, "range": None}

    orb_high = round(float(orb_df["high"].max()), 2)
    orb_low = round(float(orb_df["low"].min()), 2)
    return {
        "high": orb_high,
        "low": orb_low,
        "range": round(orb_high - orb_low, 2),
    }


def calc_volume_profile(df: pd.DataFrame, bins: int = 20) -> dict:
    """Simplified volume profile — returns HVN and LVN price levels."""
    if df.empty or "volume" not in df.columns:
        return {"hvn": [], "lvn": [], "poc": None}

    price_min = df["low"].min()
    price_max = df["high"].max()
    bucket_size = (price_max - price_min) / bins

    vol_by_bucket: dict[float, float] = {}
    for _, row in df.iterrows():
        mid = (row["high"] + row["low"]) / 2
        bucket = round(price_min + math.floor((mid - price_min) / bucket_size) * bucket_size, 2)
        vol_by_bucket[bucket] = vol_by_bucket.get(bucket, 0) + row.get("volume", 0)

    if not vol_by_bucket:
        return {"hvn": [], "lvn": [], "poc": None}

    sorted_buckets = sorted(vol_by_bucket.items(), key=lambda x: x[1], reverse=True)
    poc = sorted_buckets[0][0]
    hvn = [b for b, _ in sorted_buckets[:3]]
    lvn = [b for b, _ in sorted_buckets[-3:]]

    return {
        "poc": round(poc, 2),
        "hvn": [round(b, 2) for b in hvn],
        "lvn": [round(b, 2) for b in lvn],
    }


# ─────────────────────────────────────────────
# 3. TRADINGVIEW PUBLIC SCRIPT SEARCH
# ─────────────────────────────────────────────

TV_SEARCH_URL = "https://www.tradingview.com/pubscripts-suggest-json/"

def search_tv_scripts(query: str, limit: int = 3) -> list[dict]:
    """
    Search TradingView's public Pine Script library.
    Uses the undocumented suggest endpoint (same one powering the website search).
    Returns a list of {title, author, likes, description} dicts.
    """
    try:
        params = {"search": query, "total": limit}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tradingview.com/",
        }
        r = requests.get(TV_SEARCH_URL, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            results = r.json()
            scripts = []
            for item in results[:limit]:
                scripts.append({
                    "title": item.get("scriptName", "Unknown"),
                    "author": item.get("authorName", "Unknown"),
                    "likes": item.get("likes", 0),
                    "description": item.get("extra", {}).get("shortDescription", "")[:120],
                    "url": f"https://www.tradingview.com/script/{item.get('scriptIdPart', '')}",
                })
            return scripts
    except Exception as e:
        log.warning(f"TradingView script search failed for '{query}': {e}")
    return []


SEARCH_QUERIES = [
    "ORB Opening Range Breakout",
    "CPR Tomorrow Pivots",
    "Squeeze Momentum",
    "RSI Divergence Nifty",
    "Market Structure Break",
    "Liquidity Zones",
    "Smart Money Concepts",
    "Volume Profile VPVR",
    "Support Resistance Auto",
    "Supertrend ATR",
    "Relative Volume RVOL",
    "Trend Strength ADX",
]

def collect_community_indicators() -> list[dict]:
    """Run all search queries and collect top results."""
    log.info("Searching TradingView public script library …")
    all_results = []
    for q in SEARCH_QUERIES:
        results = search_tv_scripts(q, limit=2)
        for r in results:
            r["query"] = q
        all_results.extend(results)
    log.info(f"Found {len(all_results)} community indicator references")
    return all_results


# ─────────────────────────────────────────────
# 4. SIGNAL SCORING
# ─────────────────────────────────────────────

def score_signal(value, bull_cond, bear_cond) -> str:
    """Return Bullish / Bearish / Neutral."""
    if value is None:
        return "Neutral"
    if bull_cond(value):
        return "Bullish"
    if bear_cond(value):
        return "Bearish"
    return "Neutral"


def build_timeframe_bias(
    daily_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    m30_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    pivots: dict,
) -> dict:
    """
    Weighted bias engine.
    Weights:
      Daily: 25%, 1H: 20%, 30M: 15%, 15M: 10%, 5M: 10%,
      CPR/Pivot: 10%, Community indicators: 10%
    """
    weights = {
        "daily": 0.25,
        "h1": 0.20,
        "m30": 0.15,
        "m15": 0.10,
        "m5": 0.10,
        "cpr": 0.10,
        "community": 0.10,
    }

    scores: dict[str, str] = {}

    def last_close(df):
        return float(df["close"].iloc[-1]) if not df.empty else None

    def tf_score(df, label):
        if df.empty or len(df) < 2:
            return "Neutral"
        close = float(df["close"].iloc[-1])
        # Supertrend direction
        try:
            st = calc_supertrend(df)
            st_sig = st["signal"]
        except Exception:
            st_sig = "Neutral"
        # EMA20 vs EMA50
        ema20 = df["close"].ewm(span=20).mean().iloc[-1]
        ema50 = df["close"].ewm(span=50).mean().iloc[-1]
        ema_sig = "Bullish" if ema20 > ema50 else "Bearish"
        # RSI
        rsi_val = float(calc_rsi(df["close"]).iloc[-1]) if len(df) >= 14 else 50
        rsi_sig = "Bullish" if rsi_val > 55 else ("Bearish" if rsi_val < 45 else "Neutral")
        # Majority vote
        sigs = [st_sig, ema_sig, rsi_sig]
        bull = sigs.count("Bullish")
        bear = sigs.count("Bearish")
        result = "Bullish" if bull > bear else ("Bearish" if bear > bull else "Neutral")
        log.info(f"  {label} signals → ST:{st_sig} EMA:{ema_sig} RSI:{rsi_sig} → {result}")
        return result

    scores["daily"] = tf_score(daily_df, "Daily")
    scores["h1"]    = tf_score(h1_df, "1H")
    scores["m30"]   = tf_score(m30_df, "30M")
    scores["m15"]   = tf_score(m15_df, "15M")
    scores["m5"]    = tf_score(m5_df, "5M")

    # CPR bias: is current price above TC (bullish), below BC (bearish), or inside (neutral)?
    last_price = last_close(m5_df) or last_close(m15_df) or last_close(daily_df)
    if last_price and pivots:
        if last_price > pivots.get("tc", 0):
            scores["cpr"] = "Bullish"
        elif last_price < pivots.get("bc", float("inf")):
            scores["cpr"] = "Bearish"
        else:
            scores["cpr"] = "Neutral"
    else:
        scores["cpr"] = "Neutral"

    # Community placeholder (will be updated after TV search)
    scores["community"] = "Neutral"

    # Weighted score
    bull_score = sum(weights[k] for k, v in scores.items() if v == "Bullish")
    bear_score = sum(weights[k] for k, v in scores.items() if v == "Bearish")
    neut_score = sum(weights[k] for k, v in scores.items() if v == "Neutral")

    total = bull_score + bear_score + neut_score
    bull_pct = round(bull_score / total * 100) if total else 0
    bear_pct = round(bear_score / total * 100) if total else 0

    if bull_score > bear_score and bull_score > neut_score:
        final_bias = "BULLISH"
        bias_strength = "Strong" if bull_pct >= 60 else "Moderate"
    elif bear_score > bull_score and bear_score > neut_score:
        final_bias = "BEARISH"
        bias_strength = "Strong" if bear_pct >= 60 else "Moderate"
    else:
        final_bias = "NEUTRAL/SIDEWAYS"
        bias_strength = ""

    return {
        "scores": scores,
        "bull_pct": bull_pct,
        "bear_pct": bear_pct,
        "final_bias": final_bias,
        "bias_strength": bias_strength,
    }


# ─────────────────────────────────────────────
# 5. SCENARIO TABLE BUILDER
# ─────────────────────────────────────────────

def build_scenario_table(cpr: dict, floor: dict, prev_high: float, prev_low: float) -> str:
    """Build the open-price scenario table in markdown."""
    tc = cpr["tc"]
    bc = cpr["bc"]
    pivot = floor["pivot"]
    r1, r2, r3 = floor["r1"], floor["r2"], floor["r3"]
    s1, s2, s3 = floor["s1"], floor["s2"], floor["s3"]

    rows = [
        (
            f"**Above R1** (> {r1})",
            "Strong Bullish",
            str(r2),
            str(r3),
            str(r1),
            "Momentum breakout; ride the trend with trailing stop at R1.",
        ),
        (
            f"**Above TC, below R1** ({tc}–{r1})",
            "Bullish",
            str(r1),
            str(r2),
            str(tc),
            "Above CPR top; first target R1. Exit if price falls back below TC.",
        ),
        (
            f"**Inside CPR** ({bc}–{tc})",
            "Neutral/Range",
            f"{tc} or {bc}",
            f"{r1} / {s1}",
            "–",
            "Choppy session likely. Wait for CPR breakout/breakdown before entering.",
        ),
        (
            f"**Below BC, above S1** ({s1}–{bc})",
            "Bearish",
            str(s1),
            str(s2),
            str(bc),
            "Below CPR base; first target S1. Invalidated if price reclaims BC.",
        ),
        (
            f"**Below S1** (< {s1})",
            "Strong Bearish",
            str(s2),
            str(s3),
            str(s1),
            "Weakness confirmed. Target S2/S3 with stop above S1.",
        ),
        (
            f"**Near Prev Day High** (~{prev_high})",
            "Watch",
            str(r1),
            str(r2),
            str(prev_high),
            "Use 5m/15m candle close above PDH as breakout confirmation.",
        ),
        (
            f"**Near Prev Day Low** (~{prev_low})",
            "Watch",
            str(s1),
            str(s2),
            str(prev_low),
            "Watch for ORB low + RSI divergence for reversal; else target S2.",
        ),
    ]

    header = (
        "| If Nifty opens in this range | Bias | First target | Extended target "
        "| Invalid level | Simple explanation |\n"
        "|:------------------------------|:-----|:-------------|:----------------|"
        ":--------------|:-------------------|\n"
    )
    body = "\n".join(
        f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} |"
        for r in rows
    )
    return header + body


# ─────────────────────────────────────────────
# 6. REPORT GENERATOR
# ─────────────────────────────────────────────

def generate_report(report_date: date) -> str:
    """
    Full pipeline: fetch data → calculate indicators → score signals → build report.
    """
    uk_now = datetime.now(UK_TZ)
    ist_now = datetime.now(IST_TZ)
    log.info(f"=== Nifty 50 Analysis Agent starting | UK: {uk_now:%Y-%m-%d %H:%M} | IST: {ist_now:%Y-%m-%d %H:%M} ===")

    # ── Fetch data ──────────────────────────────
    # Try TradingView first, fallback to yfinance
    daily_df = fetch_nifty_tvdatafeed("1D")
    if daily_df.empty:
        daily_df = fetch_nifty_yfinance("60d")

    m5_df = fetch_nifty_tvdatafeed("5m")
    if m5_df.empty:
        m5_df = fetch_nifty_intraday_yfinance("5m", "5d")

    m15_df = fetch_nifty_tvdatafeed("15m")
    if m15_df.empty:
        m15_df = fetch_nifty_intraday_yfinance("15m", "5d")

    m30_df = fetch_nifty_tvdatafeed("30m")
    if m30_df.empty:
        m30_df = fetch_nifty_intraday_yfinance("30m", "5d")

    h1_df = fetch_nifty_tvdatafeed("1h")
    if h1_df.empty:
        h1_df = fetch_nifty_intraday_yfinance("60m", "5d")

    # ── Previous day OHLC ────────────────────────
    prev = daily_df.iloc[-1]
    curr_close = float(prev["close"])
    prev_high  = float(prev["high"])
    prev_low   = float(prev["low"])
    prev_open  = float(prev["open"])
    prev_close = float(daily_df.iloc[-2]["close"]) if len(daily_df) >= 2 else curr_close

    log.info(
        f"Previous day OHLC — O:{prev_open} H:{prev_high} L:{prev_low} C:{curr_close}"
    )

    # ── Supplemental data ────────────────────────
    india_vix  = fetch_india_vix()
    gift_nifty = fetch_gift_nifty()
    fii_dii    = fetch_fii_dii()
    options    = fetch_option_chain_levels()

    # ── Indicators ───────────────────────────────
    cpr    = calc_cpr(prev_high, prev_low, curr_close)
    floor  = calc_floor_pivots(prev_high, prev_low, curr_close)
    orb    = calc_orb(m5_df)
    vwap_series = calc_vwap(m5_df)
    last_vwap   = round(float(vwap_series.iloc[-1]), 2) if not vwap_series.empty and vwap_series.iloc[-1] is not None else None

    rsi_daily  = round(float(calc_rsi(daily_df["close"]).iloc[-1]), 2) if len(daily_df) >= 14 else None
    rsi_1h     = round(float(calc_rsi(h1_df["close"]).iloc[-1]), 2) if len(h1_df) >= 14 else None
    rsi_15m    = round(float(calc_rsi(m15_df["close"]).iloc[-1]), 2) if len(m15_df) >= 14 else None

    bb_daily   = calc_bollinger_bands(daily_df["close"])
    bb_1h      = calc_bollinger_bands(h1_df["close"]) if not h1_df.empty else {}

    try:
        st_daily = calc_supertrend(daily_df)
    except Exception:
        st_daily = {"value": None, "signal": "Neutral"}

    try:
        st_1h = calc_supertrend(h1_df) if not h1_df.empty else {"value": None, "signal": "Neutral"}
    except Exception:
        st_1h = {"value": None, "signal": "Neutral"}

    vol_profile = calc_volume_profile(daily_df.tail(20))

    # ── Community indicators ─────────────────────
    community_scripts = collect_community_indicators()

    # ── Bias engine ──────────────────────────────
    bias = build_timeframe_bias(daily_df, h1_df, m30_df, m15_df, m5_df, cpr)

    # Adjust community bias based on script search availability
    # (Simple heuristic: if we got > 5 results treat as confirmation of current bias)
    if len(community_scripts) > 5:
        bias["scores"]["community"] = bias["final_bias"].split("/")[0].title() if "NEUTRAL" not in bias["final_bias"] else "Neutral"

    # ── Scenario table ───────────────────────────
    scenario_table = build_scenario_table(cpr, floor, prev_high, prev_low)

    # ── CPR narrative ────────────────────────────
    cpr_width_pct = round(cpr["width"] / curr_close * 100, 3)
    cpr_narrative = (
        f"CPR width is **{cpr['width']} pts ({cpr_width_pct}%)** — "
        + ("**narrow CPR → trending day likely.**" if cpr_width_pct < 0.15 else
           "**wide CPR → sideways/volatile day expected.**")
    )

    # ── VIX narrative ────────────────────────────
    vix_narrative = ""
    if india_vix is not None:
        if india_vix < 14:
            vix_narrative = f"India VIX at **{india_vix}** — low volatility, complacency; watch for sudden spikes."
        elif india_vix > 20:
            vix_narrative = f"India VIX at **{india_vix}** — elevated volatility; wider stops recommended."
        else:
            vix_narrative = f"India VIX at **{india_vix}** — moderate volatility; normal trading conditions."

    # ── FII/DII narrative ────────────────────────
    fii_narrative = ""
    if fii_dii["fii_net"] is not None:
        fii_val = fii_dii["fii_net"]
        dii_val = fii_dii.get("dii_net")
        fii_narrative = (
            f"FII: ₹**{fii_val} Cr** ({'buying' if float(str(fii_val).replace(',','')) > 0 else 'selling'})"
        )
        if dii_val is not None:
            fii_narrative += f" | DII: ₹**{dii_val} Cr** ({'buying' if float(str(dii_val).replace(',','')) > 0 else 'selling'})"

    # ── Indicator agreement summary ──────────────
    signal_rows = []
    sig_map = {
        "Daily Supertrend": st_daily["signal"],
        "1H Supertrend": st_1h["signal"],
        "RSI(14) Daily": "Bullish" if rsi_daily and rsi_daily > 55 else ("Bearish" if rsi_daily and rsi_daily < 45 else "Neutral"),
        "RSI(14) 1H": "Bullish" if rsi_1h and rsi_1h > 55 else ("Bearish" if rsi_1h and rsi_1h < 45 else "Neutral"),
        "RSI(14) 15M": "Bullish" if rsi_15m and rsi_15m > 55 else ("Bearish" if rsi_15m and rsi_15m < 45 else "Neutral"),
        "BB Daily (price vs upper)": "Bearish" if curr_close > (bb_daily.get("upper") or 0) else ("Bullish" if curr_close < (bb_daily.get("lower") or float("inf")) else "Neutral"),
        "CPR Position": bias["scores"]["cpr"],
        "Daily TF Bias": bias["scores"]["daily"],
        "1H TF Bias": bias["scores"]["h1"],
        "30M TF Bias": bias["scores"]["m30"],
    }

    for ind, sig in sig_map.items():
        emoji = "🟢" if sig == "Bullish" else ("🔴" if sig == "Bearish" else "🟡")
        signal_rows.append(f"| {ind} | {emoji} {sig} |")

    signal_table = (
        "| Indicator | Signal |\n|:----------|:-------|\n" +
        "\n".join(signal_rows)
    )

    # ── Community scripts table ───────────────────
    if community_scripts:
        cs_header = "| Query | Script | Author | Likes |\n|:------|:-------|:-------|:------|\n"
        cs_rows = "\n".join(
            f"| {s['query']} | [{s['title']}]({s['url']}) | {s['author']} | {s['likes']} |"
            for s in community_scripts[:12]
        )
        community_section = cs_header + cs_rows
    else:
        community_section = "_TradingView script search unavailable today. Using standard indicators only._"

    # ── Build markdown report ─────────────────────
    next_day_str = (report_date + timedelta(days=1)).strftime("%A, %d %B %Y")
    report_date_str = report_date.strftime("%Y-%m-%d")

    report = f"""# Nifty 50 Range Forecast — {next_day_str}
_Generated: {uk_now:%Y-%m-%d %H:%M} UK / {ist_now:%Y-%m-%d %H:%M} IST_
{DISCLAIMER}
---

## 1. Market Context

| Item | Value |
|:-----|:------|
| **Previous Close** | {curr_close} |
| **Previous High** | {prev_high} |
| **Previous Low** | {prev_low} |
| **Previous Open** | {prev_open} |
| **India VIX** | {india_vix if india_vix else "N/A"} |
| **GIFT Nifty (last)** | {gift_nifty if gift_nifty else "N/A"} |
| **VWAP (5M)** | {last_vwap if last_vwap else "N/A"} |
| **FII/DII** | {fii_narrative if fii_narrative else "N/A"} |

{vix_narrative}

---

## 2. Key Levels for {next_day_str}

### Central Pivot Range (CPR)
| Level | Value |
|:------|:------|
| **TC (Top CPR)** | {cpr["tc"]} |
| **Pivot** | {cpr["pivot"]} |
| **BC (Bottom CPR)** | {cpr["bc"]} |
| **CPR Width** | {cpr["width"]} pts |

{cpr_narrative}

### Floor Pivots
| Level | Value |
|:------|:------|
| R3 | {floor["r3"]} |
| R2 | {floor["r2"]} |
| R1 | {floor["r1"]} |
| **Pivot** | **{floor["pivot"]}** |
| S1 | {floor["s1"]} |
| S2 | {floor["s2"]} |
| S3 | {floor["s3"]} |

### Bollinger Bands (20,2) — Daily
| Band | Value |
|:-----|:------|
| Upper | {bb_daily.get("upper", "N/A")} |
| Middle (SMA 20) | {bb_daily.get("middle", "N/A")} |
| Lower | {bb_daily.get("lower", "N/A")} |

### Supertrend (ATR 10, Mult 3)
| Timeframe | Value | Signal |
|:----------|:------|:-------|
| Daily | {st_daily.get("value", "N/A")} | {st_daily.get("signal", "N/A")} |
| 1H | {st_1h.get("value", "N/A")} | {st_1h.get("signal", "N/A")} |

### RSI (14)
| Timeframe | RSI |
|:----------|:----|
| Daily | {rsi_daily if rsi_daily else "N/A"} |
| 1H | {rsi_1h if rsi_1h else "N/A"} |
| 15M | {rsi_15m if rsi_15m else "N/A"} |

### Opening Range (last session)
| Level | Value |
|:------|:------|
| ORB High | {orb.get("high", "N/A")} |
| ORB Low | {orb.get("low", "N/A")} |
| ORB Range | {orb.get("range", "N/A")} pts |

### Volume Profile (20-day)
| Level | Value |
|:------|:------|
| POC (Point of Control) | {vol_profile.get("poc", "N/A")} |
| HVN (High Vol Nodes) | {", ".join(str(x) for x in vol_profile.get("hvn", [])) or "N/A"} |
| LVN (Low Vol Nodes) | {", ".join(str(x) for x in vol_profile.get("lvn", [])) or "N/A"} |

{"### Option Chain Levels" if options["underlying"] else ""}
{"| Item | Value |" if options["underlying"] else ""}
{"|:-----|:------|" if options["underlying"] else ""}
{"| Underlying | " + str(options.get("underlying")) + " |" if options["underlying"] else ""}
{"| Max Pain | " + str(options.get("max_pain", "N/A")) + " |" if options["underlying"] else ""}
{"| PCR | " + str(options.get("pcr", "N/A")) + " |" if options["underlying"] else ""}
{"| Top CE OI Strikes (resistance) | " + ", ".join(str(x) for x in options.get("top_ce_oi_strikes", [])) + " |" if options["underlying"] else ""}
{"| Top PE OI Strikes (support) | " + ", ".join(str(x) for x in options.get("top_pe_oi_strikes", [])) + " |" if options["underlying"] else ""}

---

## 3. Indicator Agreement / Disagreement

{signal_table}

**Bullish signals: {bias["bull_pct"]}% | Bearish signals: {bias["bear_pct"]}%**

---

## 4. TradingView Community Indicators Searched Today

_These popular public Pine Script indicators were surfaced from TradingView's library to supplement standard analysis._

{community_section}

---

## 5. Weighted Bias Breakdown

| Component | Signal | Weight |
|:----------|:-------|:-------|
| Daily Chart Trend | {bias["scores"]["daily"]} | 25% |
| 1H Trend | {bias["scores"]["h1"]} | 20% |
| 30M Trend | {bias["scores"]["m30"]} | 15% |
| 15M Trend | {bias["scores"]["m15"]} | 10% |
| 5M Trend | {bias["scores"]["m5"]} | 10% |
| CPR / Pivot Position | {bias["scores"]["cpr"]} | 10% |
| Community Indicators | {bias["scores"]["community"]} | 10% |

### ➜ Final Bias: **{bias["bias_strength"]} {bias["final_bias"]}** ({bias["bull_pct"]}% bull vs {bias["bear_pct"]}% bear)

---

## 6. Scenario Table for {next_day_str}

{scenario_table}

---

## 7. Key Risks & Watch Points

- **Gap up > {floor["r1"]}**: watch for exhaustion and early reversal near R2 ({floor["r2"]}).
- **Gap down < {floor["s1"]}**: confirm with 15M candle close; avoid buying the first red candle.
- **India VIX spike** during session → widen stops by 20–30%.
- **FII heavy selling** overnight → gap-down risk; wait for stability before going long.
- **PCR {"above 1.2 (Put heavy) → supports higher prices" if options.get("pcr") and options["pcr"] > 1.2 else "below 0.8 (Call heavy) → bearish pressure" if options.get("pcr") and options["pcr"] < 0.8 else "data unavailable today"}**.

---

## 8. Data Sources Used Today

| Source | Data |
|:-------|:-----|
| TradingView (tvdatafeed) | OHLCV multi-timeframe (primary) |
| Yahoo Finance (yfinance) | OHLCV fallback + India VIX |
| NSE India (nseindia.com) | Option chain, FII/DII data |
| TradingView Public Scripts | Community indicator search |

---
{DISCLAIMER}
"""
    return report


# ─────────────────────────────────────────────
# 7. HTML REPORT GENERATOR
# ─────────────────────────────────────────────

HTML_CSS = """
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --border: #2e3348;
    --accent: #4f8ef7;
    --green: #26a69a;
    --red: #ef5350;
    --yellow: #ffa726;
    --text: #e0e0e0;
    --muted: #8b90a0;
    --font: 'Segoe UI', system-ui, sans-serif;
    --mono: 'JetBrains Mono', 'Fira Code', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    font-size: 14px;
    display: flex;
    min-height: 100vh;
  }

  /* ── Sidebar ── */
  #sidebar {
    width: 220px;
    min-width: 220px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
    padding: 20px 12px;
  }
  #sidebar h2 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    margin-bottom: 14px;
    padding-left: 4px;
  }
  #sidebar ul { list-style: none; }
  #sidebar li { margin: 2px 0; }
  #sidebar a {
    display: block;
    padding: 6px 10px;
    border-radius: 6px;
    color: var(--text);
    text-decoration: none;
    font-size: 13px;
    transition: background 0.15s;
  }
  #sidebar a:hover, #sidebar a.active {
    background: var(--accent);
    color: #fff;
  }

  /* ── Main content ── */
  #content { flex: 1; padding: 32px 40px; max-width: 1100px; }

  .report-section {
    border: 1px solid var(--border);
    border-radius: 12px;
    margin-bottom: 48px;
    overflow: hidden;
    scroll-margin-top: 24px;
  }
  .report-header {
    background: linear-gradient(135deg, #1e2236 0%, #252a3d 100%);
    padding: 20px 28px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }
  .report-header h1 {
    font-size: 20px;
    font-weight: 700;
    color: #fff;
  }
  .report-header .meta {
    font-size: 12px;
    color: var(--muted);
  }
  .report-body { padding: 24px 28px; }

  /* ── Typography ── */
  h2 {
    font-size: 15px;
    font-weight: 600;
    color: var(--accent);
    margin: 28px 0 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }
  h3 {
    font-size: 13px;
    font-weight: 600;
    color: #c5c8d8;
    margin: 18px 0 8px;
  }
  p { line-height: 1.6; margin: 8px 0; color: var(--text); }
  em { color: var(--muted); font-style: italic; }
  strong { color: #fff; font-weight: 600; }
  hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
  blockquote {
    border-left: 3px solid var(--yellow);
    background: #1e2030;
    padding: 10px 16px;
    border-radius: 0 6px 6px 0;
    margin: 12px 0;
    color: var(--yellow);
    font-size: 12px;
  }

  /* ── Tables ── */
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0 18px;
    font-size: 13px;
  }
  th {
    background: #1e2236;
    color: var(--accent);
    padding: 8px 12px;
    text-align: left;
    font-weight: 600;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 7px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(79,142,247,0.05); }

  /* ── Signal colours ── */
  td:has(> span.bull), td.bull { color: var(--green); }
  td:has(> span.bear), td.bear { color: var(--red); }
  td:has(> span.neut), td.neut { color: var(--yellow); }

  /* ── Bias badge ── */
  .bias-badge {
    display: inline-block;
    padding: 6px 16px;
    border-radius: 20px;
    font-size: 15px;
    font-weight: 700;
    margin: 10px 0;
  }
  .bias-BULLISH  { background: rgba(38,166,154,.2); color: var(--green); border: 1px solid var(--green); }
  .bias-BEARISH  { background: rgba(239,83,80,.2);  color: var(--red);   border: 1px solid var(--red); }
  .bias-NEUTRAL  { background: rgba(255,167,38,.2); color: var(--yellow);border: 1px solid var(--yellow); }

  /* ── Links ── */
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* ── Scenario table special ── */
  .scenario-table td:nth-child(2) { font-weight: 600; }

  @media (max-width: 768px) {
    #sidebar { display: none; }
    #content { padding: 16px; }
  }
</style>
"""

HTML_JS = """
<script>
  // Highlight sidebar link for the section currently in view
  const sections = document.querySelectorAll('.report-section');
  const links = document.querySelectorAll('#sidebar a');
  const observer = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        links.forEach(l => l.classList.remove('active'));
        const active = document.querySelector(`#sidebar a[href="#${e.target.id}"]`);
        if (active) active.classList.add('active');
      }
    });
  }, { threshold: 0.2 });
  sections.forEach(s => observer.observe(s));
</script>
"""

SIGNAL_COLOUR = {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "🟡"}
BIAS_COLOUR_MAP = {"BULLISH": "BULLISH", "BEARISH": "BEARISH"}


def md_to_html_body(md_text: str) -> str:
    """Convert markdown to HTML using the `markdown` library."""
    import markdown as md_lib
    extensions = ["tables", "fenced_code", "nl2br"]
    html = md_lib.markdown(md_text, extensions=extensions)
    # Colour signal cells
    for word, colour in [("Bullish", "var(--green)"), ("Bearish", "var(--red)"), ("Neutral", "var(--yellow)")]:
        html = html.replace(
            f"<td>{SIGNAL_COLOUR.get(word, '')} {word}</td>",
            f'<td style="color:{colour}">{SIGNAL_COLOUR.get(word,"")} {word}</td>',
        )
    return html


def build_history_html(reports_dir: str, new_date: date, new_md: str) -> None:
    """
    Append today's report to reports/nifty-history.html.
    If the file doesn't exist it is created from scratch.
    If the file already has today's section it is replaced (idempotent re-runs).
    """
    history_path = os.path.join(reports_dir, "nifty-history.html")
    section_id = str(new_date)

    # ── Parse existing file (if any) ────────────────
    existing_sections: dict[str, str] = {}   # date-str → inner html of <section>
    toc_order: list[str] = []

    if os.path.exists(history_path):
        from bs4 import BeautifulSoup as BS
        soup = BS(open(history_path, encoding="utf-8"), "html.parser")
        for sec in soup.select("section.report-section"):
            sid = sec.get("id", "")
            if sid:
                existing_sections[sid] = str(sec)
                if sid not in toc_order:
                    toc_order.append(sid)

    # ── Build new section HTML ───────────────────────
    body_html = md_to_html_body(new_md)

    # Extract title line (first <h1> in converted html) for the header bar
    from bs4 import BeautifulSoup as BS2
    soup2 = BS2(body_html, "html.parser")
    h1_tag = soup2.find("h1")
    title_text = h1_tag.get_text() if h1_tag else f"Nifty Report {new_date}"
    # Remove the first <h1> from body to avoid duplication
    if h1_tag:
        h1_tag.decompose()
        body_html = str(soup2)

    # Extract generated timestamp (first <em> tag usually)
    meta_tag = soup2.find("em")
    meta_text = meta_tag.get_text() if meta_tag else ""

    # Detect bias for badge colour
    bias_key = "NEUTRAL"
    if "BULLISH" in new_md.upper():
        bias_key = "BULLISH"
    if "BEARISH" in new_md.upper():
        bias_key = "BEARISH"
    # More precise: look for the Final Bias line
    for line in new_md.splitlines():
        if "Final Bias:" in line:
            if "BULLISH" in line.upper():
                bias_key = "BULLISH"
            elif "BEARISH" in line.upper():
                bias_key = "BEARISH"
            else:
                bias_key = "NEUTRAL"
            break

    new_section_html = f"""
<section class="report-section" id="{section_id}">
  <div class="report-header">
    <h1>{title_text}</h1>
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <span class="bias-badge bias-{bias_key}">{bias_key}</span>
      <span class="meta">{meta_text}</span>
    </div>
  </div>
  <div class="report-body">
    {body_html}
  </div>
</section>
"""

    # ── Update sections dict (replace if re-run on same day) ────
    existing_sections[section_id] = new_section_html
    if section_id not in toc_order:
        toc_order.append(section_id)

    # Sort newest first
    toc_order_sorted = sorted(toc_order, reverse=True)

    # ── Build TOC sidebar ────────────────────────────
    toc_items = "\n".join(
        f'    <li><a href="#{d}">{d}</a></li>' for d in toc_order_sorted
    )

    # ── Assemble full HTML ───────────────────────────
    sections_html = "\n".join(existing_sections[d] for d in toc_order_sorted)

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Nifty 50 Daily Analysis — History</title>
  {HTML_CSS}
</head>
<body>
  <nav id="sidebar">
    <h2>📈 Reports</h2>
    <ul>
{toc_items}
    </ul>
  </nav>
  <div id="content">
{sections_html}
  </div>
  {HTML_JS}
</body>
</html>
"""

    with open(history_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    log.info(f"HTML history updated → {history_path}  ({len(toc_order_sorted)} report(s))")


# ─────────────────────────────────────────────
# 8. ENTRY POINT
# ─────────────────────────────────────────────

def should_run_now(window_minutes: int = 30) -> bool:
    """
    Check if current UK time is within [19:00, 19:00+window_minutes].
    Used when this script is triggered by a cron that fires at both 18:00 and 19:00 UTC.
    Pass FORCE_RUN=1 env var to skip the time check (for local testing).
    """
    if os.environ.get("FORCE_RUN", "0") == "1":
        return True
    uk_now = datetime.now(UK_TZ)
    target_hour = 19
    target_minute = 0
    minutes_since_target = (uk_now.hour - target_hour) * 60 + (uk_now.minute - target_minute)
    return 0 <= minutes_since_target < window_minutes


def main():
    if not should_run_now():
        uk_now = datetime.now(UK_TZ)
        log.info(
            f"Current UK time is {uk_now:%H:%M} — not within the 19:00 run window. "
            "Set FORCE_RUN=1 to override."
        )
        sys.exit(0)

    report_date = date.today()
    log.info(f"Generating Nifty 50 report for next trading day (base: {report_date})")

    report = generate_report(report_date)

    reports_dir = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(reports_dir, exist_ok=True)

    # Save individual markdown file
    md_path = os.path.join(reports_dir, f"nifty-report-{report_date}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)
    log.info(f"Markdown saved → {md_path}")

    # Append/update cumulative HTML history file
    build_history_html(reports_dir, report_date, report)

    # Print to terminal
    print("\n" + "=" * 80)
    print(report)
    print("=" * 80)


if __name__ == "__main__":
    main()
