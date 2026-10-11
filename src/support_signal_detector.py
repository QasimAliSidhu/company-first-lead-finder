"""Match site evidence only; query categories do not earn points."""
import re

PATTERNS = {
    "managed_it": r"\bmanaged\s+(?:it|services)|\bmanaged service provider|\bmsp\b",
    "software": r"\bsaas\b|software(?:\s+as\s+a\s+service)?|cloud software",
    "technical_support": r"technical support|technical assistance|tech support",
    "support_team": r"help\s?desk|service desk|support (?:team|engineer|specialist|analyst)",
    "support_24_7": r"(?:24\s*[/x×]\s*7|round.the.clock)\s*(?:technical\s+|customer\s+)?support",
    "remote_support": r"remote (?:technical )?support",
    "help_center": r"help cent(?:er|re)",
    "knowledge_base": r"knowledge[\s-]*base",
    "support_portal": r"support portal|submit a ticket|ticketing|customer portal",
    "api_docs": r"api documentation|api reference|developer documentation",
    "remote_team": r"(?:fully|all)[ -]remote|distributed team|remote.first|remote team",
}
EXTRA = r"customer support|customer success|cloud|hosting|cybersecurity|voip|telecommunications"

def detect(pages, useful_pages):
    evidence = {}
    phrases = set()
    for page in pages:
        text = page["text"]
        for key, pattern in PATTERNS.items():
            match = re.search(pattern, text, re.I)
            if match:
                evidence.setdefault(key, []).append({"url": page["url"], "phrase": match.group(0)})
        phrases.update(m.group(0).lower() for m in re.finditer(EXTRA, text, re.I))
        # Vacancy points require an explicit opening/apply signal on a careers/jobs page.
        if re.search(r"/(?:careers|jobs|work-with-us)(?:/|$)", page["url"], re.I):
            role = r"(?:technical support (?:engineer|specialist|analyst|representative)|support (?:engineer|specialist|analyst))"
            opening = r"(?:apply now|apply for|open position|current opening|job opening)"
            if re.search(role+r".{0,250}"+opening+r"|"+opening+r".{0,250}"+role, text, re.I):
                evidence.setdefault("technical_support_vacancy", []).append({"url":page["url"],"phrase":"support role + application/opening text; review required"})
    for key in ("help_center","knowledge_base","careers_page"):
        if useful_pages.get(key):
            evidence.setdefault(key, []).append({"url":useful_pages[key],"phrase":"page link"})
    return evidence, sorted(phrases)

