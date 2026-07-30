# core/settings.py
import os
from datetime import datetime

from core import sensitive

# ============================================================
# SELECT CRAWLER
# ============================================================

CRAWLER = os.getenv("CRAWLER", "threads").lower()

BOT_NAME = CRAWLER

SPIDER_MODULES = [
    "instagram.spiders",
    "threads.spiders",
]

# ============================================================
# CONFIGURATION
# ============================================================


INSTAGRAM_USERNAME = sensitive.INSTAGRAM_USERNAME
INSTAGRAM_PASSWORD = sensitive.INSTAGRAM_PASSWORD

# ============================================================
# PLAYWRIGHT
# ============================================================

DOWNLOAD_HANDLERS = {
    # "http": "scrapy_playwright_stealth.handler.ScrapyPlaywrightStealthDownloadHandler", will cause browser duplicate in logs if both are active
    "https": "scrapy_playwright_stealth.handler.ScrapyPlaywrightStealthDownloadHandler",
}

TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

PLAYWRIGHT_BROWSER_TYPE = "chromium"

PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": False,
    "args": [
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US",
        "--start-maximized",
    ],
}

PLAYWRIGHT_CONTEXTS = {
    "default": {
        "no_viewport": True,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "color_scheme": "light",
        "has_touch": False,
        "is_mobile": False,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.7204.184 Safari/537.36"
        ),
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
}

if os.path.exists("playwright_state.json"):
    PLAYWRIGHT_CONTEXTS["default"]["storage_state"] = "playwright_state.json"


# ============================================================
# CRAWLING
# ============================================================

CONCURRENT_REQUESTS = 2
DOWNLOAD_DELAY = 5
MAX_TOTAL_SCRAPED = 10
RANDOMIZE_DOWNLOAD_DELAY = True
MAX_TOTAL_POST_CHECKED_PER_ACCOUNT = 10
MAX_CHECK_FOR_FLAG_TIME = 60 * 15
MAX_USERNAME_SCAN = 25

DEPTH_LIMIT = 2
CLOSESPIDER_PAGECOUNT = 50

RETRY_ENABLED = True
RETRY_TIMES = 3

# ============================================================
# OUTPUT
# ============================================================


SCHEDULER_DEBUG = True
LOG_LEVEL = "INFO"

now = datetime.now().strftime("%Y-%m-%d_%H:%M")
FEEDS = {
    f"instagram_data_{now}.json": {
        "format": "json",
        "indent": 2,
        "overwrite": True,
    }
}
