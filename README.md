# Pump.fun Discord Scanner

A Discord bot that watches new Pump.fun token launches, enriches them with DEX Screener + Solana RPC data, scores them, and posts high-scoring alerts into your own Discord server.

## What it does

- Watches Pump.fun new-token events through PumpPortal's realtime websocket.
- Looks up market data through DEX Screener.
- Checks the largest SPL token accounts using Solana RPC.
- Calculates a 0-100 research score.
- Posts alerts only when the token passes your filters.
- Adds slash commands:
  - `/scan <mint>` — analyze one Solana token address
  - `/settings` — show active scanner thresholds
  - `/ping` — check whether the bot is online

This version **does not buy or sell anything**.

## Setup

1. Install Python 3.11+.
2. Create a Discord application and bot in the Discord Developer Portal.
3. Invite it to your server with the `bot` and `applications.commands` scopes.
4. Give it permission to View Channels, Send Messages and Embed Links.
5. Copy `.env.example` to `.env`.
6. Put your Discord bot token, server ID and alert channel ID in `.env`.
7. Get a PumpPortal API key and add it as `PUMPPORTAL_API_KEY`.
8. Install dependencies:
   `pip install -r requirements.txt`
9. Start:
   `python bot.py`

## Scoring

The score uses available market signals:
- liquidity
- 5-minute volume
- 5-minute buys versus sells
- short-term price change
- market-cap / liquidity sanity checks
- concentration among the largest token accounts

The score is deliberately conservative. Missing data reduces confidence rather than being treated as a positive signal.

## Important

A high score is **not** a prediction or guarantee of profit. Pump.fun tokens can collapse very quickly and market data can be manipulated. Treat alerts as candidates for further research.
