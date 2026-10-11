GROUPS = {
    "RECRUITING_HIGH": {"careers","career","jobs","recruiting","recruitment","talent","hr","people"},
    "GENERAL": {"hello","contact","info"},
    "SUPPORT": {"support","help","helpdesk"},
    "SALES": {"sales"},
}
def classify(email):
    local = email.lower().split("@", 1)[0]
    for kind, prefixes in GROUPS.items():
        if local in prefixes:
            return kind
    return "UNKNOWN"

