"""Build the offline US location menu from downloaded Census Gazetteer ZIPs."""
import csv
import io
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
YEAR = 2026
SOURCE = f'https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{YEAR}_Gazetteer/'

def rows(kind):
    path = ROOT/'data/location_sources'/f'{YEAR}_Gaz_{kind}_national.zip'
    with zipfile.ZipFile(path) as archive:
        content = archive.read(archive.namelist()[0]).decode('utf-8-sig')
    return csv.DictReader(io.StringIO(content), delimiter='|')

def main():
    states = {row['USPS']: {'code': row['USPS'], 'name': row['NAME'], 'cities': set()}
              for row in rows('state') if row['USPS'] != 'PR'}
    suffix = r' (?:city and borough|city and county|consolidated government|unified government|metropolitan government|municipality|borough|village|town|city|CDP)(?: \(balance\))?$'
    for row in rows('place'):
        if row['USPS'] in states:
            name = re.sub(suffix, '', row['NAME']).strip()
            states[row['USPS']]['cities'].add(name)
    result = sorted(states.values(), key=lambda state: state['name'])
    for state in result:
        state['cities'] = sorted(state['cities'], key=str.casefold)
    assert len(result) == 51 and all(state['cities'] for state in result)
    payload = {'country': {'code': 'US', 'name': 'United States'}, 'year': YEAR,
               'source_url': 'https://www.census.gov/geographies/reference-files/2026/geo/gazetter-file.html',
               'source_files': [SOURCE+f'{YEAR}_Gaz_{kind}_national.zip' for kind in ('state','place')],
               'states': result}
    (ROOT/'config/us_locations.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f"Saved {len(result)} states/DC and {sum(len(state['cities']) for state in result)} cities/places.")

if __name__ == '__main__':
    main()
