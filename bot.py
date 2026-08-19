import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import websockets

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pump-scanner")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))
PUMPPORTAL_API_KEY = os.getenv("PUMPPORTAL_API_KEY", "")

MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MIN_5M_VOLUME_USD = float(os.getenv("MIN_5M_VOLUME_USD", "3000"))
MAX_TOP_HOLDER_PCT = float(os.getenv("MAX_TOP_HOLDER_PCT", "35"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
seen_alerts: dict[str, float] = {}


@dataclass
class Analysis:
    mint: str
    symbol: str = "?"
    name: str = "Unknown"
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_5m: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    price_change_5m: float = 0.0
    market_cap: float = 0.0
    top20_pct: Optional[float] = None
    score: int = 0
    confidence: str = "Low"
    pair_url: str = ""
    reasons: tuple[str, ...] = ()


async def get_json(session: aiohttp.ClientSession, url: str, **kwargs):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), **kwargs) as r:
        if r.status != 200:
            return None
        return await r.json()


async def dex_data(session: aiohttp.ClientSession, mint: str):
    data = await get_json(session, DEX_URL.format(mint=mint))
    if not data:
        return None
    pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs:
        return None
    # Prefer the deepest-liquidity pool, which tends to have the most reliable market data.
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


async def top_holder_concentration(session: aiohttp.ClientSession, mint: str) -> Optional[float]:
    largest_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [mint, {"commitment": "confirmed"}],
    }
    supply_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "getTokenSupply",
        "params": [mint, {"commitment": "confirmed"}],
    }

    async def rpc(payload):
        async with session.post(
            SOLANA_RPC,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return None
            return await r.json()

    try:
        largest, supply = await asyncio.gather(rpc(largest_payload), rpc(supply_payload))
        vals = (((largest or {}).get("result") or {}).get("value") or [])
        supply_value = float(((((supply or {}).get("result") or {}).get("value") or {}).get("uiAmount")) or 0)
        if not vals or supply_value <= 0:
            return None
        total_largest = sum(float(v.get("uiAmount") or 0) for v in vals[:20])
        return 100.0 * total_largest / supply_value
    except Exception:
        return None


def calculate_score(a: Analysis) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if a.liquidity_usd >= 50000:
        score += 25; reasons.append("strong liquidity")
    elif a.liquidity_usd >= 20000:
        score += 20; reasons.append("good liquidity")
    elif a.liquidity_usd >= MIN_LIQUIDITY_USD:
        score += 12; reasons.append("acceptable liquidity")
    else:
        reasons.append("thin liquidity")

    if a.volume_5m >= 50000:
        score += 25; reasons.append("very strong 5m volume")
    elif a.volume_5m >= 15000:
        score += 20; reasons.append("strong 5m volume")
    elif a.volume_5m >= MIN_5M_VOLUME_USD:
        score += 12; reasons.append("active 5m volume")
    else:
        reasons.append("low 5m volume")

    total_trades = a.buys_5m + a.sells_5m
    buy_ratio = a.buys_5m / total_trades if total_trades else 0.0
    if total_trades >= 20 and buy_ratio >= 0.65:
        score += 18; reasons.append("buyers dominate recent trades")
    elif total_trades >= 10 and buy_ratio >= 0.55:
        score += 10; reasons.append("positive recent buy pressure")
    elif total_trades:
        reasons.append("weak/mixed buy pressure")

    if 2 <= a.price_change_5m <= 25:
        score += 12; reasons.append("positive but not extreme 5m momentum")
    elif 25 < a.price_change_5m <= 60:
        score += 7; reasons.append("very fast 5m move")
    elif a.price_change_5m > 60:
        score -= 5; reasons.append("extreme 5m spike / chase risk")
    elif a.price_change_5m < -10:
        score -= 10; reasons.append("sharp 5m decline")

    if a.top20_pct is not None:
        if a.top20_pct <= 20:
            score += 20; reasons.append("lower top-account concentration")
        elif a.top20_pct <= MAX_TOP_HOLDER_PCT:
            score += 10; reasons.append("moderate top-account concentration")
        else:
            score -= 15; reasons.append("high top-account concentration")
    else:
        reasons.append("holder concentration unavailable")

    return max(0, min(100, score)), reasons


async def analyze_mint(mint: str) -> Analysis:
    async with aiohttp.ClientSession(headers={"User-Agent": "PumpFunDiscordScanner/1.0"}) as session:
        pair, top20 = await asyncio.gather(
            dex_data(session, mint),
            top_holder_concentration(session, mint),
        )

    a = Analysis(mint=mint, top20_pct=top20)
    if pair:
        a.symbol = ((pair.get("baseToken") or {}).get("symbol") or "?")[:20]
        a.name = ((pair.get("baseToken") or {}).get("name") or "Unknown")[:60]
        a.price_usd = float(pair.get("priceUsd") or 0)
        a.liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0)
        a.volume_5m = float((pair.get("volume") or {}).get("m5") or 0)
        tx = (pair.get("txns") or {}).get("m5") or {}
        a.buys_5m = int(tx.get("buys") or 0)
        a.sells_5m = int(tx.get("sells") or 0)
        a.price_change_5m = float((pair.get("priceChange") or {}).get("m5") or 0)
        a.market_cap = float(pair.get("marketCap") or pair.get("fdv") or 0)
        a.pair_url = pair.get("url") or ""

    a.score, why = calculate_score(a)
    data_points = sum([
        bool(pair),
        a.top20_pct is not None,
        a.volume_5m > 0,
        (a.buys_5m + a.sells_5m) > 0,
    ])
    a.confidence = "High" if data_points >= 4 else "Medium" if data_points >= 2 else "Low"
    a.reasons = tuple(why)
    return a


def fmt_money(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:,.2f}"


def make_embed(a: Analysis, title="Pump.fun Scanner Alert") -> discord.Embed:
    risk = "LOWER" if a.score >= 85 else "MEDIUM" if a.score >= 70 else "HIGH"
    e = discord.Embed(
        title=title,
        description=f"**{a.name} ({a.symbol})**\n`{a.mint}`",
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name="Score", value=f"**{a.score}/100**", inline=True)
    e.add_field(name="Data confidence", value=a.confidence, inline=True)
    e.add_field(name="Relative risk", value=risk, inline=True)
    e.add_field(name="Liquidity", value=fmt_money(a.liquidity_usd), inline=True)
    e.add_field(name="5m volume", value=fmt_money(a.volume_5m), inline=True)
    e.add_field(name="5m price", value=f"{a.price_change_5m:+.1f}%", inline=True)
    e.add_field(name="5m buys / sells", value=f"{a.buys_5m} / {a.sells_5m}", inline=True)
    e.add_field(
        name="Top 20 token accounts",
        value=f"{a.top20_pct:.1f}%" if a.top20_pct is not None else "Unavailable",
        inline=True,
    )
    e.add_field(name="Market cap / FDV", value=fmt_money(a.market_cap), inline=True)
    e.add_field(name="Why", value=" • ".join(a.reasons[:6]) or "Insufficient data", inline=False)
    if a.pair_url:
        e.add_field(name="Chart", value=f"[Open DEX Screener]({a.pair_url})", inline=False)
    e.set_footer(text="Research signal only — not a profit guarantee or automatic trade.")
    return e


async def maybe_alert(mint: str):
    now = time.time()
    last = seen_alerts.get(mint, 0)
    if now - last < ALERT_COOLDOWN_MINUTES * 60:
        return
    a = await analyze_mint(mint)
    if (
        a.score >= MIN_SCORE
        and a.liquidity_usd >= MIN_LIQUIDITY_USD
        and a.volume_5m >= MIN_5M_VOLUME_USD
    ):
        channel = bot.get_channel(ALERT_CHANNEL_ID)
        if channel:
            await channel.send(embed=make_embed(a))
            seen_alerts[mint] = now


async def pumpportal_listener():
    if not PUMPPORTAL_API_KEY:
        log.warning("No PUMPPORTAL_API_KEY set; realtime new-token scanning is disabled.")
        return

    url = f"wss://pumpportal.fun/api/data?api-key={PUMPPORTAL_API_KEY}"
    while not bot.is_closed():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                log.info("Subscribed to Pump.fun new token stream")
                async for message in ws:
                    try:
                        event = json.loads(message)
                        mint = event.get("mint")
                        if mint:
                            asyncio.create_task(delayed_analysis(mint))
                    except Exception as exc:
                        log.debug("Ignored websocket event: %s", exc)
        except Exception as exc:
            log.warning("PumpPortal websocket error: %s", exc)
            await asyncio.sleep(5)


async def delayed_analysis(mint: str):
    # Give market-data indexers a little time to see a brand-new token.
    await asyncio.sleep(20)
    try:
        await maybe_alert(mint)
    except Exception:
        log.exception("Analysis failed for %s", mint)


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    if DISCORD_GUILD_ID:
        guild = discord.Object(id=DISCORD_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("Synced %d commands to guild %s", len(synced), DISCORD_GUILD_ID)
    else:
        synced = await bot.tree.sync()
        log.info("Synced %d global commands", len(synced))


@bot.tree.command(name="ping", description="Check whether the Pump.fun scanner is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Scanner is online ✅", ephemeral=True)


@bot.tree.command(name="settings", description="Show the active scanner thresholds.")
async def settings(interaction: discord.Interaction):
    msg = (
        f"**Pump.fun scanner settings**\n"
        f"Minimum score: `{MIN_SCORE}`\n"
        f"Minimum liquidity: `{fmt_money(MIN_LIQUIDITY_USD)}`\n"
        f"Minimum 5m volume: `{fmt_money(MIN_5M_VOLUME_USD)}`\n"
        f"Maximum preferred top-account concentration: `{MAX_TOP_HOLDER_PCT:.0f}%`\n"
        f"Alert cooldown: `{ALERT_COOLDOWN_MINUTES} minutes`"
    )
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="testalert", description="Send a test Pump.fun scanner alert.")
async def testalert(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🚨 Pump.fun Scanner Test Alert",
        description="This is a test alert from your Pump Scanner."
    )

    embed.add_field(name="Token", value="TEST", inline=True)
    embed.add_field(name="Score", value="85/100", inline=True)
    embed.add_field(name="Liquidity", value="$25.0K", inline=True)
    embed.add_field(name="5m Volume", value="$12.0K", inline=True)
    embed.add_field(name="5m Price Change", value="+18.5%", inline=True)
    embed.add_field(name="Buy / Sell", value="42 / 19", inline=True)

    embed.set_footer(
        text="TEST ONLY — this is not a real token or trading recommendation."
    )

    await interaction.response.send_message(embed=embed)
@bot.tree.command(name="scan", description="Analyze a Solana token mint address.")
@app_commands.describe(mint="Solana SPL token mint address")
async def scan(interaction: discord.Interaction, mint: str):
    await interaction.response.defer(thinking=True)
    try:
        a = await analyze_mint(mint.strip())
        await interaction.followup.send(embed=make_embed(a, title="Manual Token Scan"))
    except Exception as exc:
        log.exception("Manual scan failed")
        await interaction.followup.send(f"Could not analyze that mint: `{exc}`", ephemeral=True)


async def main():
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing. Copy .env.example to .env and add your token.")
    async with bot:
        bot.loop.create_task(pumpportal_listener())
        await bot.start(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
