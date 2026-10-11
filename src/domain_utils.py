"""Normalize registrable domains using a bundled Public Suffix List."""
import ipaddress
from urllib.parse import urlsplit, urlunsplit
import tldextract

EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)

def normalize_domain(value):
    try:
        host = (urlsplit(value if "://" in value else "https://" + value).hostname or "").lower().rstrip(".")
        host = host.encode("idna").decode("ascii")
        if not host or host == "localhost":
            return ""
        try:
            ipaddress.ip_address(host)
            return ""
        except ValueError:
            pass
        parts = EXTRACT(host)
        return parts.top_domain_under_public_suffix if parts.suffix else ""
    except (ValueError, UnicodeError):
        return ""

def blocked(value, domains):
    domain = normalize_domain(value)
    host = (urlsplit(value if "://" in value else "https://" + value).hostname or "").lower()
    return not domain or any(host == d or host.endswith("." + d) or domain == d for d in domains)

def website_url(value):
    parsed = urlsplit(value if "://" in value else "https://" + value)
    return urlunsplit((parsed.scheme if parsed.scheme in ("http", "https") else "https", parsed.netloc, "/", "", ""))

