# threads/spiders/spider.py

import asyncio
import re
from datetime import datetime
from typing import List, Optional, Set

import scrapy
from playwright.async_api import Page
from scrapy import Request
from scrapy.http import HtmlResponse, Response

from core import settings

# ============================================================
# MAIN SPIDER
# ============================================================


class ThreadsCrawlerSpider(scrapy.Spider):
    name = "threads_crawler"

    custom_settings = {
        "FEEDS": {
            "output/threads_data.json": {
                "format": "json",
                "overwrite": True,
            }
        }
    }

    STARTING_ACCOUNT = "fcbarcelona"

    FLAG_KEYWORDS = [
        "fifa",
        "worldcup",
        "world cup",
        "wolds champion",
        "wolds champions",
        "mvp",
    ]

    async def start(self):
        """
        Async start - opens the Threads homepage.
        """
        self.logger.info(">>> async start() called")
        print(">>> async start() called")

        yield Request(
            url="https://www.threads.com/",
            callback=self.after_login_and_search,
            meta={
                "playwright": True,
                "playwright_include_page": True,
            },
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.scraped_accounts: Set[str] = set()
        self.failed_accounts: List[str] = []

        self._login_completed = False

        self.max_depth = settings.DEPTH_LIMIT

        self.stats = {
            "total_scraped": 0,
            "total_flags_found": 0,
            "total_errors": 0,
        }

    async def after_login_and_search(self, response: Response) -> None:
        """
        Open Threads, ensure we're logged in, then navigate to the starting account.
        """
        page: Page = response.meta.get("playwright_page")
        if not page:
            self.logger.error("No Playwright page found")
            return

        self.logger.info(">>> Threads page loaded")

        await asyncio.sleep(3)

        # ============================================================
        # STEP 1: LOGIN IF REQUIRED
        # ============================================================

        is_logged_in = await self._check_if_logged_in(page)

        if not is_logged_in:
            self.logger.info("Not logged in, starting Threads login...")

            login_success = await self._login_threads(page)

            if not login_success:
                self.logger.error("Login failed. Stopping spider.")
                self.logger.info(
                    f"Arguments being passed: {('Login failed',)}"
                )  # Debug
                # Try this alternative
                from scrapy.exceptions import CloseSpider

                raise CloseSpider("Login failed")

            self._login_completed = True
            self.logger.info(">>> Login successful!")

            # Threads sometimes requires a reload after login
            await page.reload(wait_until="networkidle")
            await asyncio.sleep(3)

        else:
            self.logger.info(">>> Already logged in!")
            self._login_completed = True

        # ============================================================
        # STEP 2: SEARCH / OPEN TARGET ACCOUNT
        # ============================================================

        account = self.STARTING_ACCOUNT

        search_success = await self._search_and_navigate_to_account(
            page,
            account,
        )

        if not search_success:
            self.logger.warning(
                f"Search failed. Trying direct URL for @{account}"
            )

            await page.goto(
                f"https://www.threads.com/@{account}",
                wait_until="networkidle",
            )

            await asyncio.sleep(3)

        # ============================================================
        # STEP 3: BEGIN SCRAPING
        # ============================================================

        current_url = page.url
        self.logger.info(f"Currently on: {current_url}")

        html = await page.content()

        new_response = HtmlResponse(
            url=current_url,
            body=html.encode("utf-8"),
            request=response.request,
        )

        new_response.meta["playwright_page"] = page
        new_response.meta["depth"] = 0

        async for item in self.parse(new_response):
            yield item

    async def _check_if_logged_in(self, page: Page) -> bool:
        """
        Return True only when Threads is fully logged in.
        """

        try:
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)

            # ------------------------------------------------------------
            # Landing page
            # ------------------------------------------------------------

            landing_button = page.locator(
                'div[role="button"]:has(i[aria-label="Instagram"])'
            ).filter(
                has=page.locator('span:has-text("Continue with Instagram")')
            )

            if await landing_button.count() > 0:
                self.logger.info("Threads landing page detected.")
                return False

            # ------------------------------------------------------------
            # Logged-in navigation
            # ------------------------------------------------------------

            logged_in_selectors = [
                'a[href="/search"]',
                'a[href="/activity"]',
                'a[href="/create"]',
                'svg[aria-label="Search"]',
                'svg[aria-label="Activity"]',
                'svg[aria-label="New thread"]',
            ]

            for selector in logged_in_selectors:
                if await page.locator(selector).count() > 0:
                    self.logger.info("Threads login confirmed.")
                    return True

            return False

        except Exception as e:
            self.logger.warning(f"Login check failed: {e}")
            return False

    async def _login_threads(self, page: Page) -> bool:
        """
        Log into Threads.

        This function does not return until either:

        - login succeeds
        - login fails
        """

        self.logger.info("Attempting Threads login...")

        try:

            # ============================================================
            # Landing page
            # ============================================================

            continue_button = (
                page.locator(
                    'div[role="button"]:has(i[aria-label="Instagram"])'
                )
                .filter(
                    has=page.locator('span:has-text("Continue with Instagram")')
                )
                .first
            )

            await continue_button.wait_for(
                state="visible",
                timeout=15000,
            )

            await continue_button.click()

            self.logger.info("Clicked Continue with Instagram")

            # ============================================================
            # Instagram login page
            # ============================================================

            await page.wait_for_selector(
                'input[name="email"]',
                timeout=30000,
            )

            await page.fill(
                'input[name="email"]',
                settings.INSTAGRAM_USERNAME,
            )

            await page.fill(
                'input[name="pass"]',
                settings.INSTAGRAM_PASSWORD,
            )

            login_button = page.locator(
                'div[role="button"][aria-label="Log In"]'
            )

            if await login_button.count() == 0:
                login_button = page.locator(
                    'div[role="button"]:has-text("Log in")'
                )

            await login_button.first.click()

            self.logger.info("Instagram login submitted.")

            # ============================================================
            # Monitor login flow
            # ============================================================

            while True:

                # Already logged in?
                if await self._check_if_logged_in(page):
                    self.logger.info("Threads login successful.")
                    return True

                # WhatsApp verification?
                verification = page.locator(
                    'span:has-text("Check your WhatsApp messages")'
                )

                if await verification.count() > 0:
                    await self._wait_for_whatsapp_verification(page)
                    continue

                # Login failed?
                login_error = page.locator(
                    "text=/incorrect|invalid|try again/i"
                )

                if await login_error.count() > 0:
                    self.logger.error("Instagram login failed.")
                    return False

                await asyncio.sleep(2)

        except Exception as e:
            self.logger.error(f"Threads login failed: {e}")

            try:
                await page.screenshot(path="debug_threads_login_failed.png")
            except Exception:
                pass

            return False

    async def _wait_for_whatsapp_verification(
        self,
        page: Page,
    ) -> None:
        """
        Wait until the WhatsApp verification flow has completed.

        This function intentionally has no timeout because verification
        requires manual interaction.
        """

        self.logger.info("WhatsApp verification detected.")
        self.logger.info("Waiting for user to complete verification...")

        verification = page.locator(
            'span:has-text("Check your WhatsApp messages")'
        )

        while await verification.count() > 0:
            await asyncio.sleep(2)

        self.logger.info("WhatsApp verification page closed.")

        # Allow redirects to finish.
        while not await self._check_if_logged_in(page):
            await asyncio.sleep(2)

        self.logger.info("Threads login detected after verification.")

    async def _search_and_navigate_to_account(
        self,
        page: Page,
        account: str,
    ) -> bool:
        """
        Navigate to a Threads profile using the built-in search.

        Steps:
        1. Click the Search button.
        2. Enter the username.
        3. Wait for search results.
        4. Click the matching account.
        5. Verify the profile opened.
        """

        self.logger.info(f"Navigating to account: @{account}")

        try:
            # ============================================================
            # STEP 1: OPEN SEARCH
            # ============================================================

            search_button = page.locator(
                'a[href="/search"], svg[aria-label="Search"]'
            ).first

            await search_button.wait_for(
                state="visible",
                timeout=10000,
            )

            await search_button.click()

            self.logger.info("Opened Threads search.")

            # ============================================================
            # STEP 2: WAIT FOR SEARCH INPUT
            # ============================================================

            search_input = page.locator(
                'input[type="search"], '
                'input[placeholder*="Search"], '
                'input[aria-label*="Search"]'
            ).first

            await search_input.wait_for(
                state="visible",
                timeout=10000,
            )

            await search_input.fill("")
            await search_input.fill(account)

            self.logger.info(f"Searching for @{account}")

            await page.wait_for_timeout(2000)

            # ============================================================
            # STEP 3: CLICK MATCHING ACCOUNT
            # ============================================================

            profile = page.locator(f'a[href="/@{account}"]').first

            await profile.wait_for(
                state="visible",
                timeout=10000,
            )

            await profile.click()

            self.logger.info(f"Clicked search result for @{account}")

            # ============================================================
            # STEP 4: VERIFY PROFILE
            # ============================================================

            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)

            if f"/@{account.lower()}" not in page.url.lower():
                self.logger.warning(
                    f"Unexpected page after navigation: {page.url}"
                )
                return False

            self.logger.info(f"Successfully navigated to @{account}")

            return True

        except Exception as e:
            self.logger.exception(f"Failed navigating to @{account}: {e}")
            return False

    async def parse(self, response: Response) -> None:
        """
        Parse a Threads profile.

        Workflow:
            1. Reload the profile if necessary.
            2. Check for scam flags.
            3. Stop immediately if no flags are found.
            4. Open relationship modal.
            5. Extract followers/following.
            6. Recursively crawl connected accounts.
        """

        page: Page = response.meta.get("playwright_page")
        if not page:
            self.logger.error("No Playwright page found.")
            return

        current_account = self._extract_username_from_url(page.url)
        if not current_account:
            self.logger.error(
                f"Could not determine username from URL: {page.url}"
            )
            return

        depth = response.meta.get("depth", 0)

        self.logger.info(f"Processing @{current_account} (Depth: {depth})")

        # ============================================================
        # ENSURE PROFILE IS FULLY LOADED
        # ============================================================

        await self._reload_profile_if_needed(page)

        # ============================================================
        # CHECK FLAGS
        # ============================================================

        flags = await self._check_for_flags(
            page,
            current_account,
        )

        if not flags:
            self.stats["total_scraped"] += 1

            self.logger.info(
                f"No flags found for @{current_account}. "
                "Skipping relationships."
            )

            self.logger.info(f"Stats: {self.stats}")
            return

        self.stats["total_flags_found"] += 1

        yield {
            "type": "flag",
            "account": current_account,
            "flags": flags,
            "depth": depth,
            "timestamp": self._get_timestamp(),
        }

        # ============================================================
        # DEPTH LIMIT
        # ============================================================

        if depth >= self.max_depth:
            self.stats["total_scraped"] += 1
            self.logger.info(f"Stats: {self.stats}")
            return

        # ============================================================
        # OPEN RELATIONSHIP MODAL
        # ============================================================

        relationship_button = page.locator(
            'div[role="button"]:has(span:has-text("followers"))'
        ).first

        await relationship_button.wait_for(
            state="visible",
            timeout=10000,
        )

        await relationship_button.click()

        # Wait until the Followers / Following tabs appear
        tabs = page.locator(
            'div[role="tab"][aria-selected]'
            ':has(> div[role="button"][aria-label][tabindex="0"])'
        )

        await tabs.first.wait_for(
            state="visible",
            timeout=10000,
        )

        await asyncio.sleep(1)

        # ============================================================
        # EXTRACT RELATIONSHIPS
        # ============================================================

        followers = (
            await self._extract_followers_from_modal(
                page,
                current_account,
                "Followers",
            )
            or []
        )

        following = (
            await self._extract_followers_from_modal(
                page,
                current_account,
                "Following",
            )
            or []
        )

        yield {
            "type": "relationship",
            "account": current_account,
            "followers_count": len(followers),
            "following_count": len(following),
            "followers": followers[:50],
            "following": following[:50],
            "depth": depth,
            "timestamp": self._get_timestamp(),
        }

        # ============================================================
        # RECURSIVE CRAWL
        # ============================================================

        new_depth = depth + 1

        discovered_accounts = set(followers + following)

        discovered_accounts.discard(current_account)

        for username in discovered_accounts:

            if username in self.scraped_accounts:
                continue

            if self.stats["total_scraped"] >= 50:
                break

            self.scraped_accounts.add(username)

            yield Request(
                url=f"https://www.threads.com/@{username}",
                callback=self.parse,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "depth": new_depth,
                },
            )

        self.stats["total_scraped"] += 1

        self.logger.info(f"Stats: {self.stats}")

    def _extract_username_from_url(self, url: str) -> Optional[str]:
        """
        Extract the username from a Threads profile URL.

        Examples:
            https://www.threads.com/@amongjiwo22
                -> amongjiwo22

            https://www.threads.com/@amongjiwo22/
                -> amongjiwo22

            https://www.threads.com/@amongjiwo22?xmt=AQ...
                -> amongjiwo22
        """
        try:
            match = re.search(r"threads\.com/@([^/?#]+)", url)
            if match:
                return match.group(1)

            return None

        except Exception:
            return None

    async def _reload_profile_if_needed(self, page: Page) -> None:
        # Wait a bit for the profile to finish rendering
        await asyncio.sleep(2)

        follower_button = page.locator(
            'div[role="button"]:has-text("followers")'
        ).first

        try:
            await follower_button.wait_for(timeout=3000)
            self.logger.info("Follower button already present.")
            return
        except Exception:
            pass

        self.logger.info("Follower button missing. Reloading profile...")

        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

        self.logger.info("Profile reload complete.")

    async def _check_for_flags(
        self,
        page: Page,
        account: str,
    ) -> List[str]:
        """
        Check the profile page for configured flag keywords.

        Returns a list of matched keywords.
        """
        self.logger.info(f"Checking flags for @{account}")

        try:
            # Wait for the page to finish rendering.
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

            # Extract all visible text from the page.
            page_text = await page.locator("body").inner_text()
            page_text = page_text.lower()

            matched_flags = []

            for keyword in self.FLAG_KEYWORDS:
                if keyword.lower() in page_text:
                    matched_flags.append(keyword)

            if matched_flags:
                self.logger.info(f"Flags found for @{account}: {matched_flags}")
            else:
                self.logger.info(f"No flags found for @{account}")

            return matched_flags

        except Exception as e:
            self.logger.error(f"Failed to check flags for @{account}: {e}")
            return []

    async def _extract_followers_from_modal(
        self,
        page: Page,
        account: str,
        list_type: str,
    ) -> List[str]:
        """
        Extract usernames from the Followers/Following modal.
        Assumes:
            - relationship modal is already open
            - Followers tab is initially selected
        """
        self.logger.info(f"Extracting {list_type} for @{account}")

        try:
            # ============================================================
            # WAIT FOR TABS
            # ============================================================

            tabs = page.locator(
                'div[role="tab"][aria-selected]'
                ':has(> div[role="button"][aria-label][tabindex="0"])'
            )

            await tabs.first.wait_for(
                state="visible",
                timeout=10000,
            )

            # ============================================================
            # SELECT TAB
            # ============================================================

            if list_type == "Following":
                await tabs.nth(1).locator(
                    'div[role="button"][aria-label][tabindex="0"]'
                ).click()

                await asyncio.sleep(2)

            # ============================================================
            # WAIT FOR FIRST LIST ITEM
            # ============================================================

            try:
                modal = page.locator(
                    "div:has(>"
                    "div:has(>"
                    "div:has(>"
                    "div[data-pressable-container][data-interactive-id]"
                    ")))"
                ).first
                print()
                print("scroll modal")
                print(await modal.evaluate("el => el.outerHTML"))
                print()
            except:
                pass

            items = page.locator(
                "div[data-pressable-container][data-interactive-id]"
            )
            print(await items.count())

            items = page.locator(
                'div[data-pressable-container][data-interactive-id]:has(a[href^="/@"])'
            )
            print(await items.count())

            items = page.locator(
                "div[data-pressable-container][data-interactive-id]"
                ':has(a[href^="/@"])'
                ':has(div[role="button"]:has-text("Follow"))'
            )
            print(await items.count())

            items = page.locator(
                "div[data-pressable-container][data-interactive-id]"
                ':has(a[href^="/@"])'
                ':has(div[role="button"]:has-text("Follow"))'
                ':not(:has(svg[aria-label="Like"]))'
            )
            print(await items.count())

            print()
            print("first item")
            print(await items.first.evaluate("el => el.outerHTML"))
            print(await items.first.is_visible())
            print()

            await items.first.wait_for(
                state="visible",
                timeout=10000,
            )

            # ============================================================
            # FIND SCROLLABLE CONTAINER
            # ============================================================

            scrollable = await items.first.evaluate_handle("""
            (node) => {
                let p = node.parentElement;

                while (p) {
                    if (p.scrollHeight > p.clientHeight + 10) {
                        return p;
                    }
                    p = p.parentElement;
                }

                return null;
            }
            """)

            usernames = set()

            previous_count = -1
            stagnant_rounds = 0

            while stagnant_rounds < 5:

                # --------------------------------------------------------
                # Read every visible list row
                # --------------------------------------------------------

                rows = page.locator(
                    'div[data-pressable-container="true"][data-interactive-id]'
                )

                row_count = await rows.count()

                for i in range(row_count):

                    row = rows.nth(i)

                    link = row.locator('a[href^="/@"]').first

                    if await link.count() == 0:
                        continue

                    href = await link.get_attribute("href")

                    if not href:
                        continue

                    username = href.removeprefix("/@")
                    username = username.split("?", 1)[0]
                    username = username.split("/", 1)[0]

                    if username:
                        usernames.add(username)

                self.logger.info(
                    f"{list_type}: collected {len(usernames)} usernames"
                )

                # --------------------------------------------------------
                # End detection
                # --------------------------------------------------------

                if len(usernames) == previous_count:
                    stagnant_rounds += 1
                else:
                    stagnant_rounds = 0
                    previous_count = len(usernames)

                # --------------------------------------------------------
                # Scroll the ACTUAL scroll container
                # --------------------------------------------------------

                await scrollable.evaluate("""
    el => {
        el.scrollBy(0, el.clientHeight * 0.8);
    }
    """)

                await asyncio.sleep(1)

            self.logger.info(
                f"Extracted {len(usernames)} {list_type} from @{account}"
            )

            return sorted(usernames)

        except Exception as e:
            self.logger.error(
                f"Failed extracting {list_type} for @{account}: {e}"
            )
            return []

    def _get_timestamp(self) -> str:
        """
        Return the current timestamp in ISO 8601 format.
        Example: 2026-07-22T15:34:12
        """
        return datetime.now().isoformat(timespec="seconds")
