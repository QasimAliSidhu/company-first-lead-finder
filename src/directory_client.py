"""Find indexed business profiles and collect their published company websites."""
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time

from .config import ROOT
from .directory_sources import SOURCES, profile_url, indexed_business_name, indexed_website
from .domain_utils import normalize_domain

def search_profiles(settings, client, query, engine, limit):
    if engine != 'ddgs':
        return client.search(query, engine, limit)
    from ddgs import DDGS
    raw = DDGS(timeout=15).text(query, region='us-en', max_results=limit,
                              backend=settings.get('directories', {}).get('metasearch_backends', 'yahoo,duckduckgo,bing,brave,startpage,yandex,google,mojeek'))
    return raw, [dict(title=r.get('title',''),url=r.get('href',''),snippet=r.get('body','')) for r in raw if r.get('href')]

def search_engines(settings):
    engines = list(settings['openserp']['engines'])
    if settings.get('directories', {}).get('metasearch', True):
        engines.append('ddgs')
    return list(dict.fromkeys(engines))

def discover(settings, client, report, run_root, sources):
    if importlib.util.find_spec('scrapy') is None:
        raise RuntimeError('Scrapy missing; install requirements.txt')
    config = settings.get('directories', {})
    maximum = config.get('profiles_per_query', 15)
    unavailable = {s['engine'] for s in report.get('searches', []) if '429' in s.get('error', '')}
    seeds = {}
    searches = report.setdefault('directory_searches', [])
    for location in settings['locations']:
        for category in settings['categories']:
            term = settings.get('industry_search_terms', {}).get(category, category)
            for source in sources:
                domain, prefix = SOURCES[source]
                query = f'site:{domain}{prefix if source != "yellowpages" else ""} {term} {location}'
                source_urls = set()
                for engine in search_engines(settings):
                    if engine in unavailable:
                        continue
                    try:
                        payload, results = search_profiles(settings, client, query, engine, settings['openserp']['results_per_query'])
                        digest = hashlib.sha1((query + engine).encode()).hexdigest()[:12]
                        (run_root/'data/raw'/f'directory_{source}_{engine}_{digest}.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
                        added = 0
                        for result in results:
                            url = profile_url(result.get('url', ''), source)
                            if not url or url in source_urls or len(source_urls) >= maximum:
                                continue
                            # Keep city relevance, even when the result names an unfamiliar industry synonym.
                            city = location.rsplit(' ', 1)[0].casefold()
                            if city not in (' '.join(str(result.get(k, '')) for k in ('title', 'snippet', 'url'))).casefold():
                                continue
                            source_urls.add(url)
                            seeds.setdefault(url, []).append(dict(source=source, category=category, location=location, query=query,
                                                                  indexed_title=result.get('title', '')))
                            added += 1
                        searches.append(dict(source=source, engine=engine, query=query, profiles=added))
                        logging.info('%s directory search via %s: %d profile candidates for %s / %s', source, engine, added, category, location)
                    except Exception as error:
                        if getattr(getattr(error, 'response', None), 'status_code', None) == 429:
                            unavailable.add(engine)
                        searches.append(dict(source=source, engine=engine, query=query, error=str(error)))
                        logging.warning('%s directory search via %s failed: %s', source, engine, error)
                    time.sleep(settings['openserp']['query_delay_seconds'])
                    if len(source_urls) >= maximum:
                        break
    report['directory_profiles_requested'] = len(seeds)
    if not seeds:
        report['directory_companies'] = 0
        return []
    seed_path = run_root/'queries/directory_profiles.json'
    seed_path.write_text(json.dumps([dict(url=url, contexts=contexts) for url, contexts in seeds.items()], indent=2), encoding='utf-8')
    output = run_root/'data/directory_discovery.json'
    # Reset this run's output so a worker startup failure cannot reuse stale profile data.
    output.write_text(json.dumps(dict(records=[], errors=[], finish_reason='starting')), encoding='utf-8')
    logging.info('Directory profile scraping started with Scrapy: %d profiles.', len(seeds))
    try:
        process = subprocess.run([sys.executable, '-m', 'src.directory_worker', '--seeds', str(seed_path),
                                  '--output', str(output), '--log', str(run_root/'logs/directories.log')],
                                 cwd=ROOT, timeout=config.get('timeout_seconds', 300),
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if process.returncode:
            report['errors'].append('Directory worker exited with code ' + str(process.returncode))
    except subprocess.TimeoutExpired:
        report['errors'].append('Directory scraping time limit reached; keeping completed profiles')
    payload = json.loads(output.read_text(encoding='utf-8')) if output.exists() else dict(records=[], errors=[])
    report['directory_errors'] = payload['errors']
    reason = payload.get('finish_reason', 'unknown')
    report['directory_finish_reason'] = reason
    if reason not in ('finished', 'unknown'):
        report['errors'].append('Directory collection incomplete: ' + reason)
    records = payload['records']
    if config.get('indexed_fallback', True):
        records.extend(resolve_indexed_profiles(settings, client, seeds, records, unavailable, report, run_root))
        payload['records'] = records
        output.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    report['directory_companies'] = len({r['website'] for r in records})
    report['directory_source_counts'] = {source: len({normalize_domain(r['website']) for r in records if r['discovery_source'] in ('directory:' + source, 'directory_index:' + source)}) for source in sources}
    logging.info('Directories returned %d company websites (%d unavailable or unextractable profiles).', report['directory_companies'], len(payload['errors']))
    return records

def resolve_indexed_profiles(settings, client, seeds, records, unavailable, report, run_root):
    """Recover company leads through public search evidence when profiles are inaccessible."""
    completed = {r['source_url'] for r in records}
    counts = {}
    recovered = []
    maximum = settings.get('directories', {}).get('fallback_profiles_per_source', 5)
    searches = report.setdefault('directory_resolution_searches', [])
    for url, contexts in seeds.items():
        if url in completed:
            continue
        seed = contexts[0]
        source = seed['source']
        if counts.get(source, 0) >= maximum:
            continue
        name = indexed_business_name({'title':seed.get('indexed_title','')}, source, seed['location'])
        if not name:
            continue
        counts[source] = counts.get(source, 0) + 1
        query = f'"{name}" {seed["location"]} -site:{SOURCES[source][0]}'
        for engine in search_engines(settings):
            if engine in unavailable:
                continue
            try:
                raw, results = search_profiles(settings, client, query, engine, 10)
                digest = hashlib.sha1((query + engine).encode()).hexdigest()[:12]
                (run_root/'data/raw'/f'directory_resolution_{source}_{engine}_{digest}.json').write_text(json.dumps(raw, indent=2), encoding='utf-8')
                matches = [r for r in results if indexed_website(r, name, seed['location'], settings['blocked_domains'])]
                domains = {normalize_domain(r['url']) for r in matches}
                searches.append(dict(profile_url=url, engine=engine, query=query, matched_domains=len(domains)))
                if len(domains) == 1:
                    for context in contexts:
                        recovered.append(dict(company_name=name, website=matches[0]['url'], category=context['category'],
                                              location=context['location'], phone='', public_emails=[],
                                              location_evidence='Directory and company search evidence only; business identity and location require review',
                                              discovery_source='directory_index:' + source, source_url=url + '; ' + matches[0]['url'],
                                              discovery_query=query, directory_profile_fetched=False))
                    logging.info('%s indexed listing resolved to company website: %s -> %s (profile unavailable)', source, name, matches[0]['url'])
                    break
            except Exception as error:
                if getattr(getattr(error, 'response', None), 'status_code', None) == 429:
                    unavailable.add(engine)
                searches.append(dict(profile_url=url, engine=engine, query=query, error=str(error)))
            finally:
                time.sleep(settings['openserp']['query_delay_seconds'])
    report['directory_index_recovered_companies'] = len({normalize_domain(r['website']) for r in recovered})
    return recovered
