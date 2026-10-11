import csv
import json
from datetime import datetime, timezone
from .email_classifier import classify

COLUMNS = ["company_name","domain","website","category","location","phone","public_email","email_type",
           "contact_page","careers_page","support_page","help_center","knowledge_base",
           "technical_support_detected","remote_support_detected","support_signals",
           "technical_support_score","priority","discovery_source","source_url",
           "social_links","email_source_urls","location_evidence","search_categories","discovery_queries","notes","collected_at"]

def export(records, path):
    rows = 0
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w",encoding="utf-8-sig",newline="") as handle:
        writer = csv.DictWriter(handle,fieldnames=COLUMNS)
        writer.writeheader()
        for record in sorted(records,key=lambda r:(-r["technical_support_score"],r["domain"])):
            for email in record.get("public_emails",[]) or [""]:
                row = {key:record.get(key,"") for key in COLUMNS}
                row.update(public_email=email,email_type=classify(email) if email else "",
                           email_source_urls="; ".join(record.get("email_evidence",{}).get(email,[])))
                for key,value in row.items():
                    if isinstance(value,(list,dict)):
                        row[key] = json.dumps(value,ensure_ascii=False) if isinstance(value,dict) else "; ".join(str(v) for v in value)
                    if isinstance(row[key],str) and row[key].lstrip().startswith(("=","+","-","@")):
                        row[key] = "'" + row[key] # avoid spreadsheet formula execution
                writer.writerow(row)
                rows += 1
    temporary.replace(path)
    return rows

