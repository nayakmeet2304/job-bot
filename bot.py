"""
Amazon UK Warehouse Job Alert Bot
Authenticates with jobsatamazon.co.uk and polls the GraphQL API
for new warehouse jobs near Leicester.
"""

import os
import json
import time
import logging
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Your jobsatamazon.co.uk login credentials
AMAZON_PHONE = os.environ.get("AMAZON_PHONE", "")   # e.g. +447352697050
AMAZON_PIN   = os.environ.get("AMAZON_PIN",   "")   # your static PIN

# Leicester coordinates
CENTRE_LAT = 52.6369
CENTRE_LON = -1.1398
MAX_MILES  = 30  # search radius in miles

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))  # seconds
SEEN_FILE     = "seen_jobs.json"

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── AUTHENTICATION ──────────────────────────────────────────────────────────

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def _step1_request_sign_in(session: requests.Session) -> str:
    """
    POST to /sign-in with phone number.
    Amazon returns a short-lived KMS-signed JWT that acts as a CSRF token
    for the PIN verification step.
    """
    url = "https://auth.hiring.amazon.com/api/authentication/sign-in"
    headers = {
        **BASE_HEADERS,
        "Origin":  "https://auth.hiring.amazon.com",
        "Referer": "https://auth.hiring.amazon.com/",
    }
    resp = session.post(
        url,
        json={"user": AMAZON_PHONE, "countryCode": "UK"},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = (
        data.get("token")
        or data.get("csrfToken")
        or data.get("sessionToken")
        or data.get("authToken")
    )
    if not token:
        raise RuntimeError(f"No token in sign-in response: {data}")
    log.info("Step 1 OK — got sign-in token.")
    return token


def _step2_verify_pin(session: requests.Session, csrf_token: str) -> str:
    """
    POST to /verify-sign-in with phone + PIN + CSRF token.
    Returns the Bearer token (HVH_ACCESS_TOKEN).
    """
    url = "https://auth.hiring.amazon.com/api/authentication/verify-sign-in?countryCode=UK"
    headers = {
        **BASE_HEADERS,
        "Origin":     "https://auth.hiring.amazon.com",
        "Referer":    "https://auth.hiring.amazon.com/",
        "CSRF-Token": csrf_token,
    }
    resp = session.post(
        url,
        json={
            "user":        AMAZON_PHONE,
            "pin":         AMAZON_PIN,
            "token":       csrf_token,
            "countryName": "United Kingdom",
        },
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    log.debug(f"verify-sign-in response: {data}")

    # Bearer token may be in the response body or in the Set-Cookie header
    bearer = (
        data.get("accessToken")
        or data.get("token")
        or data.get("sessionToken")
        or data.get("authToken")
    )
    if not bearer:
        # Fall back to checking cookies (HVH_ACCESS_TOKEN cookie)
        raw = session.cookies.get("HVH_ACCESS_TOKEN")
        if raw:
            from urllib.parse import unquote
            bearer = unquote(raw)

    if not bearer:
        raise RuntimeError(
            f"Could not extract Bearer token from verify-sign-in.\n"
            f"Response body: {data}\n"
            f"Cookies: {dict(session.cookies)}"
        )

    log.info("Step 2 OK — authenticated.")
    return bearer


def login() -> tuple[requests.Session, str]:
    """Full login flow. Returns (session, bearer_token)."""
    session = requests.Session()
    # Visit the auth page first so the WAF / session cookies are set
    try:
        session.get(
            "https://auth.hiring.amazon.com/",
            headers={**BASE_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=10,
        )
    except Exception:
        pass  # best-effort; carry on

    log.info("Logging in to jobsatamazon.co.uk...")
    csrf_token = _step1_request_sign_in(session)
    bearer     = _step2_verify_pin(session, csrf_token)
    return session, bearer

# ─── GRAPHQL JOB SEARCH ──────────────────────────────────────────────────────

GRAPHQL_URL = "https://www.jobsatamazon.co.uk/graphql"

# Exact query the site uses
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
        **BASE_HEADERS,
        "Authorization": f"Bearer {bearer}",
        "country":       "United Kingdom",
        "iscanary":      "false",
        "Origin":        "https://www.jobsatamazon.co.uk",
        "Referer":       "https://www.jobsatamazon.co.uk/app",
        "Sec-Fetch-Site": "same-origin",
    }
    payload = {
        "operationName": "searchJobCardsByLocation",
        "variables": {
            "searchJobRequest": {
                "locale":   "en-GB",
                "country":  "United Kingdom",
                "keyWords": "",
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
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent.")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def send_combined_alert(jobs: list):
    header = f"🆕 <b>{len(jobs)} new Amazon warehouse job(s) near Leicester!</b>\n"
    separator = "\n" + "─" * 30 + "\n"
    blocks = [format_job(j) for j in jobs]

    messages = []
    current = header
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
    bearer: str,
    seen: set,
) -> tuple[set, requests.Session, str]:
    log.info("Checking for new jobs...")
    try:
        jobs = fetch_jobs(session, bearer)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            log.warning("Session expired — re-logging in...")
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
