"""One-time (or whenever the session expires) Facebook login.

Opens a visible browser on the persistent profile the scraper uses.
Log into Facebook normally, verify Marketplace loads, then close the window
or press Enter in the terminal. Cookies persist in ~/.dealscout/fb_profile.

Usage: python -m worker.fb_login
"""

from playwright.sync_api import sync_playwright

from worker.scraper import USER_DATA_DIR


def main():
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("Opening Facebook in a browser window…")
    print("1. Log in with your normal account (approve any 2FA prompt).")
    print("2. Confirm you can see facebook.com/marketplace.")
    print("3. Come back here and press Enter to save the session.\n")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=False,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        input("Press Enter once you are logged in and Marketplace is visible… ")
        ctx.close()
    print(f"Session saved to {USER_DATA_DIR}. The worker can now scrape headlessly.")


if __name__ == "__main__":
    main()
