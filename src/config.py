"""Project-local configuration."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def lines(name):
    return [s.strip() for s in (ROOT / "config" / name).read_text(encoding="utf-8-sig").splitlines()
            if s.strip() and not s.lstrip().startswith("#")]

def load():
    settings = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))
    settings["scoring"] = json.loads((ROOT / "config/scoring.json").read_text(encoding="utf-8-sig"))
    settings["categories"] = lines("categories.txt")
    settings["locations"] = lines("locations.txt")
    settings["blocked_domains"] = set(lines("block_domains.txt"))
    settings["search_templates"] = lines("search_templates.txt")
    return settings

