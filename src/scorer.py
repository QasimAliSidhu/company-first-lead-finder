from .email_classifier import classify

def score(signals, emails, config):
    present = set(signals)
    kinds = {classify(e) for e in emails}
    if "RECRUITING_HIGH" in kinds:
        present.add("recruiting_email")
    if "GENERAL" in kinds:
        present.add("general_email")
    contributions = {k: v for k, v in config["weights"].items() if k in present}
    total = sum(contributions.values())
    priority = "HIGH" if total >= config["thresholds"]["HIGH"] else "MEDIUM" if total >= config["thresholds"]["MEDIUM"] else "LOW"
    return total, priority, contributions

