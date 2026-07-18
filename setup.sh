#!/bin/bash

echo "==========================================="
echo "        ZetBot AI Setup Checker"
echo "==========================================="

# ------------------------------------------------------------------
# Create .env if missing
# ------------------------------------------------------------------
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "✅ .env created from .env.example"
    echo ""
    echo "⚠️ Edit .env before running the bot."
    exit 1
fi

echo "✅ .env found"

# ------------------------------------------------------------------
# Check Telegram Enabled
# ------------------------------------------------------------------
telegram_enabled=$(grep "^TELEGRAM_ENABLED=" .env | cut -d '=' -f2)

if [ "$telegram_enabled" != "true" ]; then
    echo ""
    echo "❌ TELEGRAM_ENABLED is not true"
    echo "Please set:"
    echo "TELEGRAM_ENABLED=true"
    exit 1
fi

# ------------------------------------------------------------------
# Check Telegram Token
# ------------------------------------------------------------------
telegram_token=$(grep "^TELEGRAM_TOKEN=" .env | cut -d '=' -f2)

if [ -z "$telegram_token" ] || [ "$telegram_token" = "YOUR_TELEGRAM_BOT_TOKEN" ]; then
    echo ""
    echo "❌ Telegram Token not configured."
    exit 1
fi

# ------------------------------------------------------------------
# Check Telegram Chat ID
# ------------------------------------------------------------------
telegram_chat=$(grep "^TELEGRAM_CHAT_ID=" .env | cut -d '=' -f2)

if [ -z "$telegram_chat" ] || [ "$telegram_chat" = "YOUR_CHAT_ID" ]; then
    echo ""
    echo "❌ Telegram Chat ID not configured."
    exit 1
fi

echo ""
echo "==========================================="
echo "✅ Environment looks good!"
echo "Ready to start ZetBot AI."
echo "==========================================="
