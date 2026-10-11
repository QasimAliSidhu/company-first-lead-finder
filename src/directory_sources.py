"""Directory profiles are evidence; only explicit business websites become leads."""
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from .domain_utils import blocked, normalize_domain
from .email_extractor import extract as extract_emails

SOURCES = {
    'yelp': ('yelp.com', '/biz/'),
    'yellowpages': ('yellowpages.com', '/'),
    'clutch': ('clutch.co', '/profile/'),
    'goodfirms': ('goodfirms.co', '/company/'),
}

def indexed_business_name(result, source, location):
    """Extract a brand from an indexed profile title, never from review authors."""
    title = result.get('title', '').strip()
    name = re.split(r'\s+Reviews?\s*\(|\s+(?:Reviews?,|Services & Company Info)|\s+[–—|]|\s+-\s+', title, maxsplit=1, flags=re.I)[0]
    if re.match(r'^(?:updated|best|top|find|search)\b', name, re.I) or not name:
        return ''
    ignored = {'it','llc','inc','com','company','services','service','support','managed','solutions','of','the','and','tx'} | set(re.findall(r'[a-z0-9]+', location.casefold()))
    if not any(len(t) >= 4 and t not in ignored for t in re.findall(r'[a-z0-9]+', name.casefold())):
        return ''
    return name[:120]

def indexed_website(result, name, location, blocklist):
    """Require a distinctive brand match plus the requested city in search evidence."""
    value = result.get('url', '')
    if not external_website(value, '', blocklist):
        return ''
    text = ' '.join(str(result.get(k, '')) for k in ('title', 'snippet', 'url')).casefold()
    city = location.rsplit(' ', 1)[0].casefold()
    if city not in text:
        return ''
    ignored = {'it','llc','inc','com','company','services','service','support','managed','solutions','of','the','and','tx'} | set(re.findall(r'[a-z0-9]+', city))
    tokens = [t for t in re.findall(r'[a-z0-9]+', name.casefold()) if t not in ignored and len(t) >= 4]
    if not tokens:
        return ''
    label = normalize_domain(value).split('.')[0].replace('-', '')
    title = result.get('title', '').casefold()
    matched = any(t in label for t in tokens) or all(re.search(r'\b' + re.escape(t) + r'\b', title) for t in tokens)
    if not matched:
        return ''
    path = urlsplit(value).path.casefold()
    if re.search(r'/(?:blog|news|articles|directory|reviews|jobs|careers)(?:/|$)', path):
        return ''
    return value

def profile_url(value, source):
    """Reject directory home/search pages and arbitrary SERP URLs."""
    try:
        parsed = urlsplit(value)
        domain, prefix = SOURCES[source]
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return ''
        if normalize_domain(value) != domain:
            return ''
        if source == 'yellowpages':
            matches = re.match(r'^/[^/]+/mip/[^/]+', parsed.path)
        else:
            matches = parsed.path.startswith(prefix) and len(parsed.path) > len(prefix)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')) if matches else ''
    except (ValueError, KeyError):
        return ''

def external_website(value, source, blocklist):
    """Unwrap Yelp business redirects without requesting the redirect endpoint."""
    if not isinstance(value, str):
        return ''
    try:
        parsed = urlsplit(value)
        if normalize_domain(value) == 'yelp.com' and parsed.path == '/biz_redir':
            value = parse_qs(parsed.query).get('url', [''])[0]
            parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return ''
        if blocked(value, set(blocklist) | {entry[0] for entry in SOURCES.values()}):
            return ''
        return value
    except ValueError:
        return ''

def business_objects(value):
    """Traverse JSON-LD graphs, excluding publisher/review author organizations."""
    if isinstance(value, list):
        for item in value:
            yield from business_objects(item)
    elif isinstance(value, dict):
        types = value.get('@type', [])
        types = types if isinstance(types, list) else [types]
        if any(t in {'LocalBusiness', 'Organization', 'Corporation', 'ProfessionalService', 'Restaurant',
                     'Store', 'Dentist', 'MedicalBusiness', 'HomeAndConstructionBusiness', 'AutoRepair'} for t in types):
            yield value
        for key in ('@graph', 'mainEntity', 'itemListElement', 'item'):
            if key in value:
                yield from business_objects(value[key])

def extract_profile(html, url, seed, blocklist):
    source = seed['source']
    if not profile_url(url, source):
        return None
    soup = BeautifulSoup(html, 'lxml')
    heading = soup.find('h1')
    name = heading.get_text(' ', strip=True) if heading else ''
    # A profile heading is essential: do not extract footer links on error/search pages.
    if not name:
        return None
    candidates, phone, address, emails = [], '', '', set()
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            objects = business_objects(json.loads(script.get_text()))
            for obj in objects:
                obj_name = obj.get('name', '')
                if not isinstance(obj_name, str) or not obj_name or obj_name.casefold() not in name.casefold():
                    continue
                for value in (obj.get('url'), obj.get('sameAs', [])):
                    for item in value if isinstance(value, list) else [value]:
                        target = external_website(item, source, blocklist)
                        if target:
                            candidates.append(target)
                phone = obj.get('telephone', '') or phone
                contact_points = obj.get('contactPoint', [])
                contact_points = contact_points if isinstance(contact_points, list) else [contact_points]
                published = [obj.get('email', '')] + [point.get('email', '') for point in contact_points if isinstance(point, dict)]
                for value in published:
                    if isinstance(value, str):
                        emails.update(extract_emails(value))
                adr = obj.get('address')
                if isinstance(adr, list):
                    adr = next((item for item in adr if isinstance(item, dict)), None)
                if isinstance(adr, dict):
                    address = ', '.join(str(adr[k]) for k in ('streetAddress', 'addressLocality', 'addressRegion', 'postalCode') if adr.get(k))
        except (ValueError, TypeError):
            continue
    structured_domains = {normalize_domain(item) for item in candidates}
    for link in soup.select('a[href]'):
        if link.find_parent(['footer', 'nav']):
            continue
        link_company = link.get('data-company-name', '')
        if link_company and link_company.casefold() != name.casefold():
            continue
        text = link.get_text(' ', strip=True)
        classes = ' '.join(link.get('class', []))
        label = text + ' ' + link.get('aria-label', '') + ' ' + link.get('title', '')
        href = urljoin(url, link['href'])
        explicit = (re.search(r'\b(?:visit (?:the )?website|business website|company website|official website|website)\b', label, re.I)
                    or source == 'yellowpages' and 'website-link' in classes
                    or source == 'clutch' and 'website' in classes
                    or source == 'yelp' and urlsplit(href).path == '/biz_redir')
        if explicit:
            target = external_website(href, source, blocklist)
            if target and (not structured_domains or normalize_domain(target) in structured_domains):
                candidates.append(target)
    # Multiple unrelated website links are ambiguous. Avoid merging companies incorrectly.
    domains = {normalize_domain(item) for item in candidates}
    if len(domains) != 1:
        return None
    return dict(company_name=name, website=candidates[0], category=seed['category'],
                location=address or seed['location'], phone=str(phone), public_emails=sorted(emails),
                email_evidence={email: [url] for email in sorted(emails)},
                location_evidence=f'{source} profile address' if address else 'Directory search query only; business location unverified',
                discovery_source='directory:' + source, source_url=url,
                discovery_query=seed.get('query', ''), directory_search_location=seed['location'])
