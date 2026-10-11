"""Bounded crawler for public business pages. No login or email guessing."""
import ipaddress
import re
import socket
import time
from collections import deque
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .domain_utils import normalize_domain
from .email_extractor import extract

PAGE_PATTERNS = {
    "contact_page":r"contact|contact-us",
    "careers_page":r"careers|jobs|work-with-us|join-us",
    "support_page":r"support|helpdesk|help-desk|service-desk",
    "help_center":r"help[-/]?center|help[-/]?centre|/help(?:/|$)",
    "knowledge_base":r"knowledge[-/]?base|/kb(?:/|$)",
    "api_docs":r"/docs(?:/|$)|documentation|developer|api-reference",
}
SOCIAL_HOSTS = ("linkedin.com","facebook.com","instagram.com","twitter.com","x.com","youtube.com")
SEEDS = ["/contact","/contact-us","/about","/about-us","/company","/team","/careers","/jobs",
         "/work-with-us","/support","/help","/help-center","/knowledge-base"]

def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.port not in (None,80,443):
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (OSError, ValueError):
        return False

class WebsiteCrawler:
    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings["user_agent"]
        retries = Retry(total=settings["retries"], backoff_factor=1,
                        status_forcelist=[429,500,502,503,504], allowed_methods=["GET"],
                        respect_retry_after_header=False)
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.mount("http://", HTTPAdapter(max_retries=retries))
        self.robots = {}
        self.last_request = {}

    def fetch(self, url, max_bytes=None):
        for _ in range(6):
            if not public_url(url):
                raise ValueError("Not a public HTTP(S) URL")
            origin = urlunsplit((*urlsplit(url)[:2], "", "", ""))
            delay = self.settings["delay_seconds"]
            rules = self.robots.get(origin)
            if rules and rules.crawl_delay(self.settings["user_agent"]):
                delay = max(delay, rules.crawl_delay(self.settings["user_agent"]))
            remaining = delay - (time.monotonic() - self.last_request.get(origin,0))
            if remaining > 0:
                time.sleep(remaining)
            self.last_request[origin] = time.monotonic()
            with self.session.get(url, timeout=self.settings["timeout_seconds"], allow_redirects=False, stream=True) as response:
                if response.is_redirect or response.is_permanent_redirect:
                    url = urljoin(url, response.headers["Location"])
                    if self.settings["respect_robots"] and urlsplit(url).path != "/robots.txt" and not self.allowed(url):
                        raise ValueError("Redirect blocked by robots.txt")
                    continue
                response.raise_for_status()
                content_type = response.headers.get("Content-Type","").lower()
                if "html" not in content_type and not url.endswith("/robots.txt"):
                    raise ValueError("Non-HTML page")
                data = bytearray()
                limit = max_bytes or self.settings["max_bytes"]
                for chunk in response.iter_content(16384):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("Page exceeds max_bytes")
                encoding = response.encoding if response.encoding and response.encoding.lower() != "iso-8859-1" else "utf-8"
                return url, data.decode(encoding, errors="replace")
        raise ValueError("Too many redirects")

    def allowed(self, url):
        origin = urlunsplit((*urlsplit(url)[:2],"","",""))
        if origin not in self.robots:
            rules = RobotFileParser()
            rules.set_url(origin + "/robots.txt")
            try:
                _, text = self.fetch(origin + "/robots.txt", 250000)
                rules.parse(text.splitlines())
            except requests.HTTPError as error:
                if error.response.status_code == 404:
                    rules.parse([])
                else:
                    rules.disallow_all = True
            except (requests.RequestException, ValueError, OSError):
                rules.disallow_all = True
            self.robots[origin] = rules
        return self.robots[origin].can_fetch(self.settings["user_agent"], url)

    def crawl(self, website):
        queue = deque([website])
        seen, pages, emails, useful, socials, phones, errors = set(), [], {}, {}, set(), set(), []
        domain = normalize_domain(website)
        start = time.monotonic()
        fallback_added = False
        name = ""
        while queue and len(seen) < self.settings["max_pages"]:
            if time.monotonic() - start > self.settings["max_company_seconds"]:
                errors.append("Company crawl time budget reached")
                break
            url = queue.popleft()
            if url in seen or normalize_domain(url) != domain:
                continue
            seen.add(url)
            try:
                if self.settings["respect_robots"] and not self.allowed(url):
                    errors.append(url + ": robots.txt disallows or unavailable")
                    continue
                final_url, html = self.fetch(url)
                if normalize_domain(final_url) != domain:
                    errors.append(url + ": redirect to another domain")
                    continue
                soup = BeautifulSoup(html, "lxml")
                if not pages:
                    brand = soup.find("meta", attrs={"property":"og:site_name"})
                    name = brand.get("content","").strip() if brand else ""
                    if not name:
                        org = soup.find(attrs={"itemprop":"name"})
                        name = org.get_text(" ",strip=True) if org else ""
                # Ignore scripts/styles so tracking code and asset names aren't email evidence.
                for tag in soup(["script","style","noscript","svg"]):
                    tag.decompose()
                text = soup.get_text(" ",strip=True)
                mailtos = " ".join(a.get("href","") for a in soup.select('a[href^="mailto:"]'))
                for email in extract(text + " " + mailtos):
                    emails.setdefault(email,[]).append(final_url)
                pages.append({"url":final_url,"text":text})
                for key, pattern in PAGE_PATTERNS.items():
                    if re.search(pattern, urlsplit(final_url).path,re.I):
                        useful.setdefault(key,final_url)
                preferred = []
                for link in soup.select("a[href]"):
                    href = link["href"]
                    if href.lower().startswith("tel:"):
                        phones.add(href[4:].split("?")[0])
                        continue
                    target = urljoin(final_url, href)
                    parsed = urlsplit(target)
                    if parsed.scheme not in ("http","https"):
                        continue
                    target = urlunsplit((parsed.scheme,parsed.netloc,parsed.path or "/","",""))
                    host = (parsed.hostname or "").lower()
                    if any(host == d or host.endswith("." + d) for d in SOCIAL_HOSTS):
                        socials.add(target)
                    matches = [key for key, pattern in PAGE_PATTERNS.items()
                               if re.search(pattern, parsed.path + " " + link.get_text(" ",strip=True),re.I)]
                    if matches:
                        for key in matches:
                            useful.setdefault(key,target)
                        if normalize_domain(target) == domain:
                            preferred.append(target)
                for target in reversed(list(dict.fromkeys(preferred))):
                    if target not in seen:
                        queue.appendleft(target)
                if not fallback_added:
                    queue.extend(urljoin(final_url,p) for p in SEEDS)
                    fallback_added = True
            except (requests.RequestException, ValueError, OSError) as error:
                errors.append(url + ": " + str(error)[:200])
        self.session.close()
        return {"pages":pages,"public_emails":sorted(emails),"email_evidence":emails,
                "useful_pages":useful,"social_links":sorted(socials),"phones":sorted(phones),
                "crawl_errors":errors,"site_company_name":name}

