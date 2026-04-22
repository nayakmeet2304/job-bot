# Amazon UK Warehouse Job Alert Bot

Polls Amazon Jobs@Amazon UK site and sends Telegram alerts for new warehouse jobs within 150 miles of Leicester.

## Current Status

**Testing Branch**: `railway-testing` is deployed on Railway

### Recent Fixes (2026-04-22)

1. **Amazon API**: All REST endpoints return 403 Forbidden. Bot now falls back to **HTML page scraping** with proper User-Agent headers.
2. **Telegram**: Fixed error logging to show exact failure reason (e.g., "chat not found").
3. **Errors**: Improved startup warnings to clearly indicate missing credentials.

## Setup

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export TELEGRAM_TOKEN="your_bot_token_here"
export TELEGRAM_CHAT_ID="your_chat_id_here"
export POLL_INTERVAL="300"  # seconds, default 300 = 5 mins

# Run bot
python bot.py
```

### Railway Deployment

1. Connect your GitHub repo to Railway
2. Set the deploy branch to `railway-testing` for testing
3. Go to **Service Settings** → **Variables**
4. Add/update these environment variables:

| Variable | Value | Notes |
|----------|-------|-------|
| `TELEGRAM_TOKEN` | `123456:ABC-DEF...` | From BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | `987654321` | Your Telegram user/group chat ID (numeric) |
| `POLL_INTERVAL` | `300` | Check interval in seconds (optional, default 300) |

5. **Redeploy** the service

## Troubleshooting

### Telegram "chat not found" (400 error)

**Cause**: The `TELEGRAM_CHAT_ID` is wrong or the bot was removed from that chat.

**Fix**:
1. Open Telegram and find your chat ID:
   - Message your bot (created via BotFather)
   - Paste this in browser: `https://api.telegram.org/botYOUR_TOKEN/getUpdates`
   - Look for `"chat":{"id":YOUR_CHAT_ID}`
2. Update `TELEGRAM_CHAT_ID` on Railway
3. Redeploy

### No jobs being fetched (403 Forbidden from Amazon)

**Current workaround**: Bot uses HTML scraping as fallback. If this doesn't work, the Amazon site may have changed its HTML structure.

**Next steps**: 
- Check latest logs for error messages
- Alternative: Use a headless browser (Selenium/Playwright) to render the page

### Not receiving Telegram messages

1. Verify your bot is still in the chat (add it again if needed)
2. Check Railway logs for Telegram send errors
3. Confirm `TELEGRAM_TOKEN` is correct (ask BotFather if unsure)

## Files

- `bot.py` - Main bot logic
- `requirements.txt` - Python dependencies
- `railway.json` - Railway deployment config
- `.gitignore` - Excludes `seen_jobs.json` (runtime state file)

## Logs

- Railway logs show all activity
- Check for `[ERROR]` messages first
- `[WARNING]` messages indicate fallbacks being used (expected sometimes)

## Next Steps

- Once `railway-testing` works, merge to `main` branch
- Set Railway to deploy from `main` for production
