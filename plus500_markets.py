import asyncio
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import aiohttp
import discord

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

MARKETS = {
    "SP500": ("S&P 500", "ES=F"),
    "NASDAQ": ("Nasdaq-100", "NQ=F"),
    "GOLD": ("Gold", "GC=F"),
    "WTI": ("WTI Oil", "CL=F"),
    "EURUSD": ("EUR/USD", "EURUSD=X"),
    "BTC": ("Bitcoin", "BTC-USD"),
}


@dataclass
class MarketSignal:
    key: str
    name: str
    symbol: str
    price: float
    score: int
    direction: str
    confidence: int
    rsi: float
    ema9: float
    ema21: float
    momentum: float
    change_30m: float
    change_2h: float
    volume_ratio: float
    reasons: tuple[str, ...]
    market_time: Optional[int] = None


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    value = values[0]
    for x in values[1:]:
        value = alpha * x + (1 - alpha) * value
    return value


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def pct_change(current: float, previous: float) -> float:
    return ((current / previous) - 1.0) * 100.0 if previous else 0.0


async def fetch_candles(symbol: str) -> tuple[list[float], list[float], Optional[int]]:
    params = {"interval": "5m", "range": "5d", "includePrePost": "true"}
    headers = {"User-Agent": "Mozilla/5.0 MultiMarketDiscordScanner/1.0"}
    url = YAHOO_CHART_URL.format(symbol=quote(symbol, safe=""))
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                raise RuntimeError(f"market data returned HTTP {response.status}")
            data = await response.json()

    result = (((data or {}).get("chart") or {}).get("result") or [])
    if not result:
        raise RuntimeError("market data is unavailable")
    r0 = result[0]
    timestamps = r0.get("timestamp") or []
    quote_data = (((r0.get("indicators") or {}).get("quote") or [{}])[0])
    closes_raw = quote_data.get("close") or []
    volumes_raw = quote_data.get("volume") or []

    closes, volumes, valid_times = [], [], []
    for ts, close, vol in zip(timestamps, closes_raw, volumes_raw):
        if close is None:
            continue
        closes.append(float(close))
        volumes.append(float(vol or 0))
        valid_times.append(int(ts))
    if len(closes) < 30:
        raise RuntimeError("not enough recent candles")
    return closes, volumes, valid_times[-1] if valid_times else None


async def analyze_market(key: str) -> MarketSignal:
    key = key.upper()
    if key not in MARKETS:
        raise ValueError(f"Unknown market {key}")
    name, symbol = MARKETS[key]
    closes, volumes, market_time = await fetch_candles(symbol)

    price = closes[-1]
    ema9_value = ema(closes[-60:], 9)
    ema21_value = ema(closes[-80:], 21)
    ema12_value = ema(closes[-80:], 12)
    ema26_value = ema(closes[-100:], 26)
    momentum = ema12_value - ema26_value
    rsi_value = rsi(closes, 14)
    change_30m = pct_change(price, closes[-7]) if len(closes) >= 7 else 0.0
    change_2h = pct_change(price, closes[-25]) if len(closes) >= 25 else 0.0

    recent_vols = [v for v in volumes[-21:-1] if v > 0]
    baseline_volume = sum(recent_vols) / len(recent_vols) if recent_vols else 0.0
    volume_ratio = volumes[-1] / baseline_volume if baseline_volume and volumes[-1] else 1.0

    score = 50
    reasons: list[str] = []
    if ema9_value > ema21_value:
        score += 16; reasons.append("9 EMA above 21 EMA")
    else:
        score -= 16; reasons.append("9 EMA below 21 EMA")
    if price > ema9_value:
        score += 8; reasons.append("price above short-term trend")
    else:
        score -= 8; reasons.append("price below short-term trend")
    if momentum > 0:
        score += 12; reasons.append("momentum positive")
    else:
        score -= 12; reasons.append("momentum negative")
    if 55 <= rsi_value <= 70:
        score += 10; reasons.append("RSI supports upside momentum")
    elif rsi_value > 70:
        score += 3; reasons.append("RSI bullish but overbought")
    elif 30 <= rsi_value <= 45:
        score -= 10; reasons.append("RSI supports downside momentum")
    elif rsi_value < 30:
        score -= 3; reasons.append("RSI bearish but oversold")

    # Scaled movement thresholds work reasonably across these liquid markets.
    if change_30m >= 0.30:
        score += 10; reasons.append("strong positive 30m move")
    elif change_30m >= 0.10:
        score += 5; reasons.append("positive 30m move")
    elif change_30m <= -0.30:
        score -= 10; reasons.append("strong negative 30m move")
    elif change_30m <= -0.10:
        score -= 5; reasons.append("negative 30m move")

    if change_2h >= 0.60:
        score += 10; reasons.append("2h trend strongly higher")
    elif change_2h >= 0.20:
        score += 5; reasons.append("2h trend higher")
    elif change_2h <= -0.60:
        score -= 10; reasons.append("2h trend strongly lower")
    elif change_2h <= -0.20:
        score -= 5; reasons.append("2h trend lower")

    if volume_ratio >= 1.5:
        if change_30m > 0:
            score += 7; reasons.append("volume confirms upside")
        elif change_30m < 0:
            score -= 7; reasons.append("volume confirms downside")

    score = max(0, min(100, score))
    direction = "BULLISH" if score >= 65 else "BEARISH" if score <= 35 else "NEUTRAL"
    confidence = min(100, abs(score - 50) * 2)
    return MarketSignal(
        key=key, name=name, symbol=symbol, price=price, score=score,
        direction=direction, confidence=confidence, rsi=rsi_value,
        ema9=ema9_value, ema21=ema21_value, momentum=momentum,
        change_30m=change_30m, change_2h=change_2h,
        volume_ratio=volume_ratio, reasons=tuple(reasons), market_time=market_time,
    )


async def analyze_all() -> tuple[list[MarketSignal], list[str]]:
    keys = list(MARKETS)
    results = await asyncio.gather(*(analyze_market(k) for k in keys), return_exceptions=True)
    signals: list[MarketSignal] = []
    errors: list[str] = []
    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            errors.append(f"{key}: {result}")
        else:
            signals.append(result)
    signals.sort(key=lambda s: abs(s.score - 50), reverse=True)
    return signals, errors


def make_market_embed(signal: MarketSignal) -> discord.Embed:
    arrow = "📈" if signal.direction == "BULLISH" else "📉" if signal.direction == "BEARISH" else "➡️"
    e = discord.Embed(
        title=f"{arrow} {signal.name} Market Signal",
        description=f"Proxy: `{signal.symbol}` • Analyzed price: **{signal.price:,.4f}**",
    )
    e.add_field(name="Bias", value=f"**{signal.direction}**", inline=True)
    e.add_field(name="Score", value=f"**{signal.score}/100**", inline=True)
    e.add_field(name="Confidence", value=f"{signal.confidence}%", inline=True)
    e.add_field(name="30m", value=f"{signal.change_30m:+.2f}%", inline=True)
    e.add_field(name="2h", value=f"{signal.change_2h:+.2f}%", inline=True)
    e.add_field(name="RSI", value=f"{signal.rsi:.1f}", inline=True)
    e.add_field(name="Why", value=" • ".join(signal.reasons[:6]), inline=False)
    e.set_footer(text="Movement-based research signal only — not a guarantee, order, or Plus500 account connection.")
    return e


def make_rankings_embed(signals: list[MarketSignal], errors: list[str]) -> discord.Embed:
    e = discord.Embed(
        title="📊 Plus500-Style Market Scanner",
        description="Ranks liquid markets by the strength of the current movement signal."
    )
    for i, s in enumerate(signals, 1):
        icon = "📈" if s.direction == "BULLISH" else "📉" if s.direction == "BEARISH" else "➡️"
        e.add_field(
            name=f"{i}. {icon} {s.name}",
            value=(f"**{s.direction}** • score **{s.score}/100** • confidence {s.confidence}%\n"
                   f"30m {s.change_30m:+.2f}% • 2h {s.change_2h:+.2f}% • RSI {s.rsi:.1f}"),
            inline=False,
        )
    if errors:
        e.add_field(name="Unavailable", value="\n".join(errors[:4]), inline=False)
    e.set_footer(text="Research signals only. Compare the exact Plus500 instrument/price before trading; no outcome is guaranteed.")
    return e
