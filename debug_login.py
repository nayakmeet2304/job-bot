"""
Run this to take a screenshot and dump the HTML of the login page.
Usage:  python debug_login.py
Outputs: login_page.png  and  login_page.html
"""

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        locale="en-GB",
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.new_page()

    print("Loading main site...")
    page.goto("https://www.jobsatamazon.co.uk/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)

    print("Navigating to login page...")
    page.goto("https://www.jobsatamazon.co.uk/app#/login", wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(3000)

    print(f"Current URL: {page.url}")

    # Save screenshot
    page.screenshot(path="login_page.png", full_page=True)
    print("Screenshot saved: login_page.png")

    # Save HTML
    with open("login_page.html", "w") as f:
        f.write(page.content())
    print("HTML saved: login_page.html")

    # Print all input elements found
    inputs = page.locator("input").all()
    print(f"\nFound {len(inputs)} input element(s):")
    for i, inp in enumerate(inputs):
        try:
            attrs = {
                "type":        inp.get_attribute("type"),
                "name":        inp.get_attribute("name"),
                "placeholder": inp.get_attribute("placeholder"),
                "id":          inp.get_attribute("id"),
                "class":       inp.get_attribute("class"),
            }
            print(f"  [{i}] {attrs}")
        except Exception as e:
            print(f"  [{i}] error: {e}")

    # Print all buttons
    buttons = page.locator("button").all()
    print(f"\nFound {len(buttons)} button(s):")
    for i, btn in enumerate(buttons):
        try:
            print(f"  [{i}] text={btn.inner_text()!r}  type={btn.get_attribute('type')}")
        except Exception as e:
            print(f"  [{i}] error: {e}")

    browser.close()
    print("\nDone. Open login_page.png to see what the page looks like.")
