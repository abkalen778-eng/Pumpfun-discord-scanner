import asyncio
import logging
import os
import time

import discord

import bot as core
from plus500_markets import MARKETS, analyze_all, analyze_market, make_market_embed, make_rankings_embed

log = logging.getLogger("plus500-market-scanner")

MARKET_ALERT_SCORE = int(os.getenv("MARKET_ALERT_SCORE", "78"))
MARKET_CHECK_SECONDS = int(os.getenv("MARKET_CHECK_SECONDS", "300"))
MARKET_ALERT_COOLDOWN_MINUTES = int(os.getenv("MARKET_ALERT_COOLDOWN_MINUTES", "60"))

last_market_alert: dict[str, tuple[float, str]] = {}


@core.bot.tree.command(name="markets", description="Rank current Plus500-style market movement signals.")
async def markets(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        signals, errors = await analyze_all()
        if not signals:
            await interaction.followup.send("Market data is unavailable right now.", ephemeral=True)
            return
        await interaction.followup.send(embed=make_rankings_embed(signals, errors))
    except Exception as exc:
        log.exception("Multi-market command failed")
        await interaction.followup.send(f"Could not analyze markets right now: `{exc}`", ephemeral=True)


@core.bot.tree.command(name="market", description="Analyze one market: SP500, NASDAQ, GOLD, WTI, EURUSD, or BTC.")
async def market(interaction: discord.Interaction, name: str):
    await interaction.response.defer(thinking=True)
    key = name.strip().upper().replace(" ", "")
    aliases = {
        "S&P500": "SP500", "S&P": "SP500", "SPX": "SP500", "ES": "SP500",
        "NASDAQ100": "NASDAQ", "NAS100": "NASDAQ", "NQ": "NASDAQ",
        "XAUUSD": "GOLD", "XAU": "GOLD",
        "OIL": "WTI", "CRUDE": "WTI", "CL": "WTI",
        "EUR/USD": "EURUSD", "EUR": "EURUSD",
        "BITCOIN": "BTC", "BTCUSD": "BTC",
    }
    key = aliases.get(key, key)
    if key not in MARKETS:
        await interaction.followup.send(
            "Choose one of: `SP500`, `NASDAQ`, `GOLD`, `WTI`, `EURUSD`, or `BTC`.",
            ephemeral=True,
        )
        return
    try:
        signal = await analyze_market(key)
        await interaction.followup.send(embed=make_market_embed(signal))
    except Exception as exc:
        log.exception("Single-market command failed for %s", key)
        await interaction.followup.send(f"Could not analyze {key} right now: `{exc}`", ephemeral=True)


@core.bot.tree.command(name="marketsettings", description="Show multi-market scanner settings.")
async def marketsettings(interaction: discord.Interaction):
    names = ", ".join(MARKETS.keys())
    msg = (
        "**Plus500-style market scanner**\n"
        f"Markets: `{names}`\n"
        f"Automatic checks: every `{max(60, MARKET_CHECK_SECONDS) // 60} minutes`\n"
        f"Bullish alert score: `≥ {MARKET_ALERT_SCORE}`\n"
        f"Bearish alert score: `≤ {100 - MARKET_ALERT_SCORE}`\n"
        f"Same-market/direction cooldown: `{MARKET_ALERT_COOLDOWN_MINUTES} minutes`\n\n"
        "This scanner uses public market-price proxies and is not directly connected to your Plus500 account."
    )
    await interaction.response.send_message(msg, ephemeral=True)


async def maybe_send_market_alerts():
    signals, _ = await analyze_all()
    channel = core.bot.get_channel(core.ALERT_CHANNEL_ID)
    if not channel:
        return
    now = time.time()
    cooldown = MARKET_ALERT_COOLDOWN_MINUTES * 60
    for signal in signals:
        strong_bull = signal.score >= MARKET_ALERT_SCORE
        strong_bear = signal.score <= (100 - MARKET_ALERT_SCORE)
        if not (strong_bull or strong_bear):
            continue
        prev_time, prev_direction = last_market_alert.get(signal.key, (0.0, ""))
        if prev_direction == signal.direction and now - prev_time < cooldown:
            continue
        await channel.send(embed=make_market_embed(signal))
        last_market_alert[signal.key] = (now, signal.direction)
        log.info("Sent market alert: %s %s score=%s", signal.key, signal.direction, signal.score)


async def market_monitor():
    await core.bot.wait_until_ready()
    while not core.bot.is_closed():
        try:
            await maybe_send_market_alerts()
        except Exception:
            log.exception("Multi-market monitor failed")
        await asyncio.sleep(max(60, MARKET_CHECK_SECONDS))


async def run():
    asyncio.create_task(market_monitor())
    await core.main()


if __name__ == "__main__":
    asyncio.run(run())
