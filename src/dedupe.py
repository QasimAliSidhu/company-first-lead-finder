from .domain_utils import normalize_domain, blocked, website_url

def deduplicate(records, blocklist):
    merged = {}
    for record in records:
        value = record.get("website", "")
        domain = normalize_domain(value)
        if blocked(value, blocklist):
            continue
        record = dict(record, domain=domain, website=website_url(value))
        if domain not in merged:
            record["public_emails"] = list(set(record.get("public_emails", [])))
            record["discovery_queries"] = [record.get("discovery_query", "")]
            record['search_categories'] = [record.get('category','')]
            merged[domain] = record
            continue
        target = merged[domain]
        # Prefer Google Maps' verified business name/address over the SERP title/query location.
        if record.get("discovery_source") == "google_maps":
            for key in ("company_name","location","phone","category"):
                if record.get(key):
                    target[key] = record[key]
            target["location_evidence"] = "Google Maps business address"
        for key in ("discovery_source","source_url"):
            target[key] = "; ".join(sorted(set(target.get(key, "").split("; ") + record.get(key, "").split("; "))))
        target["public_emails"] = sorted(set(target["public_emails"] + record.get("public_emails", [])))
        for email, urls in record.get('email_evidence', {}).items():
            evidence = target.setdefault('email_evidence', {})
            evidence[email] = sorted(set(evidence.get(email, []) + urls))
        target["discovery_queries"] = sorted(set(target["discovery_queries"] + [record.get("discovery_query","")]))
        target['search_categories'] = sorted(set(target['search_categories'] + [record.get('category','')]))
    return list(merged.values())

