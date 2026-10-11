"""Optional native Crawl4AI rendering; existing bounded contact crawler stays in control."""
import asyncio
import logging
import os
from urllib.parse import urlsplit
from .config import ROOT
from .domain_utils import normalize_domain
from .website_crawler import WebsiteCrawler, public_url

os.environ.setdefault('CRAWL4_AI_BASE_DIRECTORY', str(ROOT/'.runtime/crawl4ai'))

class Crawl4AICrawler(WebsiteCrawler):
    def __init__(self, settings):
        super().__init__(settings)
        self.loop = asyncio.new_event_loop()
        self.browser = None
        self.domain = ''
        self.rendered_pages = []
        self.render_errors = []

    async def render(self, url):
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
        if self.browser is None:
            self.browser = AsyncWebCrawler(config=BrowserConfig(
                headless=True, chrome_channel='chrome', verbose=False,
                ignore_https_errors=False, user_agent=self.settings['user_agent'],
                extra_args=['--disable-background-networking']))

            async def guard(page, context, **kwargs):
                async def route_filter(route):
                    request = route.request
                    try:
                        permitted = request.method in ('GET','HEAD') and await asyncio.to_thread(public_url, request.url)
                        if request.is_navigation_request():
                            permitted = permitted and normalize_domain(request.url) == self.domain
                            if permitted and self.settings['respect_robots']:
                                permitted = await asyncio.to_thread(self.allowed, request.url)
                        if not permitted or request.resource_type in ('image','media','font'):
                            await route.abort()
                        else:
                            await route.continue_()
                    except Exception:
                        await route.abort()
                await context.route('**/*', route_filter)
                return page

            self.browser.crawler_strategy.set_hook('on_page_context_created', guard)
            await self.browser.start()
        config = CrawlerRunConfig(cache_mode=CacheMode.DISABLED,
            wait_until='domcontentloaded', page_timeout=self.settings['timeout_seconds']*1000,
            delay_before_return_html=1.0, verbose=False, check_robots_txt=False)
        # Robots are checked fail-closed by the parent and navigation guard above.
        result = await asyncio.wait_for(self.browser.arun(url=url, config=config),
                                        timeout=self.settings['timeout_seconds']+5)
        if not result.success or (result.status_code and result.status_code >= 400):
            raise ValueError(str(result.error_message or f'HTTP {result.status_code}')[:180])
        final_url = result.redirected_url or result.url
        if normalize_domain(final_url) != self.domain:
            raise ValueError('Browser redirect left company domain')
        if len(result.html.encode('utf-8')) > self.settings['max_bytes']:
            raise ValueError('Rendered page exceeds max_bytes')
        return final_url, result.html

    def fetch(self, url, max_bytes=None):
        final_url, html = super().fetch(url, max_bytes)
        if urlsplit(url).path == '/robots.txt':
            return final_url, html
        try:
            rendered = self.loop.run_until_complete(self.render(final_url))
            self.rendered_pages.append(rendered[0])
            return rendered
        except Exception as error:
            self.render_errors.append(final_url + ': Crawl4AI failed; static HTML retained: ' + str(error)[:160])
            logging.warning('%s', self.render_errors[-1])
            return final_url, html

    def crawl(self, website):
        self.domain = normalize_domain(website)
        try:
            result = super().crawl(website)
            result['rendered_pages'] = self.rendered_pages
            result['crawl_errors'].extend(self.render_errors)
            return result
        finally:
            if self.browser is not None:
                self.loop.run_until_complete(self.browser.close())
            self.loop.close()
