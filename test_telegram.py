#!/usr/bin/env python3
"""
Debug Telegram bot setup issues
"""
import os
import sys
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

print("=" * 60)
print("TELEGRAM BOT SETUP DEBUGGER")
print("=" * 60)

# Check 1: Token exists
if not TOKEN:
    print("❌ TELEGRAM_TOKEN is not set!")
    print("   Action: Set TELEGRAM_TOKEN environment variable")
    sys.exit(1)

print(f"✓ Token found: {TOKEN[:20]}...")

# Check 2: Validate token format
if ":" not in TOKEN:
    print("❌ Token format looks wrong (should contain a colon)")
    print("   Format: 123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
    sys.exit(1)

print("✓ Token format looks correct")

# Check 3: Test bot connection
print("\nTesting bot connection...")
try:
    resp = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=10)
    resp.raise_for_status()
    bot_info = resp.json()
    
    if bot_info.get("ok"):
        bot_data = bot_info.get("result", {})
        bot_username = bot_data.get("username", "unknown")
        bot_id = bot_data.get("id", "unknown")
        print(f"✓ Bot is valid!")
        print(f"  Bot ID: {bot_id}")
        print(f"  Bot username: @{bot_username}")
    else:
        error = bot_info.get("description", "Unknown error")
        print(f"❌ Bot validation failed: {error}")
        sys.exit(1)
except Exception as e:
    print(f"❌ Connection failed: {e}")
    print("   Check: Is your TELEGRAM_TOKEN correct?")
    sys.exit(1)

# Check 4: Look for recent messages
print("\nLooking for messages to the bot...")
try:
    resp = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    
    if data.get("ok"):
        updates = data.get("result", [])
        if not updates:
            print("❌ No updates found!")
            print("   Action: Send a message to your bot in Telegram first")
            print(f"   Bot: @{bot_username}")
            sys.exit(1)
        
        print(f"✓ Found {len(updates)} update(s)")
        
        # Get the most recent message
        latest = updates[-1]
        message = latest.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "(no text)")
        
        print(f"\nMost recent message:")
        print(f"  Chat ID: {chat_id}")
        print(f"  Message: {text}")
        print(f"\n✓ Your TELEGRAM_CHAT_ID should be: {chat_id}")
        
    else:
        error = data.get("description", "Unknown error")
        print(f"❌ Update fetch failed: {error}")
        sys.exit(1)
        
except Exception as e:
    print(f"❌ Failed to fetch updates: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("NEXT STEPS:")
print("=" * 60)
print(f"1. Set TELEGRAM_CHAT_ID={chat_id} in Railway variables")
print(f"2. Redeploy the bot")
print(f"3. Check Railway logs for the startup message in Telegram")
print("=" * 60)
