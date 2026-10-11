"""Run Scrapy in a separate process so every collection gets a fresh reactor."""
import argparse
import asyncio
import json
from pathlib import Path

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest

from .config import load
from .directory_sources import SOURCES, extract_profile
from .domain_utils import normalize_domain
from .website_crawler import WebsiteCrawler, public_url

class PublicDirectoryGuard:
    """Use the existing fail-closed robots policy on initial and redirected requests."""
    def __init__(self):
        self.checker = WebsiteCrawler(load()['crawl'])

    async def process_request(self, request, spider=None):
        if normalize_domain(request.url) not in {s[0] for s in SOURCES.values()}:
            raise IgnoreRequest('Redirect left supported directory domains')
        if not await asyncio.to_thread(public_url, request.url):
            raise IgnoreRequest('Directory URL is not public')
        if url_is_robots(request.url):
            return None
        if not await asyncio.to_thread(self.checker.allowed, request.url):
            raise IgnoreRequest('robots.txt disallows or unavailable')

def url_is_robots(url):
    from urllib.parse import urlsplit
    return urlsplit(url).path == '/robots.txt'

class DirectorySpider(scrapy.Spider):
    name = 'business_directories'
    allowed_domains = [s[0] for s in SOURCES.values()]

    def __init__(self, seeds, output, **kwargs):
        super().__init__(**kwargs)
        self.seeds, self.output = seeds, Path(output)
        self.records, self.errors = [], []
        self.finish_reason = 'running'

    async def start(self):
        # The client combines all query contexts for a URL, so download it only once.
        for entry in self.seeds:
            yield scrapy.Request(entry['url'], callback=self.parse, errback=self.failed,
                                 meta={'contexts': entry['contexts']})

    def parse(self, response):
        if 'html' not in response.headers.get('Content-Type', b'').decode().lower():
            self.errors.append(dict(url=response.url, error='Non-HTML profile'))
            return
        found = False
        for seed in response.meta['contexts']:
            record = extract_profile(response.text, response.url, seed, load()['blocked_domains'])
            if record:
                self.records.append(record)
                found = True
        if not found:
            self.errors.append(dict(url=response.url, error='No unambiguous public business website in profile'))
        self.save()

    def failed(self, failure):
        self.errors.append(dict(url=failure.request.url, error=str(failure.value)[:240]))
        self.save()

    def save(self):
        temporary = self.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(records=self.records, errors=self.errors, finish_reason=self.finish_reason), indent=2), encoding='utf-8')
        temporary.replace(self.output)

    def closed(self, reason):
        self.finish_reason = reason
        self.save()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seeds', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--log', required=True, type=Path)
    args = parser.parse_args()
    config = load()['crawl']
    process = CrawlerProcess({
        'USER_AGENT': config['user_agent'], 'ROBOTSTXT_OBEY': True,
        'DOWNLOADER_MIDDLEWARES': {'src.directory_worker.PublicDirectoryGuard': 40},
        'CONCURRENT_REQUESTS': 2, 'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
        'DOWNLOAD_DELAY': max(1, config['delay_seconds']), 'DOWNLOAD_TIMEOUT': config['timeout_seconds'],
        'DOWNLOAD_MAXSIZE': config['max_bytes'], 'RETRY_TIMES': 1,
        'COOKIES_ENABLED': False, 'TELNETCONSOLE_ENABLED': False, 'REMOTE_CONTROL_ENABLED': False,
        'CLOSESPIDER_TIMEOUT': 240, 'LOG_FILE': str(args.log), 'LOG_LEVEL': 'INFO',
    })
    crawler = process.create_crawler(DirectorySpider)
    process.crawl(crawler, seeds=json.loads(args.seeds.read_text(encoding='utf-8')), output=str(args.output))
    process.start()
    if not args.output.exists():
        raise RuntimeError('Directory worker did not produce a report; see directories.log')

if __name__ == '__main__':
    main()
