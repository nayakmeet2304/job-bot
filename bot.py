"""
Amazon UK Warehouse Job Alert Bot
Login mirrors opener_new.py — Selenium + real Chrome profile + Telegram OTP.
"""

import os
import json
import time
import queue
import logging
import threading
import requests
from urllib.parse import unquote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# ─── CONFIG ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
AMAZON_PHONE      = os.environ.get("AMAZON_PHONE", "")
AMAZON_PIN        = os.environ.get("AMAZON_PIN",   "")
AMAZON_OTP_METHOD = os.environ.get("AMAZON_OTP_METHOD", "sms")  # "sms" or "email"

# ── Chrome profile ──────────────────────────────────────────────────────────
# The bot uses a dedicated profile stored in ~/.job-bot-chrome so it never
# conflicts with your open Chrome window.  Cookies are saved between runs,
# so OTP is only needed once (the very first login).
BOT_CHROME_PROFILE = os.path.expanduser("~/.job-bot-chrome")

CENTRE_LAT    = 52.6369
CENTRE_LON    = -1.1398
MAX_MILES     = 30
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
SEEN_FILE     = "seen_jobs.json"

# Session cache — skip re-auth if < 80 min since last login
_last_auth_time: float = 0.0
SESSION_TTL = 80 * 60

# Telegram queues (fed by background poller)
_otp_queue:     queue.Queue = queue.Queue()
_command_queue: queue.Queue = queue.Queue()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[BOT] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def _bot_update_poller():
    """Daemon: long-polls Telegram and routes messages to the right queue."""
    offset = None
    # Drain stale updates so old messages don't feed as OTPs
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"timeout": 0}, timeout=10,
        )
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
    except Exception:
        pass

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30}, timeout=40,
            )
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                    continue
                text = msg.get("text", "").strip()
                if text.startswith("/"):
                    _command_queue.put(text)
                else:
                    _otp_queue.put(text)
        except Exception:
            time.sleep(5)

# ─── CHROME DRIVER ───────────────────────────────────────────────────────────

def start_driver() -> webdriver.Chrome:
    os.makedirs(BOT_CHROME_PROFILE, exist_ok=True)
    log.info(f"Using bot Chrome profile: {BOT_CHROME_PROFILE}")

    opts = Options()
    opts.add_argument(f"--user-data-dir={BOT_CHROME_PROFILE}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    except Exception:
        service = Service()

    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return driver


def wait_for_page_ready(driver, timeout=15):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def accept_cookies_if_present(driver):
    try:
        btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
            By.XPATH,
            '//button[contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"accept all")]'
            '| //button[@id="accept-all-cookies"]'
            '| //button[contains(text(),"Accept All")]',
        )))
        btn.click()
        log.info("Accepted cookie banner.")
    except TimeoutException:
        pass

# ─── SESSION STATE ────────────────────────────────────────────────────────────

def _mark_authenticated():
    global _last_auth_time
    _last_auth_time = time.time()
    log.info("Session marked as authenticated.")


def _is_session_probably_valid() -> bool:
    return time.time() - _last_auth_time < SESSION_TTL


def is_signed_in(driver) -> bool:
    try:
        src = driver.page_source.lower()
        return "sign out" in src or "signout" in src or "my profile" in src
    except Exception:
        return False

# ─── LOGIN HELPERS ────────────────────────────────────────────────────────────

def _find_input(driver, keywords: list):
    """Find a visible input whose name/id/placeholder/aria-label matches any keyword."""
    for kw in keywords:
        for attr in ("name", "id", "placeholder", "aria-label"):
            xpath = (
                f'//input[contains('
                f'translate(@{attr},"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")'
                f',"{kw}")]'
            )
            try:
                el = driver.find_element(By.XPATH, xpath)
                if el.is_displayed():
                    return el
            except Exception:
                pass
    return None


def _fill(driver, element, value: str):
    element.clear()
    element.send_keys(value)


def _click_continue(driver):
    for label in ("Verify", "Continue", "Next", "Sign in", "Confirm", "Submit"):
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((
                By.XPATH,
                f'//button[contains(text(),"{label}")] | //button[@type="submit"]',
            )))
            btn.click()
            return
        except Exception:
            pass


def _open_hamburger(driver) -> bool:
    """Open the mobile nav drawer. Returns True if 'Sign in' text appears."""
    selectors = [
        (By.XPATH, '//*[@aria-label="Open navigation menu"]'),
        (By.XPATH, '//*[@aria-label="menu"]'),
        (By.CSS_SELECTOR, 'header button:first-of-type'),
    ]
    for by, sel in selectors:
        try:
            WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel))).click()
            time.sleep(1)
            if any(t in driver.page_source for t in ["Sign in", "Create account"]):
                return True
        except Exception:
            pass
    # Positional fallback: first button in top-left
    for btn in driver.find_elements(By.TAG_NAME, "button"):
        try:
            loc = btn.location
            if loc["x"] < 220 and loc["y"] < 180:
                btn.click()
                time.sleep(1)
                if any(t in driver.page_source for t in ["Sign in", "Create account"]):
                    return True
        except Exception:
            pass
    return False


def _click_sign_in_entry(driver):
    try:
        _open_hamburger(driver)
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
            By.XPATH,
            '//a[contains(text(),"Sign in")] | //button[contains(text(),"Sign in")]'
            '| //a[contains(text(),"Login")] | //button[contains(text(),"Login")]',
        ))).click()
    except Exception:
        # Force redirect so the site sends us to auth.hiring.amazon.com
        driver.get("https://www.jobsatamazon.co.uk/app#/jobSearch")
        time.sleep(3)


def _select_otp_method(driver):
    src = driver.page_source.lower()
    if "verification code" not in src and "one-time" not in src:
        return
    method = AMAZON_OTP_METHOD.lower()
    try:
        lbl = driver.find_element(
            By.XPATH,
            f'//label[contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{method}")]',
        )
        lbl.click()
        time.sleep(0.5)
        _click_continue(driver)
    except Exception:
        pass


def _wait_for_otp_and_submit(driver, timeout=180) -> bool:
    send_telegram(
        f"🔐 Amazon OTP required!\n"
        f"Reply to this message with the code — you have {timeout // 60} minutes."
    )
    log.info("Waiting for OTP via Telegram...")

    otp = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            otp = _otp_queue.get(timeout=2)
            break
        except queue.Empty:
            pass

    if not otp:
        send_telegram("⚠️ OTP timeout — login failed.")
        return False

    log.info(f"OTP received: {otp.strip()}")
    inp = _find_input(driver, ["otp", "verification", "code", "one-time"])
    if not inp:
        try:
            inp = driver.find_element(By.CSS_SELECTOR,
                'input[type="number"], input[type="text"], input[type="tel"]')
        except Exception:
            pass

    if inp:
        _fill(driver, inp, otp.strip())
        _click_continue(driver)
        time.sleep(5)
        if is_signed_in(driver):
            send_telegram("✅ Sign-in successful!")
            return True

    send_telegram("⚠️ OTP entry failed.")
    return False


def fill_login_form(driver):
    """Step through auth.hiring.amazon.com pages until signed in."""
    for _ in range(12):
        time.sleep(2)
        url = driver.current_url.lower()
        src = driver.page_source.lower()

        # Left the auth domain → check if signed in
        if "auth.hiring.amazon.com" not in url:
            if is_signed_in(driver):
                log.info("Signed in.")
                return True
            continue

        if is_signed_in(driver):
            return True

        # OTP step
        if "verification code" in src or "one-time" in src or " otp" in src:
            log.info("OTP step.")
            _select_otp_method(driver)
            _wait_for_otp_and_submit(driver)
            continue

        # PIN step
        if "pin" in src and ("personal" in src or "enter" in src):
            log.info("PIN step.")
            inp = _find_input(driver, ["pin", "personal pin", "passcode", "password"])
            if not inp:
                try:
                    inp = driver.find_element(By.CSS_SELECTOR,
                        'input[type="password"], input[type="number"]')
                except Exception:
                    pass
            if inp:
                _fill(driver, inp, AMAZON_PIN)
                _click_continue(driver)
            continue

        # Mobile/phone step
        if any(k in src for k in ("mobile", "phone number", "country code")):
            log.info("Phone number step.")
            inp = _find_input(driver, ["mobile", "phone", "telephone"])
            if not inp:
                try:
                    inp = driver.find_element(By.CSS_SELECTOR,
                        'input[type="tel"], input[type="text"]')
                except Exception:
                    pass
            if inp:
                _fill(driver, inp, AMAZON_PHONE)
                _click_continue(driver)
            continue

        # Generic fallbacks
        for css, value in [
            ('input[type="password"]',            AMAZON_PIN),
            ('input[type="tel"]',                 AMAZON_PHONE),
            ('input[type="text"], input[type="number"]', AMAZON_PHONE),
        ]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, css)
                if el.is_displayed():
                    _fill(driver, el, value)
                    _click_continue(driver)
                    break
            except Exception:
                pass

    return is_signed_in(driver)


def ensure_signed_in(driver) -> bool:
    if is_signed_in(driver):
        _mark_authenticated()
        return True

    send_telegram("🔄 Not signed in — starting authentication...")
    accept_cookies_if_present(driver)
    _click_sign_in_entry(driver)
    time.sleep(3)
    fill_login_form(driver)

    # First wait (normal flow)
    deadline = time.time() + 20
    while time.time() < deadline:
        if is_signed_in(driver):
            _mark_authenticated()
            return True
        time.sleep(2)

    # Second wait (CAPTCHA / manual)
    send_telegram("⚠️ Login needs help — complete it in the browser within 60 s.")
    deadline = time.time() + 60
    while time.time() < deadline:
        if is_signed_in(driver):
            _mark_authenticated()
            return True
        time.sleep(2)

    log.error("Could not sign in.")
    return False

# ─── GRAPHQL JOB FETCH ───────────────────────────────────────────────────────

GRAPHQL_URL  = "https://www.jobsatamazon.co.uk/graphql"
SEARCH_QUERY = (
    "query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {\n"
    "  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {\n"
    "    nextToken\n"
    "    jobCards {\n"
    "      jobId jobTitle jobType employmentType\n"
    "      city state postalCode locationName\n"
    "      totalPayRateMin totalPayRateMax totalPayRateMinL10N totalPayRateMaxL10N\n"
    "      tagLine distance distanceL10N scheduleCount currencyCode\n"
    "      bonusPay bonusPayL10N bonusJob featuredJob\n"
    "      jobTypeL10N employmentTypeL10N virtualLocation jobLocationType\n"
    "    }\n"
    "    __typename\n"
    "  }\n"
    "}\n"
)


def fetch_jobs(driver) -> list:
    # Copy browser cookies + bearer token into a requests session
    bearer  = ""
    session = requests.Session()
    for c in driver.get_cookies():
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
        if c["name"] == "HVH_ACCESS_TOKEN":
            bearer = unquote(c["value"])

    resp = session.post(
        GRAPHQL_URL,
        json={
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
                        "lat": CENTRE_LAT, "lng": CENTRE_LON,
                        "unit": "mi", "distance": MAX_MILES,
                    },
                    "consolidateSchedule": True,
                }
            },
            "query": SEARCH_QUERY,
        },
        headers={
            "User-Agent":    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept":        "*/*",
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {bearer}",
            "country":       "United Kingdom",
            "iscanary":      "false",
            "Origin":        "https://www.jobsatamazon.co.uk",
            "Referer":       "https://www.jobsatamazon.co.uk/app",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return (
        resp.json()
            .get("data", {})
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

# ─── ALERTS ──────────────────────────────────────────────────────────────────

def format_job(job: dict) -> str:
    lines = []
    loc = ", ".join(filter(None, [
        job.get("locationName"), job.get("city"),
        job.get("state"), job.get("postalCode"),
    ]))
    lines.append(f"📍 {loc}")
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
    lines.append(f"🔗 https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job.get('jobId','')}")
    return "\n".join(lines)


def send_combined_alert(jobs: list):
    header    = f"🆕 {len(jobs)} new Amazon warehouse job(s) near Leicester!\n"
    separator = "\n" + "─" * 30 + "\n"
    blocks    = [format_job(j) for j in jobs]
    messages, current = [], header
    for i, block in enumerate(blocks):
        chunk = (separator if i > 0 else "\n") + block
        if len(current) + len(chunk) > 4000:
            messages.append(current)
            current = f"🆕 Continued ({i+1}/{len(blocks)})\n\n" + block
        else:
            current += chunk
    messages.append(current)
    for msg in messages:
        send_telegram(msg)
        time.sleep(0.3)

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────

def check_once(driver, seen: set) -> set:
    log.info("Checking for new jobs...")
    if not _is_session_probably_valid():
        driver.get("https://www.jobsatamazon.co.uk/")
        wait_for_page_ready(driver)
        accept_cookies_if_present(driver)
        ensure_signed_in(driver)

    try:
        jobs = fetch_jobs(driver)
    except Exception as e:
        log.warning(f"Fetch failed ({e}) — re-authenticating...")
        driver.get("https://www.jobsatamazon.co.uk/")
        wait_for_page_ready(driver)
        ensure_signed_in(driver)
        jobs = fetch_jobs(driver)

    log.info(f"Fetched {len(jobs)} job(s).")
    new_jobs = [j for j in jobs if j.get("jobId") and j["jobId"] not in seen]
    for j in new_jobs:
        seen.add(j["jobId"])

    if new_jobs:
        log.info(f"{len(new_jobs)} new job(s) — alerting.")
        send_combined_alert(new_jobs)
    else:
        log.info("No new jobs.")

    save_seen(seen)
    return seen


def main():
    log.info("Starting Amazon Job Alert Bot...")

    if not AMAZON_PHONE or not AMAZON_PIN:
        log.error(
            "Set AMAZON_PHONE and AMAZON_PIN.\n"
            "  export AMAZON_PHONE='+447XXXXXXXXX'\n"
            "  export AMAZON_PIN='XXXXXX'"
        )
        return

    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — alerts will print to console.")
    if not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set — alerts will print to console.")
    elif TELEGRAM_TOKEN:
        threading.Thread(target=_bot_update_poller, daemon=True).start()
        log.info("Telegram OTP poller started.")

    seen = load_seen()
    log.info(f"Loaded {len(seen)} previously seen job(s).")

    driver = start_driver()
    try:
        driver.get("https://www.jobsatamazon.co.uk/")
        wait_for_page_ready(driver)
        accept_cookies_if_present(driver)
        ensure_signed_in(driver)

        send_telegram(
            f"🤖 Amazon Job Alert Bot Started\n"
            f"📍 Within {MAX_MILES} miles of Leicester\n"
            f"🔄 Checking every {POLL_INTERVAL // 60} min"
        )

        while True:
            try:
                seen = check_once(driver, seen)
            except Exception as e:
                log.error(f"Loop error: {e}")
            log.info(f"Sleeping {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
