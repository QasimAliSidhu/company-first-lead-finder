"""Project-local US state/city choices; no external requests when using the UI."""
import json
from functools import lru_cache
from .config import ROOT

@lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT/'config/us_locations.json').read_text(encoding='utf-8'))

@lru_cache(maxsize=1)
def valid_locations():
    return frozenset(f"{city} {state['code']}" for state in catalog()['states'] for city in state['cities'])
