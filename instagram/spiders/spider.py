# instagram/spiders/spider.py
import json
import math
import random
import re
from datetime import datetime
from typing import List, Optional, Set

import scrapy
from playwright.async_api import Page, TimeoutError
from scrapy import Request
from scrapy.http import HtmlResponse, Response
from twisted.python.failure import Failure

from core import settings

# ============================================================
# MAIN SPIDER
# ============================================================


class InstagramCrawlerSpider(scrapy.Spider):
    name = "instagram_crawler"

    """custom_settings = {
        "FEEDS": {
            "output/instagram_data.json": {
                "format": "json",
                "overwrite": True,
            }
        }
    }"""

    STARTING_ACCOUNT = "zahwalytm"

    FLAG_KEYWORDS = [
        "kartini",
        "queens",
        "queen",
        "pengalaman",
    ]

    async def start(self):
        """
        Async start - opens the Instagram homepage.
        """
        self.logger.info(">>> async start() called")
        print(">>> async start() called")

        yield Request(
            url="https://www.instagram.com/",
            callback=self.after_login_and_search,
            errback=self.errback,
            meta={
                "playwright": True,
                "playwright_include_page": True,
            },
        )

    async def errback(self, failure: Failure):
        """
        Called when a request fails.
        Always close the Playwright page to avoid page leaks.
        """
        self.logger.error(f"Request failed: {failure.getErrorMessage()}")

        page = failure.request.meta.get("playwright_page")

        if page and not page.is_closed():
            try:
                await page.close()
                self.logger.info("Closed Playwright page from errback.")
            except Exception:
                self.logger.exception("Failed to close Playwright page.")

        self.stats["total_errors"] += 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.scraped_accounts: Set[str] = set()
        self.failed_accounts: List[str] = []

        self._login_completed = False

        self.max_depth = settings.DEPTH_LIMIT
        self.max_total_scraped = settings.MAX_TOTAL_SCRAPED
        self.max_total_post_checked_per_account = (
            settings.MAX_TOTAL_POST_CHECKED_PER_ACCOUNT
        )
        self.max_check_for_flag_time = settings.MAX_CHECK_FOR_FLAG_TIME
        self.max_username_scan = settings.MAX_USERNAME_SCAN
        self.max_comments_scan = settings.MAX_COMMENTS_SCAN

        self.stats = {
            "total_scraped": 0,
            "total_flags_found": 0,
            "total_errors": 0,
        }

    async def after_login_and_search(self, response: Response) -> None:
        """
        Open Instagram, ensure we're logged in, then navigate to the starting account.
        """
        page: Page = response.meta.get("playwright_page")
        if not page:
            self.logger.error("No Playwright page found")
            return

        self.logger.info(">>> Instagram page loaded")

        await self._random_delay(page, 1000)

        # ============================================================
        # STEP 1: LOGIN IF REQUIRED
        # ============================================================

        is_logged_in = await self._check_if_logged_in(page)

        if not is_logged_in:
            self.logger.info("Not logged in, starting Instagram login...")

            login_success = await self._login_instagram(page)

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

            # save login
            try:
                save_login_button = (
                    page.locator('div[role="button"]')
                    .get_by_text("Save", exact=True)
                    .first
                )
                await save_login_button.wait_for(timeout=3000)

                if await save_login_button.count() > 0:
                    await save_login_button.click()

                await self._random_delay(page, 1000)
            except TimeoutError:
                pass
        else:
            self.logger.info(">>> Already logged in!")
            self._login_completed = True

        # reject notification
        try:
            dialog = page.locator(
                'div[role="dialog"]:has(h2:has-text("Turn on Notifications"))'
            )
            await dialog.wait_for(timeout=3000)

            await dialog.get_by_role("button", name="Not Now").click()
            self.logger.info("notification rejected")
        except TimeoutError:
            # Modal never appeared
            self.logger.info("notification request doesnt exist")

        # save login state
        await page.context.storage_state(path="playwright_state.json")

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
                f"Search failed. Trying direct URL for {account}"
            )

            await page.goto(
                f"https://www.instagram.com/{account}",
                wait_until="networkidle",
            )

            await self._random_delay(page, 1000)

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

        self.logger.info(f"Yielding {current_url}")
        yield Request(
            url=current_url,
            callback=self.parse,
            errback=self.errback,
            meta={
                "playwright": True,
                "playwright_include_page": True,
                "depth": 0,
            },
            dont_filter=True,
        )

        # async for item in self.parse(new_response):
        #    yield item

        self.logger.info("Scraping finished")

        if page and not page.is_closed():
            await page.close()

    async def _check_if_logged_in(self, page: Page) -> bool:
        """
        Return True only when Instagram is fully logged in.
        """

        try:
            await page.wait_for_load_state("domcontentloaded")
            await self._random_delay(page, 1000)

            # ------------------------------------------------------------
            # Landing page
            # ------------------------------------------------------------

            landing_button = page.locator('input[name="email"]')

            if await landing_button.count() > 0:
                self.logger.info("Instagram landing page detected.")
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
                    self.logger.info("Instagram login confirmed.")
                    return True

            return False

        except Exception as e:
            self.logger.warning(f"Login check failed: {e}")
            return False

    async def _login_instagram(self, page: Page) -> bool:
        """
        Log into Instagram.

        This function does not return until either:

        - login succeeds
        - login fails
        """

        self.logger.info("Attempting Instagram login...")

        try:
            # ============================================================
            # Instagram login page
            # ============================================================

            await page.wait_for_selector(
                'input[name="email"]',
                timeout=5000,
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
                    self.logger.info("Instagram login successful.")
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

                await self._random_delay(page, 2000)

        except Exception as e:
            self.logger.error(f"Instagram login failed: {e}")
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
            await self._random_delay(page, 2000)

        self.logger.info("WhatsApp verification page closed.")

        # Allow redirects to finish.
        while not await self._check_if_logged_in(page):
            await self._random_delay(page, 2000)

        self.logger.info("Instagram login detected after verification.")

    async def _search_and_navigate_to_account(
        self,
        page: Page,
        account: str,
    ) -> bool:
        """
        Navigate to a Instagram profile using the built-in search.

        Steps:
        1. Click the Search button.
        2. Enter the username.
        3. Wait for search results.
        4. Click the matching account.
        5. Verify the profile opened.
        """

        self.logger.info(f"Navigating to account: {account}")

        try:
            # ============================================================
            # STEP 1: OPEN SEARCH
            # ============================================================

            search_button = page.locator(
                'a[href="/search"], svg[aria-label="Search"]'
            ).first

            await search_button.wait_for(
                state="visible",
                timeout=5000,
            )

            await search_button.click()

            self.logger.info("Opened Instagram search.")

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
                timeout=5000,
            )

            await search_input.fill("")
            await search_input.fill(account)

            self.logger.info(f"Searching for {account}")

            await page.wait_for_timeout(2000)

            # ============================================================
            # STEP 3: CLICK MATCHING ACCOUNT
            # ============================================================

            profile = page.locator(f'a[href="/{account}/"]').first

            await profile.wait_for(
                state="visible",
                timeout=5000,
            )

            await profile.click()

            self.logger.info(f"Clicked search result for {account}")

            # ============================================================
            # STEP 4: VERIFY PROFILE
            # ============================================================

            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)

            if f"/{account.lower()}" not in page.url.lower():
                self.logger.warning(
                    f"Unexpected page after navigation: {page.url}"
                )
                return False

            self.logger.info(f"Successfully navigated to {account}")

            return True

        except Exception as e:
            self.logger.exception(f"Failed navigating to {account}: {e}")
            return False

    async def parse(self, response: Response):
        """
        Parse an Instagram profile.

        Workflow:
            1. Validate profile.
            2. Skip empty/private accounts.
            3. Scan posts for flagged keywords.
            4. Save flagged post metadata.
            5. Collect followers/following.
            6. Save relationship data.
            7. Continue crawling discovered accounts.
        """
        self.logger.info(f"parse() entered for {response.url}")
        page: Page | None = response.meta.get("playwright_page")

        if page is None:
            self.logger.error("No Playwright page found.")
            return

        current_account = self._extract_username_from_url(page.url)
        self.scraped_accounts.add(current_account)

        if not current_account:
            self.logger.error(f"Unable to determine username from {page.url}")
            return

        depth = response.meta.get("depth", 0)

        self.logger.info(
            f"========== Processing @{current_account} (depth={depth}) =========="
        )

        try:

            # ============================================================
            # SKIP EMPTY ACCOUNTS
            # ============================================================

            if await page.locator('h1:has-text("No posts yet")').count() > 0:
                self.logger.info(f"Skipping @{current_account}: no posts.")
                return

            # ============================================================
            # SKIP PRIVATE ACCOUNTS
            # ============================================================

            if (
                await page.locator(
                    'span:has-text("This profile is private")'
                ).count()
                > 0
            ):
                self.logger.info(f"Skipping @{current_account}: private.")
                return

            # ============================================================
            # CHECK POSTS
            # ============================================================

            self.logger.info(f"Scanning posts for @{current_account}")

            flagged_post = await self._check_for_flags(
                page,
                current_account,
            )

            if not flagged_post:
                self.logger.info("No flagged posts found.")
                return

            self.stats["total_flags_found"] += 1

            # ============================================================
            # DEPTH LIMIT
            # ============================================================

            if depth >= self.max_depth:
                self.logger.info(f"Maximum depth ({self.max_depth}) reached.")

                yield flagged_post
                return

            # ============================================================
            # RETURN TO PROFILE
            # ============================================================

            try:
                await page.keyboard.press("Escape")
                await self._random_delay(page, 500)
            except Exception:
                pass

            await page.evaluate("window.scrollTo(0,0)")
            await self._random_delay(page, 1000)

            # ============================================================
            # EXTRACT FOLLOWERS
            # ============================================================

            self.logger.info("Opening Followers modal...")

            followers_button = page.locator(
                'section a[role="link"]:has-text("followers")'
            ).first

            await followers_button.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            await followers_button.click()

            dialog = page.locator('div[role="dialog"]').last

            await dialog.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            followers = (
                await self._extract_followers_from_modal(
                    page,
                    current_account,
                    "Followers",
                )
                or []
            )

            # ------------------------------------------------------------
            # CLOSE FOLLOWERS MODAL
            # ------------------------------------------------------------

            self.logger.info("Closing Followers modal...")

            await page.keyboard.press("Escape")

            await dialog.wait_for(
                state="hidden",
                timeout=self._random_timeout(5000),
            )

            await self._random_delay(page, 750)

            # ============================================================
            # OPEN FOLLOWING
            # ============================================================

            self.logger.info("Opening Following modal...")

            following_button = page.locator(
                'section a[role="link"]:has-text("following")'
            ).first

            await following_button.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            await following_button.click()

            dialog = page.locator('div[role="dialog"]').last

            await dialog.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            following = (
                await self._extract_followers_from_modal(
                    page,
                    current_account,
                    "Following",
                )
                or []
            )

            # ------------------------------------------------------------
            # CLOSE FOLLOWING MODAL
            # ------------------------------------------------------------

            await page.keyboard.press("Escape")

            await dialog.wait_for(
                state="hidden",
                timeout=self._random_timeout(5000),
            )

            await self._random_delay(page, 500)

            # ============================================================
            # SAVE COMPLETE RECORD
            # ============================================================

            flagged_post["scraped_account"] = current_account
            flagged_post["followers"] = followers
            flagged_post["following"] = following
            flagged_post["timestamp"] = self._get_timestamp()
            flagged_post["depth"] = depth

            per_user_json = f"output_{settings.now}/instagram_data_{settings.now}_{current_account}.json"
            with open(per_user_json, "w", encoding="utf-8") as file:
                file.write(json.dumps(flagged_post))

            per_user_json = f"output_{settings.now}/instagram_data_{settings.now}_append.json"
            with open(per_user_json, "a", encoding="utf-8") as file:
                file.write(json.dumps(flagged_post))
                file.write("\n\n")

            yield flagged_post

            # ============================================================
            # DISCOVER NEW ACCOUNTS
            # ============================================================

            new_depth = depth + 1

            discovered_accounts = (
                set(followers).union(following).difference({current_account})
            )

            self.logger.info(f"Discovered {len(discovered_accounts)} accounts.")

            queued = 0

            for username in discovered_accounts:

                if username in self.scraped_accounts:
                    continue

                if self.stats["total_scraped"] >= self.max_total_scraped:
                    self.logger.info("Maximum scrape count reached.")
                    break

                self.scraped_accounts.add(username)
                queued += 1

                self.logger.info(f"Yielding request for {username}")

                yield Request(
                    url=f"https://www.instagram.com/{username}",
                    callback=self.parse,
                    errback=self.errback,
                    meta={
                        "playwright": True,
                        "playwright_include_page": True,
                        "depth": new_depth,
                    },
                )

            self.logger.info(f"Queued {queued} accounts.")

        except Exception:
            self.logger.exception(f"Failed processing @{current_account}")
            raise

        finally:
            self.stats["total_scraped"] += 1
            self.logger.info(f"Finished @{current_account}. Stats={self.stats}")

            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

            if page and not page.is_closed():
                await page.close()
                self.logger.info(f"Closed page for @{current_account}")

        self.logger.info(f"Leaving parse() for @{current_account}")

    def _extract_username_from_url(self, url: str) -> Optional[str]:
        """
        Extract the username from a Instagram profile URL.

        Examples:
            https://www.instagram.com/amongjiwo22
                -> amongjiwo22

            https://www.instagram.com/amongjiwo22/
                -> amongjiwo22

            https://www.instagram.com/amongjiwo22?xmt=AQ...
                -> amongjiwo22
        """
        try:
            match = re.search(r"instagram\.com/([^/?#]+)", url)
            if match:
                return match.group(1)

            return None

        except Exception:
            return None

    async def _check_for_flags(
        self,
        page: Page,
        account: str,
    ) -> dict | None:
        """
        Open recent posts and search them for configured flag keywords.
        Stops immediately after the first flagged post.
        """

        self.logger.info(f"Checking flags for {account}")

        matched_post = None
        processed_posts = set()
        post_checked = 0

        try:
            # ============================================================
            # WAIT FOR PROFILE
            # ============================================================

            self.logger.info("Waiting for profile to finish loading...")

            await page.wait_for_load_state("networkidle")
            await self._random_delay(page, 1000)

            # ============================================================
            # SCROLL TO POSTS
            # ============================================================

            article = page.locator("article")

            if await article.count():
                self.logger.info("Scrolling post grid into view...")
                await article.first.scroll_into_view_if_needed()
            else:
                self.logger.info("Article not found. Scrolling page...")
                await page.mouse.wheel(0, 800)

            await self._random_delay(page, 1000)

            # ============================================================
            # CHECK POSTS
            # ============================================================
            start_time = datetime.now().timestamp()
            while True:
                posts = page.locator(
                    'main a[href*="/p/"], main a[href*="/reel/"]'
                )

                visible_posts = await posts.count()

                self.logger.info(
                    f"Found {visible_posts} visible posts "
                    f"({len(processed_posts)} already checked)"
                    f"Max number to check: {self.max_total_post_checked_per_account}"
                )

                if visible_posts == 0:
                    self.logger.warning("No posts found.")
                    break

                new_post_found = False

                for index in range(visible_posts):
                    # Instagram frequently rerenders the grid
                    if post_checked >= self.max_total_post_checked_per_account:
                        break

                    posts = page.locator(
                        'main a[href*="/p/"], main a[href*="/reel/"]'
                    )

                    if index >= await posts.count():
                        self.logger.info("Post grid changed while scanning.")
                        break

                    href = await posts.nth(index).get_attribute("href")

                    if not href:
                        continue

                    if href in processed_posts:
                        continue

                    processed_posts.add(href)
                    new_post_found = True

                    self.logger.info(f"Opening {href}")

                    try:
                        await posts.nth(index).scroll_into_view_if_needed()
                        await self._random_delay(page, 150)

                        await posts.nth(index).click(
                            timeout=self._random_timeout(2500),
                        )

                        dialog = page.locator("div[role='dialog']").first

                        self.logger.info("Waiting for post dialog...")

                        await dialog.wait_for(
                            state="visible",
                            timeout=self._random_timeout(5000),
                        )

                        await page.wait_for_load_state("networkidle")
                        await self._random_delay(page, 1000)

                        self.logger.info("Reading post text...")

                        dialog_text = (await dialog.inner_text()).lower()

                        flagged_keywords = [
                            keyword
                            for keyword in self.FLAG_KEYWORDS
                            if keyword.lower() in dialog_text
                        ]

                        if flagged_keywords:

                            self.logger.info(f"Flags found: {flagged_keywords}")

                            self.logger.info(
                                "Extracting flagged post details..."
                            )

                            matched_post = {
                                "post_url": await self._extract_post_url(page),
                                "caption": await self._extract_post_caption(
                                    page
                                ),
                                "media": await self._extract_post_media(page),
                                "comments": await self._extract_post_comments(
                                    page
                                ),
                                "flagged_keywords": flagged_keywords,
                            }

                            self.logger.info("Closing post dialog...")

                            await page.keyboard.press("Escape")

                            await dialog.wait_for(
                                state="hidden",
                                timeout=self._random_timeout(2500),
                            )

                            await self._random_delay(page, 250)

                            self.logger.info("Returning to profile header...")

                            await page.evaluate("window.scrollTo(0, 0)")

                            await self._random_delay(page, 1000)

                            await page.locator("header").first.wait_for(
                                state="visible",
                                timeout=self._random_timeout(2500),
                            )

                            self.logger.info(f"Finished checking {account}")

                            return matched_post

                        self.logger.info("No flags found in this post.")

                        await page.keyboard.press("Escape")

                        await dialog.wait_for(
                            state="hidden",
                            timeout=self._random_timeout(2500),
                        )

                        await self._random_delay(page, 250)

                    except Exception as e:

                        self.logger.warning(f"Failed processing {href}: {e}")

                        try:
                            await page.keyboard.press("Escape")
                        except Exception:
                            pass
                    finally:
                        post_checked += 1

                # ========================================================
                # DONE WITH CURRENT GRID
                # ========================================================

                if not new_post_found:
                    self.logger.info("No unseen posts remain.")
                    break

                if post_checked >= self.max_total_post_checked_per_account:
                    self.logger.info("Max post check reached")
                    break

                if (
                    datetime.now().timestamp() - start_time
                ) >= self.max_check_for_flag_time:
                    self.logger.info("Max time to check for flags reached")
                    break

                previous_count = visible_posts

                self.logger.info("Scrolling to load more posts...")

                await page.mouse.wheel(0, 2500)
                await self._random_delay(page, 1000)

                posts = page.locator(
                    'main a[href*="/p/"], main a[href*="/reel/"]'
                )

                new_count = await posts.count()

                self.logger.info(f"Visible posts after scroll: {new_count}")

                if new_count <= previous_count:
                    self.logger.info("No additional posts loaded.")
                    break

            if matched_post:
                self.logger.info(
                    f"Found {matched_post} flagged post(s) for {account}."
                )
            else:
                self.logger.info(
                    f"No flags found after checking "
                    f"{len(processed_posts)} posts."
                )

            return matched_post

        except Exception as e:
            self.logger.error(f"Failed checking flags for {account}: {e}")
            return None

    async def _extract_followers_from_modal(
        self,
        page: Page,
        account: str,
        list_type: str,
    ) -> List[str]:
        """
        Extract usernames from the Followers/Following dialog.

        Assumes:
            - Relationship dialog is already open.
            - Followers tab is selected by default.
        """

        self.logger.info(
            f"Starting {list_type.lower()} extraction for {account}"
        )

        try:
            # ============================================================
            # WAIT FOR DIALOG
            # ============================================================

            self.logger.info("Waiting for relationship dialog...")

            dialog = page.locator('div[role="dialog"]').last

            await dialog.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            self.logger.info("Relationship dialog is visible.")

            # ============================================================
            # WAIT FOR FIRST ROW
            # ============================================================

            self.logger.info(f"Waiting for first {list_type.lower()} row...")

            rows = dialog.locator('a[href^="/"][role="link"]:has(span)')

            await rows.first.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            self.logger.info("Relationship list has loaded.")

            # ============================================================
            # FIND SCROLLABLE CONTAINER
            # ============================================================

            self.logger.info("Searching for scrollable container...")

            scrollable = await rows.first.evaluate_handle("""
                node => {
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

            if await scrollable.json_value() is None:
                self.logger.warning("Scrollable container not found.")
                return []

            self.logger.info("Scrollable container found.")

            # ============================================================
            # SCAN USERNAMES
            # ============================================================

            usernames = set()

            previous_count = -1
            stagnant_rounds = 0
            round_number = 0
            max_stagnant_rounds = 3

            while stagnant_rounds < max_stagnant_rounds:

                round_number += 1

                self.logger.info(f"{list_type}: scan round {round_number}")

                links = dialog.locator('a[href^="/"][role="link"]')

                visible_links = await links.count()

                self.logger.info(f"{visible_links} visible profile links.")

                before_count = len(usernames)

                for i in range(visible_links):

                    href = await links.nth(i).get_attribute("href")

                    if not href:
                        continue

                    username = href.strip("/").split("/", 1)[0].split("?", 1)[0]

                    if (
                        not username
                        or username.startswith("p")
                        or username.startswith("explore")
                        or username.startswith("accounts")
                        or username.startswith("reels")
                    ):
                        continue

                    usernames.add(username)

                added = len(usernames) - before_count

                self.logger.info(
                    f"{list_type}: "
                    f"{len(usernames)} usernames collected "
                    f"(+{added} this round)"
                )

                # --------------------------------------------------------
                # END DETECTION
                # --------------------------------------------------------

                if len(usernames) == previous_count:

                    stagnant_rounds += 1

                    self.logger.info(
                        "No new usernames found "
                        f"({stagnant_rounds}/{max_stagnant_rounds})"
                    )

                else:

                    previous_count = len(usernames)
                    stagnant_rounds = 0

                    self.logger.info("New usernames discovered.")

                if stagnant_rounds >= max_stagnant_rounds:
                    self.logger.info("Reached end of relationship list.")
                    break

                if len(usernames) >= self.max_username_scan:
                    self.logger.info(
                        f"Reached username scan cap ({len(usernames)}/{self.max_username_scan})"
                    )
                    break

                # --------------------------------------------------------
                # SCROLL
                # --------------------------------------------------------

                self.logger.info("Scrolling relationship list...")

                distance = random.uniform(0.4, 1.2)
                await scrollable.evaluate(
                    "(el, d) => el.scrollBy(0, el.clientHeight * d)",
                    distance,
                )

                await self._random_delay(page, 300)

            # ============================================================
            # COMPLETE
            # ============================================================

            self.logger.info(f"Finished extracting {list_type.lower()}.")

            self.logger.info(
                f"Collected {len(usernames)} "
                f"{list_type.lower()} for {account}."
            )

            return sorted(usernames)

        except Exception as e:

            self.logger.exception(
                f"Failed extracting {list_type} " f"for {account}: {e}"
            )

            return []

    async def _extract_post_url(
        self,
        page: Page,
    ) -> str | None:
        """
        Return the URL of the currently opened post.
        """
        try:
            return page.url

        except Exception:
            self.logger.exception("Failed to extract post URL.")
            return None

    async def _extract_post_caption(self, page: Page) -> str | None:
        """
        Extract the caption from the currently opened Instagram post.

        Returns:
            Caption text, or None if no caption exists.
        """

        try:
            dialog = page.locator("div[role='dialog']").last

            caption = dialog.locator("h1").first

            await caption.wait_for(timeout=self._random_timeout(3000))

            text = (await caption.inner_text()).strip()

            return text or None

        except Exception:
            return None

    async def _extract_post_media(self, page: Page) -> list[dict]:
        """
        Extract all media from the currently opened Instagram post.

        Returns:
            [
                {
                    "type": "image",
                    "url": "...",
                },
                {
                    "type": "video",
                    "url": "...",
                },
            ]
        """

        media = []

        try:
            dialog = page.locator("div[role='dialog']").last

            # ============================================================
            # Images
            # ============================================================

            images = dialog.locator("img[src]")

            for i in range(await images.count()):
                img = images.nth(i)

                src = await img.get_attribute("src")

                if not src:
                    continue

                # Skip profile pictures
                alt = (await img.get_attribute("alt")) or ""

                if "profile picture" in alt.lower():
                    continue

                media.append(
                    {
                        "type": "image",
                        "url": src,
                    }
                )

            # ============================================================
            # Videos
            # ============================================================

            videos = dialog.locator("video")

            for i in range(await videos.count()):
                video = videos.nth(i)

                src = await video.get_attribute("src")

                if not src:
                    continue

                media.append(
                    {
                        "type": "video",
                        "url": src,
                    }
                )

        except Exception:
            self.logger.exception("Failed to extract post media.")

        # ============================================================
        # Remove duplicates while preserving order
        # ============================================================

        unique = []
        seen = set()

        for item in media:
            key = (item["type"], item["url"])

            if key in seen:
                continue

            seen.add(key)
            unique.append(item)

        return unique

    async def _extract_post_comments(
        self,
        page: Page,
        max_comments: int = 20,
    ) -> list[dict]:
        """
        Extract top-level comments from the opened Instagram post.

        Returns:
            [
                {
                    "username": "...",
                    "comment": "...",
                },
            ]
        """

        comments = []
        seen = set()

        try:
            dialog = page.locator("div[role='dialog']").last

            await dialog.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            # ------------------------------------------------------------
            # Wait for first comment
            # ------------------------------------------------------------

            first_comment = dialog.locator(
                "li:has(time):has(a[href^='/'])"
            ).first

            await first_comment.wait_for(
                state="visible",
                timeout=self._random_timeout(5000),
            )

            # ------------------------------------------------------------
            # Find scrollable container
            # ------------------------------------------------------------

            scrollable = await first_comment.evaluate_handle("""
                node => {
                    let p = node.parentElement;

                    while (p) {
                        if (p.scrollHeight > p.clientHeight + 10)
                            return p;

                        p = p.parentElement;
                    }

                    return null;
                }
                """)

            if await scrollable.json_value() is None:
                self.logger.warning("Comment scroll container not found.")
                return []

            previous_count = 0
            stagnant_rounds = 0

            while len(comments) < max_comments and stagnant_rounds < 3:

                rows = dialog.locator("li:has(time):has(a[href^='/'])")

                before = len(comments)

                row_count = await rows.count()

                for i in range(row_count):

                    if len(comments) >= max_comments:
                        break

                    row = rows.nth(i)

                    try:

                        username = (
                            await row.locator("a[href^='/']").first.inner_text()
                        ).strip()

                        spans = row.locator("span[dir='auto']")

                        comment = None

                        for j in range(await spans.count()):

                            text = (await spans.nth(j).inner_text()).strip()

                            if (
                                text
                                and text != username
                                and text.lower() != "reply"
                                and "like" not in text.lower()
                            ):
                                comment = text
                                break

                        if not comment:
                            continue

                        key = (username, comment)

                        if key in seen:
                            continue

                        seen.add(key)

                        comments.append(
                            {
                                "username": username,
                                "comment": comment,
                            }
                        )

                    except Exception:
                        continue

                self.logger.info(f"Collected {len(comments)} comments.")

                if len(comments) == previous_count:
                    stagnant_rounds += 1
                else:
                    previous_count = len(comments)
                    stagnant_rounds = 0

                if len(comments) >= max_comments:
                    break

                # --------------------------------------------------------
                # Load more comments
                # --------------------------------------------------------

                load_more = dialog.locator(
                    'button:has(svg[aria-label="Load more comments"])'
                )

                if (
                    await load_more.count() > 0
                    and await load_more.first.is_visible()
                ):
                    self.logger.info("Loading more comments...")

                    try:
                        await load_more.first.click()
                        await self._random_delay(page, 700)
                    except Exception:
                        pass

                # --------------------------------------------------------
                # Scroll comment container
                # --------------------------------------------------------

                await scrollable.evaluate(
                    "(el) => el.scrollBy(0, el.clientHeight * 0.9)"
                )

                await self._random_delay(page, 500)

            return comments

        except Exception as e:
            self.logger.exception(f"Failed extracting comments: {e}")
            return []

    def _get_timestamp(self) -> str:
        """
        Return the current timestamp in ISO 8601 format.
        Example: 2026-07-22T15:34:12
        """
        return datetime.now().isoformat(timespec="seconds")

    def _random_timeout(self, timeout: int) -> int:
        """
        Return timeout randomized by ±50%.
        """
        return int(math.ceil(timeout * random.uniform(0.5, 1.5)))

    async def _random_delay(self, page: Page, delay_ms: int) -> None:
        """
        Wait for a randomized delay (±50%).
        """
        await page.wait_for_timeout(self._random_timeout(delay_ms))
