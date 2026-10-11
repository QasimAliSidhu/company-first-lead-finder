import re
from html import unescape
from urllib.parse import unquote

PATTERN = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63})(?![\w.-])", re.I)
def extract(text):
    decoded = unquote(unescape(text))
    result = set()
    for match in PATTERN.findall(decoded):
        email = match.lower().strip(".")
        domain = email.split("@")[1]
        if domain.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".woff", ".js", ".css")):
            continue
        if domain in {"example.com","example.org","domain.com","email.com","yourcompany.com","sentry.io"} or domain.endswith(".sentry.io"):
            continue
        result.add(email)
    return sorted(result)

