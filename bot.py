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
    """Fallback: Use Selenium to render jobsatamazon.co.uk and fetch jobs.
    Selenium can render JavaScript and is compatible with Python 3.13.
    Attempts to find Chrome from multiple sources (system, webdriver-manager).
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.service import Service
        import os
    except ImportError as e:
        log.error(f"Selenium not installed: {e}")
        log.info("Install with: pip install selenium webdriver-manager")
        return []
    
    driver = None
    try:
        log.info("Starting Selenium browser to fetch jobs...")
        
        # Setup Chrome options for headless mode
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument(f"user-agent={HEADERS.get('User-Agent', '')}")
        
        # Try to find Chrome in common locations
        chrome_paths = [
            "/usr/bin/chromium",
            "/snap/bin/chromium",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
        
        chrome_binary = None
        for path in chrome_paths:
            if os.path.exists(path):
                log.info(f"Found Chrome at: {path}")
                chrome_binary = path
                options.binary_location = path
                break
        
        # If Chrome not found in standard paths, try webdriver-manager
        if not chrome_binary:
            log.info("Chrome not found in standard paths; using webdriver-manager...")
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                chrome_driver_path = ChromeDriverManager().install()
                service = Service(chrome_driver_path)
            except Exception as e:
                log.error(f"Could not get ChromeDriver: {e}")
                return []
        else:
            service = None
        
        # Create driver
        if service:
            driver = webdriver.Chrome(service=service, options=options)
        else:
            driver = webdriver.Chrome(options=options)
        
        # Navigate to jobs page with geo filters
        jobs_url = (
            f"https://www.jobsatamazon.co.uk/app#/jobSearch"
            f"?lat={CENTRE_LAT}&lng={CENTRE_LON}&distance={MAX_MILES}"
        )
        log.info(f"Loading: {jobs_url}")
        driver.get(jobs_url)
        
        # Wait for page to fully load
        import time
        time.sleep(5)  # Let JavaScript execute
        
        # Debug: Log page source to understand structure
        page_source = driver.page_source
        if "job" in page_source.lower():
            log.debug("Page contains 'job' keyword")
        else:
            log.warning("Page does not contain 'job' keyword - site may not be loaded")
        
        # Look for any element that might contain job info
        try:
            job_containers = driver.find_elements(By.CSS_SELECTOR, "[data-testid], [class*='job'], [class*='card'], article, section")
            log.info(f"Found {len(job_containers)} potential containers on page")
            
            # Log first few element classes to help debug
            for i, elem in enumerate(job_containers[:5]):
                try:
                    class_name = elem.get_attribute("class")
                    test_id = elem.get_attribute("data-testid")
                    tag = elem.tag_name
                    log.debug(f"  Element {i}: <{tag}> class='{class_name}' testid='{test_id}'")
                except:
                    pass
        except:
            pass
        
        # Try multiple selectors in order of specificity
        selectors_to_try = [
            "[data-testid='job-card']",
            "[class*='job-card']",
            "[class*='JobCard']",
            "article[class*='job']",
            "[class*='job'][class*='item']",
            "[class*='position'][class*='card']",
            ".job",
            "[class*='listing']",
        ]
        
        job_elements = []
        for selector in selectors_to_try:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    log.info(f"Found {len(elements)} elements with selector: {selector}")
                    job_elements = elements
                    break
            except:
                continue
        
        if not job_elements:
            # Last resort: try to find ANY clickable job link
            try:
                job_elements = driver.find_elements(By.XPATH, "//a[contains(@href, 'jobDetail') or contains(@href, 'job')]")
                if job_elements:
                    log.info(f"Found {len(job_elements)} job links via href pattern")
            except:
                pass
        
        log.info(f"Found {len(job_elements)} job cards in DOM")
        
        # Extract jobs from found elements
        jobs = []
        for elem in job_elements[:100]:  # Limit to first 100 to avoid slowdown
            try:
                # If element is a link, get its href and text directly
                if elem.tag_name == "a":
                    url = elem.get_attribute("href")
                    if not url.startswith("http"):
                        url = "https://www.jobsatamazon.co.uk" + url
                    
                    title = elem.text.strip() if elem.text else "Warehouse Operative"
                    
                    # Try to extract location/pay from parent container
                    parent = elem.find_element(By.XPATH, "..")
                    location_elem = parent.find_elements(By.CSS_SELECTOR, "[class*='location'], [class*='address']")
                    location = location_elem[0].text if location_elem else "Unknown"
                    
                    pay_elem = parent.find_elements(By.CSS_SELECTOR, "[class*='pay'], [class*='salary']")
                    pay = pay_elem[0].text if pay_elem else ""
                    
                else:
                    # Element is a container - extract nested content
                    title_elem = elem.find_elements(By.CSS_SELECTOR, "h2, [class*='title'], a[href*='jobDetail']")
                    title = title_elem[0].text if title_elem else "Warehouse Operative"
                    
                    location_elem = elem.find_elements(By.CSS_SELECTOR, "[class*='location'], [class*='address']")
                    location = location_elem[0].text if location_elem else "Unknown"
                    
                    pay_elem = elem.find_elements(By.CSS_SELECTOR, "[class*='pay'], [class*='salary']")
                    pay = pay_elem[0].text if pay_elem else ""
                    
                    link_elem = elem.find_elements(By.CSS_SELECTOR, "a[href]")
                    url = link_elem[0].get_attribute("href") if link_elem else ""
                    if url and not url.startswith("http"):
                        url = "https://www.jobsatamazon.co.uk" + url
                
                # Extract or generate job ID
                job_id = url.split("jobId=")[-1] if "jobId=" in url else f"SEL-{int(time.time())}-{len(jobs)}"
                
                # Create job dict - use keys that parse_job() expects
                job = {
                    "jobId": job_id,  # parse_job expects camelCase
                    "title": title.strip(),
                    "city": location.split(",")[0] if "," in location else location.strip(),
                    "state": "England",
                    "postalCode": "",
                    "pay": pay.strip(),
                    "employmentType": "",
                    "scheduleType": "",
                    "jobDescription": "",
                    "firstDayOnSite": "",
                    "shiftCode": "",
                    "hoursPerWeek": "",
                    "totalOpenings": 1,
                    "url": url,
                }
                
                jobs.append(job)
                log.debug(f"Extracted job: {title[:40]} @ {location} (ID: {job_id})")
                
            except Exception as e:
                log.debug(f"Error parsing job element: {e}")
                continue
        
        if jobs:
            log.info(f"✓ Selenium: extracted {len(jobs)} jobs from rendered page")
            return jobs
        else:
            log.warning("Selenium loaded page but found no jobs")
            return []
            
    except Exception as e:
        log.error(f"Selenium browser fetch failed: {type(e).__name__}: {e}")
        return []
    finally:
        if driver:
            driver.quit()


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
            log.debug(f"Job rejected: no jobId found. Keys: {list(job.keys())}")
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
