import asyncio

from playwright.async_api import Playwright, async_playwright


async def test_foo(playwright: Playwright):
    browser = await playwright.chromium.launch(
        headless=False, args=["--start-maximized"]
    )
    # create a new incognito browser context.
    context = await browser.new_context(no_viewport=True)
    # create a new page in a pristine context.
    page = await context.new_page()
    await page.goto("https://example.com")
    await page.wait_for_timeout(3000)  # Wait to see the page
    await browser.close()


async def main():
    async with async_playwright() as playwright:
        await test_foo(playwright)


if __name__ == "__main__":
    asyncio.run(main())
