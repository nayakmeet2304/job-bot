"""
Amazon UK Warehouse Job Alert Bot
Uses a headless browser (Playwright) to log in to jobsatamazon.co.uk,
then polls the GraphQL API for new warehouse jobs near Leicester.
"""

import os
import json
import time
import logging
import requests
from urllib.parse import unquote

# ─── CONFIG ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

AMAZON_PHONE = os.environ.get("AMAZON_PHONE", "")  # e.g. +447352697050
AMAZON_PIN   = os.environ.get("AMAZON_PIN",   "")  # your static PIN

# Leicester coordinates
CENTRE_LAT = 52.6369
CENTRE_LON = -1.1398
MAX_MILES  = 30

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))  # seconds
SEEN_FILE     = "seen_jobs.json"

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── BROWSER LOGIN ───────────────────────────────────────────────────────────

def login() -> tuple[requests.Session, str]:
    """
    Open a visible browser window so the user can pass the AWS WAF
    'Let's confirm you are human' check, then auto-fill phone + PIN.
    Returns (requests_session_with_cookies, bearer_token).
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log.info("Opening browser window for login — a Chrome window will appear.")
    print("\n" + "="*60)
    print("A browser window will open.")
    print("1. Click  'Begin'  on the security check page.")
    print("2. The bot will then fill in your phone and PIN automatically.")
    print("="*60 + "\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
            no_viewport=True,
        )
        page = ctx.new_page()

        # ── Step 0: hit main site so WAF cookies are set ─────────────────
        page.goto("https://www.jobsatamazon.co.uk/", wait_until="domcontentloaded", timeout=30_000)

        # ── Step 1: navigate to login ─────────────────────────────────────
        page.goto(
            "https://www.jobsatamazon.co.uk/app#/login",
            wait_until="domcontentloaded",
            timeout=30_000,
        )

        # ── Step 2: handle WAF "confirm you are human" page ───────────────
        # Wait up to 60s for either the WAF page or the real login form
        phone_sel  = 'input[type="tel"], input[name="username"], input[placeholder*="phone" i], input[placeholder*="mobile" i]'
        waf_sel    = 'button:has-text("Begin")'
        login_or_waf = f'{phone_sel}, {waf_sel}'

        page.wait_for_selector(login_or_waf, timeout=60_000)

        # If the WAF challenge appeared, wait for the user to click Begin
        if page.locator(waf_sel).count() > 0:
            log.info("WAF security check detected — please click 'Begin' in the browser.")
            # Wait until the WAF page is gone and the login form appears
            page.wait_for_selector(phone_sel, timeout=120_000)

        # ── Step 3: enter phone number ────────────────────────────────────
        log.info("Login form detected — filling in phone number...")
        page.locator(phone_sel).first.fill(AMAZON_PHONE)

        continue_sel = 'button[type="submit"], button:has-text("Continue"), button:has-text("Next"), button:has-text("Sign in")'
        page.locator(continue_sel).first.click()

        # ── Step 4: enter PIN ─────────────────────────────────────────────
        pin_sel = 'input[type="password"], input[type="number"], input[name="pin"], input[placeholder*="pin" i], input[placeholder*="passcode" i]'
        page.wait_for_selector(pin_sel, timeout=20_000)
        log.info("PIN screen detected — entering PIN...")
        page.locator(pin_sel).first.fill(AMAZON_PIN)

        verify_sel = 'button[type="submit"], button:has-text("Continue"), button:has-text("Verify"), button:has-text("Sign in"), button:has-text("Confirm")'
        page.locator(verify_sel).first.click()

        # ── Step 5: wait until back on main site ─────────────────────────
        try:
            page.wait_for_url("**/jobsatamazon.co.uk/**", timeout=30_000)
        except PWTimeout:
            page.wait_for_timeout(5_000)

        # ── Step 6: extract cookies ───────────────────────────────────────
        all_cookies = ctx.cookies()
        browser.close()

    hvh = next((c for c in all_cookies if c["name"] == "HVH_ACCESS_TOKEN"), None)
    if not hvh:
        cookie_names = [c["name"] for c in all_cookies]
        raise RuntimeError(
            f"Login failed — HVH_ACCESS_TOKEN not found in cookies.\n"
            f"Cookies present: {cookie_names}\n"
            "Check AMAZON_PHONE and AMAZON_PIN are correct."
        )

    bearer = unquote(hvh["value"])

    # Build a requests.Session pre-loaded with the browser cookies
    session = requests.Session()
    for c in all_cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    log.info("Login successful — Bearer token obtained.")
    return session, bearer

# ─── GRAPHQL JOB SEARCH ──────────────────────────────────────────────────────

GRAPHQL_URL  = "https://www.jobsatamazon.co.uk/graphql"
SEARCH_QUERY = (
    "query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {\n"
    "  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {\n"
    "    nextToken\n"
    "    jobCards {\n"
    "      jobId\n"
    "      jobTitle\n"
    "      jobType\n"
    "      employmentType\n"
    "      city\n"
    "      state\n"
    "      postalCode\n"
    "      locationName\n"
    "      totalPayRateMin\n"
    "      totalPayRateMax\n"
    "      totalPayRateMinL10N\n"
    "      totalPayRateMaxL10N\n"
    "      tagLine\n"
    "      distance\n"
    "      distanceL10N\n"
    "      scheduleCount\n"
    "      currencyCode\n"
    "      bonusPay\n"
    "      bonusPayL10N\n"
    "      bonusJob\n"
    "      featuredJob\n"
    "      jobTypeL10N\n"
    "      employmentTypeL10N\n"
    "      virtualLocation\n"
    "      jobLocationType\n"
    "    }\n"
    "    __typename\n"
    "  }\n"
    "}\n"
)


def fetch_jobs(session: requests.Session, bearer: str) -> list:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type":    "application/json",
        "Authorization":   f"Bearer {bearer}",
        "country":         "United Kingdom",
        "iscanary":        "false",
        "Origin":          "https://www.jobsatamazon.co.uk",
        "Referer":         "https://www.jobsatamazon.co.uk/app",
    }
    payload = {
        "operationName": "searchJobCardsByLocation",
        "variables": {
            "searchJobRequest": {
                "locale":         "en-GB",
                "country":        "United Kingdom",
                "keyWords":       "",
                "equalFilters":   [],
                "containFilters": [{"key": "isPrivateSchedule", "val": ["true", "false"]}],
                "rangeFilters":   [],
                "orFilters":      [],
                "dateFilters":    [],
                "sorters":        [],
                "pageSize":       100,
                "geoQueryClause": {
                    "lat":      CENTRE_LAT,
                    "lng":      CENTRE_LON,
                    "unit":     "mi",
                    "distance": MAX_MILES,
                },
                "consolidateSchedule": True,
            }
        },
        "query": SEARCH_QUERY,
    }
    resp = session.post(GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        log.warning(f"GraphQL errors: {data['errors']}")
    return (
        data.get("data", {})
            .get("searchJobCardsByLocation", {})
            .get("jobCards", [])
    )

# ─── SEEN JOBS ───────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def format_job(job: dict) -> str:
    lines = []

    loc_parts = [p for p in [
        job.get("locationName"),
        job.get("city"),
        job.get("state"),
        job.get("postalCode"),
    ] if p]
    lines.append(f"📍 {', '.join(loc_parts)}")

    lines.append(f"🏷️ {job.get('jobTitle') or 'Warehouse Operative'}")

    contract = " | ".join(filter(None, [job.get("jobTypeL10N"), job.get("employmentTypeL10N")]))
    if contract:
        lines.append(f"💼 {contract}")

    if job.get("tagLine"):
        lines.append(f"💬 {job['tagLine'][:100]}")

    pay_min = job.get("totalPayRateMinL10N") or job.get("totalPayRateMin")
    pay_max = job.get("totalPayRateMaxL10N") or job.get("totalPayRateMax")
    if pay_min and pay_max and str(pay_min) != str(pay_max):
        lines.append(f"💰 {pay_min} – {pay_max} /hr")
    elif pay_min:
        lines.append(f"💰 {pay_min} /hr")

    if job.get("bonusPay"):
        lines.append(f"🎁 Bonus: {job.get('bonusPayL10N') or job['bonusPay']}")

    dist = job.get("distanceL10N") or job.get("distance")
    if dist:
        lines.append(f"📏 {dist} away")

    if job.get("scheduleCount"):
        lines.append(f"📅 {job['scheduleCount']} schedule(s) available")

    job_id = job.get("jobId", "")
    lines.append(f"🔗 https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}")

    return "\n".join(lines)


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing to console:")
        print(text)
        print()
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent.")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def send_combined_alert(jobs: list):
    header    = f"🆕 <b>{len(jobs)} new Amazon warehouse job(s) near Leicester!</b>\n"
    separator = "\n" + "─" * 30 + "\n"
    blocks    = [format_job(j) for j in jobs]

    messages, current = [], header
    for i, block in enumerate(blocks):
        chunk = (separator if i > 0 else "\n") + block
        if len(current) + len(chunk) > 4000:
            messages.append(current)
            current = f"🆕 <b>Continued ({i+1}/{len(blocks)})</b>\n\n" + block
        else:
            current += chunk
    messages.append(current)

    for msg in messages:
        send_telegram(msg)
        time.sleep(0.3)

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────

def check_once(
    session: requests.Session,
    bearer:  str,
    seen:    set,
) -> tuple[set, requests.Session, str]:
    log.info("Checking for new jobs...")
    try:
        jobs = fetch_jobs(session, bearer)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            log.warning("Token expired — re-logging in...")
            session, bearer = login()
            jobs = fetch_jobs(session, bearer)
        else:
            raise

    log.info(f"Fetched {len(jobs)} job(s) from API.")

    new_jobs = [j for j in jobs if j.get("jobId") and j["jobId"] not in seen]
    for j in new_jobs:
        seen.add(j["jobId"])

    if new_jobs:
        log.info(f"Found {len(new_jobs)} new job(s)! Sending alert...")
        send_combined_alert(new_jobs)
    else:
        log.info("No new jobs found.")

    save_seen(seen)
    return seen, session, bearer


def main():
    log.info("Starting Amazon Job Alert Bot...")

    if not AMAZON_PHONE or not AMAZON_PIN:
        log.error(
            "AMAZON_PHONE and AMAZON_PIN must be set.\n"
            "  export AMAZON_PHONE='+447XXXXXXXXX'\n"
            "  export AMAZON_PIN='XXXXXX'"
        )
        return

    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — messages will print to console.")
    if not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set — messages will print to console.")

    seen = load_seen()
    log.info(f"Loaded {len(seen)} previously seen job(s).")

    session, bearer = login()

    send_telegram(
        f"🤖 <b>Amazon Job Alert Bot Started</b>\n\n"
        f"📍 Watching jobs within <b>{MAX_MILES} miles</b> of Leicester\n"
        f"🔄 Checking every <b>{POLL_INTERVAL // 60} min</b>\n"
        "📬 You'll be notified the moment new jobs appear! 🚀"
    )

    while True:
        try:
            seen, session, bearer = check_once(session, bearer, seen)
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")

        log.info(f"Sleeping {POLL_INTERVAL}s until next check...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
