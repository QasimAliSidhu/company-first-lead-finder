import csv
import os
import subprocess
from pathlib import Path
from .config import ROOT
from .email_extractor import extract

def run(query_file, output_file, settings):
    environment = os.environ.copy()
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".runtime/playwright")
    environment["DISABLE_TELEMETRY"] = "1"
    command = [str(ROOT / "bin/google-maps-scraper.exe"), "-input", str(query_file),
               "-results", str(output_file), "-depth", str(settings["depth"]),
               "-c", str(settings["concurrency"]), "-pages-per-browser", str(settings.get("pages_per_browser", 2)), "-lang", "en",
               "-exit-on-inactivity", settings["exit_on_inactivity"]]
    if settings["extract_emails"]:
        command.append("-email")
    log_directory = Path(settings.get('log_directory',ROOT/'logs'))
    log_directory.mkdir(parents=True,exist_ok=True)
    with (log_directory/'maps.log').open("a", encoding="utf-8") as log:
        subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=log,
                       timeout=settings["timeout_seconds"], check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return read(output_file)

def read(path):
    records = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("website"):
                continue
            records.append({
                "company_name": row.get("title",""),
                "website": row["website"], "category": row.get("category",""),
                "location": row.get("address",""), "phone": row.get("phone",""),
                "public_emails": extract(row.get("emails","")),
                "discovery_source":"google_maps", "source_url":row.get("link",""),
                "discovery_query":row.get("input_id",""),
                "location_evidence":"Google Maps business address",
                "latitude":row.get("latitude",""), "longitude":row.get("longitude",""),
            })
    return records

