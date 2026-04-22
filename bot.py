"""
Amazon UK Warehouse Job Alert Bot
Scrapes jobsatamazon.co.uk and sends Telegram notifications
for new jobs within ~150 miles of Leicester.
"""

import os
import json
import time
import math
import logging
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Leicester coordinates
CENTRE_LAT = 52.6369
CENTRE_LON = -1.1398
MAX_MILES = 150

# How often to check (seconds). 300 = every 5 mins
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))

# File to track jobs we've already seen
SEEN_FILE = "seen_jobs.json"

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def haversine_miles(lat1, lon1, lat2, lon2):
    """Calculate distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ─── AMAZON JOBS FETCHER ─────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": "https://www.jobsatamazon.co.uk",
    "Referer": "https://www.jobsatamazon.co.uk/",
}

# Job search endpoints to try. The legacy hiring.amazon.co.uk host no longer resolves.
SEARCH_ENDPOINTS = [
    "https://www.jobsatamazon.co.uk/api/search",
    "https://www.jobsatamazon.co.uk/api/v1/search",
    "https://hiring.amazon.co.uk/api/v1/search",  # legacy fallback
]

PAYLOAD = {
    "locale": "en-GB",
    "country": "GBR",
    "keyWords": "",
    "equalFilters": [],
    "containFilters": [{"key": "normalizedJobCode", "val": ["AMZL", "FC", "SC", "RS"]}],
    "rangeFilters": [],
    "orFilters": [],
    "pageSize": 100,
    "geoQueryClause": {
        "lat": CENTRE_LAT,
        "lng": CENTRE_LON,
        "unit": "mi",
        "distance": MAX_MILES,
    },
    "offset": 0,
    "total": True,
    "eventSource": "JOB_SEARCH_PAGE",
}


def fetch_jobs():
    """Try multiple endpoints and return list of job dicts."""
    for endpoint in SEARCH_ENDPOINTS:
        try:
            resp = requests.post(
                endpoint,
                json=PAYLOAD,
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            jobs = data.get("jobs", data.get("results", []))
            if isinstance(jobs, list):
                if endpoint != SEARCH_ENDPOINTS[0]:
                    log.info(f"Job fetch succeeded via fallback endpoint: {endpoint}")
                return jobs
        except requests.RequestException as e:
            log.warning(f"Endpoint failed ({endpoint}): {type(e).__name__}: {e}")
        except ValueError as e:
            log.warning(f"Invalid JSON from endpoint ({endpoint}): {e}")

    log.info("REST endpoints blocked; attempting HTML scrape...")
    return fetch_jobs_fallback()




def fetch_jobs_fallback():
    """Fallback: Attempt to parse job data from the Amazon jobs page.
    Note: The site uses client-side rendering (JavaScript SPA), making it 
    difficult to fetch jobs without a browser. This is a best-effort attempt.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("BeautifulSoup not available; cannot parse HTML")
        log.info(
            "The jobsatamazon.co.uk website uses client-side rendering and requires "
            "a JavaScript-capable browser to load jobs. Solutions:\n"
            "1) Use a headless browser like Selenium or Playwright\n"
            "2) Check if Amazon provides an official jobs feed/API\n"
            "3) Wait for the site's API to be publicly documented"
        )
        return []
    
    try:
        log.info("Attempting to fetch and parse Amazon jobs page...")
        
        # Try to fetch the main page
        resp = requests.get(
            "https://www.jobsatamazon.co.uk/",
            headers=HEADERS,
            timeout=30
        )
        resp.raise_for_status()
        
        # Parse HTML
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Look for job data in script tags (React apps often embed JSON there)
        scripts = soup.find_all("script")
        job_data = None
        
        for script in scripts:
            if script.string and ("jobs" in script.string or "jobId" in script.string):
                try:
                    # Try to extract JSON from common React patterns
                    content = script.string
                    # Look for JSON arrays or objects
                    import re
                    json_match = re.search(r'\{\s*"?jobs"?\s*:\s*\[', content)
                    if json_match:
                        start = content.rfind('{', 0, json_match.start())
                        # Find matching closing brace
                        depth = 0
                        for i in range(start, len(content)):
                            if content[i] == '{':
                                depth += 1
                            elif content[i] == '}':
                                depth -= 1
                                if depth == 0:
                                    json_str = content[start:i+1]
                                    job_data = json.loads(json_str)
                                    break
                        if job_data:
                            break
                except (json.JSONDecodeError, ValueError, IndexError):
                    continue
        
        if job_data and "jobs" in job_data:
            jobs = job_data["jobs"]
            if isinstance(jobs, list) and jobs:
                log.info(f"✓ Extracted {len(jobs)} jobs from page")
                return jobs
        
        log.warning("No job listings found in page HTML")
        return []
        
    except requests.RequestException as e:
        log.warning(f"Failed to fetch Amazon jobs page: {e}")
        return []
    except Exception as e:
        log.error(f"HTML parsing failed: {e}")
        return []


def parse_job(job: dict) -> dict | None:
    """Normalise a raw job dict into something we can work with."""
    try:
        job_id = (
            job.get("jobId")
            or job.get("id")
            or job.get("requisitionId")
            or ""
        )
        if not job_id:
            return None

        title = job.get("title") or job.get("jobTitle") or "Warehouse Operative"
        city = job.get("city") or job.get("locationName") or ""
        state = job.get("state") or job.get("region") or "England"
        postal = job.get("postalCode") or job.get("zipCode") or ""
        pay = (
            job.get("pay")
            or job.get("basePay")
            or job.get("hourlyPay")
            or ""
        )
        employment_type = job.get("employmentType") or job.get("jobType") or ""
        schedule_type = job.get("scheduleType") or ""
        description = job.get("jobDescription") or job.get("description") or "Pick, pack and ship parcels"
        first_day = job.get("firstDayOnSite") or job.get("startDate") or ""
        schedule = job.get("shiftCode") or job.get("schedule") or ""
        hours = job.get("hoursPerWeek") or ""
        openings = job.get("totalOpenings") or job.get("openings") or 1

        # Location coordinates for distance check
        lat = job.get("latitude") or job.get("lat")
        lon = job.get("longitude") or job.get("lng") or job.get("lon")
        if lat and lon:
            dist = haversine_miles(CENTRE_LAT, CENTRE_LON, float(lat), float(lon))
            if dist > MAX_MILES:
                return None  # outside range

        url = (
            job.get("applyUrl")
            or job.get("url")
            or f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}"
        )

        # Format pay nicely
        if pay and not str(pay).startswith("£"):
            try:
                pay = f"£{float(pay):.2f} /hr"
            except Exception:
                pass

        return {
            "job_id": job_id,
            "title": title,
            "city": city,
            "state": state,
            "postal": postal,
            "pay": pay,
            "employment_type": employment_type,
            "schedule_type": schedule_type,
            "description": description,
            "first_day": first_day,
            "schedule": schedule,
            "hours": hours,
            "openings": openings,
            "url": url,
        }
    except Exception as e:
        log.warning(f"Error parsing job: {e}")
        return None


# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def format_message(job: dict) -> str:
    """Format job dict into the Telegram message style you showed."""
    lines = []

    location_parts = [p for p in [job["city"], job["state"], job["postal"]] if p]
    lines.append(f"📍 {', '.join(location_parts)}")

    openings = job["openings"]
    lines.append(f"🏷️ {job['title']} | {openings}")

    contract_parts = [p for p in [job["schedule_type"], job["employment_type"]] if p]
    if contract_parts:
        lines.append(f"💼 {' | '.join(contract_parts)}")

    if job["description"]:
        desc = job["description"][:80].strip()
        lines.append(f"💬 {desc}")

    if job["pay"]:
        lines.append(f"💰 {job['pay']}")

    if job["first_day"]:
        lines.append(f"📅 First Day: {job['first_day']}")

    if job["schedule"]:
        lines.append(f"⏰ Schedule: {job['schedule']}")

    if job["hours"]:
        lines.append(f"🕐 Hours/Week: {job['hours']}")

    lines.append(f"🔗 {job['url']}")

    return "\n".join(lines)


def send_telegram(text: str):
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing to console instead:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent.")
    except Exception as e:
        body = ""
        status = "unknown"
        if hasattr(e, "response") and e.response is not None:
            status = e.response.status_code
            body = e.response.text
        log.error(f"Telegram send failed: {e} | status={status} | body={body}")


def send_startup_message():
    msg = (
        "🤖 <b>Amazon Job Alert Bot Started</b>\n\n"
        f"📍 Watching for jobs within <b>{MAX_MILES} miles</b> of Leicester\n"
        f"🔄 Checking every <b>{POLL_INTERVAL // 60} minutes</b>\n"
        "📬 All new jobs sent in <b>one combined message</b>\n\n"
        "Sit back — you'll be notified the moment new jobs appear! 🚀"
    )
    send_telegram(msg)


# ─── MAIN LOOP ───────────────────────────────────────────────────────────────

def check_once(seen: set) -> set:
    """Fetch jobs, filter new ones, send ONE combined alert. Returns updated seen set."""
    log.info("Checking for new jobs...")
    raw_jobs = fetch_jobs()
    log.info(f"Fetched {len(raw_jobs)} raw jobs from API.")

    new_jobs = []
    for raw in raw_jobs:
        job = parse_job(raw)
        if job and job["job_id"] not in seen:
            new_jobs.append(job)
            seen.add(job["job_id"])

    if new_jobs:
        log.info(f"Found {len(new_jobs)} new job(s)! Sending combined alert...")
        send_combined_alert(new_jobs)
    else:
        log.info("No new jobs found.")

    save_seen(seen)
    return seen


def send_combined_alert(jobs: list):
    """
    Send all new jobs in ONE Telegram message.
    Telegram has a 4096 char limit, so we split into chunks if needed.
    """
    header = f"🆕 <b>{len(jobs)} new Amazon warehouse job(s) near Leicester!</b>\n"
    separator = "\n" + "─" * 30 + "\n"

    # Build all job blocks
    blocks = [format_message(job) for job in jobs]

    # Pack as many as fit into one message (Telegram limit: 4096 chars)
    messages = []
    current = header
    for i, block in enumerate(blocks):
        addition = (separator if i > 0 else "\n") + block
        if len(current) + len(addition) > 4000:
            # Flush current message and start a new one
            messages.append(current)
            current = f"🆕 <b>Continued ({i+1}/{len(blocks)})</b>\n\n" + block
        else:
            current += addition
    messages.append(current)

    for msg in messages:
        send_telegram(msg)
        time.sleep(0.3)


def main():
    log.info("Starting Amazon Job Alert Bot...")

    if not TELEGRAM_TOKEN:
        log.error("❌ TELEGRAM_TOKEN not set — bot cannot send Telegram messages!")
        log.error("   Set TELEGRAM_TOKEN environment variable and restart.")
    if not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_CHAT_ID not set — bot cannot send Telegram messages!")
        log.error("   Set TELEGRAM_CHAT_ID environment variable and restart.")

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        log.info("✓ Telegram credentials configured")
    else:
        log.warning("⚠️  Running in console-only mode (Telegram disabled)")

    seen = load_seen()
    log.info(f"Loaded {len(seen)} previously seen jobs.")

    send_startup_message()

    while True:
        try:
            seen = check_once(seen)
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")

        log.info(f"Sleeping {POLL_INTERVAL}s until next check...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
