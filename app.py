"""Company discovery -> public contact enrichment -> support prospect scoring."""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time

from src.config import ROOT, load
from src.openserp_client import OpenSERPClient
from src import maps_client
from src.dedupe import deduplicate
from src.website_crawler import WebsiteCrawler
from src.support_signal_detector import detect
from src.scorer import score
from src.exporter import export
from src.relevance import relevant
from src import directory_client
from src.directory_sources import SOURCES

RUN_ROOT = ROOT

def save_json(path, data):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    temporary.replace(path)

def select_candidates(records, categories, maximum):
    selected = []
    domains = set()
    for category in categories:
        for record in [r for r in records if category in r.get('search_categories',[])][:2]:
            if record['domain'] not in domains and len(selected) < maximum:
                selected.append(record)
                domains.add(record['domain'])
    for record in records:
        if record['domain'] not in domains and len(selected) < maximum:
            selected.append(record)
            domains.add(record['domain'])
    return selected

def enrich(record, settings):
    crawler = WebsiteCrawler(settings["crawl"])
    if settings.get('browser_enrichment', False):
        from src.crawl4ai_client import Crawl4AICrawler
        crawler = Crawl4AICrawler(settings["crawl"])
    result = crawler.crawl(record["website"])
    evidence, phrases = detect(result["pages"],result["useful_pages"])
    record["public_emails"] = sorted(set(record.get("public_emails",[]) + result["public_emails"]))
    prior_email_evidence = record.get('email_evidence', {})
    record["email_evidence"] = {email: sorted(set(result['email_evidence'].get(email, []) + prior_email_evidence.get(email, [])))
                                for email in record['public_emails']}
    for email in record["public_emails"]:
        if not record["email_evidence"].get(email):
            record["email_evidence"][email] = [record["source_url"] + " (discovery source extraction)"]
    total, priority, contributions = score(evidence,record["public_emails"],settings["scoring"])
    record.update(result["useful_pages"])
    record.update(
        technical_support_score=total,priority=priority,score_breakdown=contributions,
        support_signals=sorted(evidence),signal_evidence=evidence,additional_signals=phrases,
        technical_support_detected="technical_support" in evidence,
        remote_support_detected="remote_support" in evidence,
        social_links=result["social_links"],crawl_errors=result["crawl_errors"],
        pages_crawled=[p["url"] for p in result["pages"]],
        collected_at=datetime.now(timezone.utc).isoformat(),
        enrichment_engine='crawl4ai' if result.get('rendered_pages') else 'requests',
        rendered_pages=result.get('rendered_pages', []),
        notes="; ".join(result["crawl_errors"][:3]))
    if result["site_company_name"]:
        record["company_name"] = result["site_company_name"]
    if not record.get("phone") and result["phones"]:
        record["phone"] = "; ".join(result["phones"])
    return record

def reuse_enriched(record, cached, settings):
    merged = deduplicate([cached,record],settings['blocked_domains'])[0]
    for key in ('search_categories','discovery_queries'):
        merged[key] = sorted(set(merged.get(key,[]) + cached.get(key,[]) + record.get(key,[])))
    for email in merged['public_emails']:
        merged.setdefault('email_evidence',{}).setdefault(email,[record.get('source_url','')+' (Maps website extraction)'])
    total, priority, contributions = score(merged['signal_evidence'],merged['public_emails'],settings['scoring'])
    merged.update(technical_support_score=total,priority=priority,score_breakdown=contributions)
    return merged

def discover_web(settings, client, report):
    records = []
    unavailable = set()
    for location in settings["locations"]:
        for category in settings["categories"]:
            for template in settings["search_templates"]:
                search_term = settings.get('industry_search_terms',{}).get(category,category)
                query = template.format(category=search_term,location=location)
                for engine in settings["openserp"]["engines"]:
                    if engine in unavailable:
                        continue
                    try:
                        payload, results = client.search(query,engine,settings["openserp"]["results_per_query"])
                        filename = re.sub(r"[^a-zA-Z0-9]+","_",category+"_"+location+"_"+engine)[:140] + '_' + hashlib.sha1(query.encode()).hexdigest()[:8] + ".json"
                        save_json(RUN_ROOT / "data/raw" / filename,payload)
                        raw_count = len(results)
                        results = [r for r in results if relevant(r,category,location,settings)]
                        report["searches"].append({"engine":engine,"query":query,"results":raw_count,"relevant_companies":len(results)})
                        logging.info('%s web discovery: %d results, %d relevant company candidates for %s / %s',
                                     engine,raw_count,len(results),category,location)
                        for result in results:
                            records.append({
                                "company_name":result.get("title","").split(" | ")[0],
                                "website":result["url"],"category":category,"location":location,
                                "location_evidence":"Search query only; business location unverified",
                                "phone":"","public_emails":[],
                                "discovery_source":"openserp:"+engine,"source_url":result["url"],
                                "discovery_query":query,
                                "search_snippet":result.get("snippet",""),
                            })
                        if results and settings['openserp'].get('search_mode', 'all') == 'fallback':
                            break
                    except Exception as error:
                        if getattr(getattr(error,'response',None),'status_code',None) == 429:
                            unavailable.add(engine)
                            alternatives = [name for name in settings['openserp']['engines'] if name != engine and name not in unavailable]
                            logging.warning('%s web search is throttled (HTTP 429). Continuing with %s; skipping %s for the rest of this collection.',
                                            engine,', '.join(alternatives) or 'other discovery sources',engine)
                        else:
                            logging.warning("%s search failed; trying another source: %s",engine,error)
                        report["searches"].append({"engine":engine,"query":query,"error":str(error)})
                    time.sleep(settings["openserp"]["query_delay_seconds"])
                time.sleep(settings["openserp"]["query_delay_seconds"])
    return records

def main():
    global RUN_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-leads",type=int)
    parser.add_argument('--profile', choices=['pilot','expanded'], default='pilot')
    parser.add_argument('--location', action='append', help='Repeat for multiple cities; overrides configured locations')
    parser.add_argument('--category', action='append', help='Repeat for industries; overrides configured categories')
    parser.add_argument('--results-per-query', type=int)
    parser.add_argument('--maps-depth', type=int)
    parser.add_argument('--browser-enrichment', action='store_true', help='Use native Crawl4AI to render public company pages')
    parser.add_argument('--skip-enriched', action='store_true', help='Reuse matching companies from the last enrichment')
    parser.add_argument('--append-discovery', action='store_true', help='Keep previously discovered companies when adding cities or industries')
    parser.add_argument('--run-directory',type=Path,help='Isolated data/log/query directory under data/runs for UI jobs')
    parser.add_argument("--web-only",action="store_true")
    parser.add_argument("--maps-only",action="store_true")
    parser.add_argument('--directory', action='append', choices=list(SOURCES), help='Add a business directory; repeat for more sources')
    parser.add_argument('--directories-only', action='store_true', help='Discover only through the selected business directories')
    parser.add_argument('--search-mode', choices=['all','fallback'], default='all', help='Combine all search engines or stop at the first successful one')
    parser.add_argument("--reuse-discovery",action="store_true",help="Enrich saved company discovery; no new searches")
    parser.add_argument("--discover-only",action="store_true",help="Save discovery candidates without website enrichment")
    parser.add_argument("--test-web",action="store_true")
    parser.add_argument("--test-maps",action="store_true")
    parser.add_argument("--maps-input",type=Path,help="Reuse a completed native Maps CSV instead of scraping again")
    parser.add_argument("--engine",default="google")
    parser.add_argument("--query",default='"managed IT services" "Austin TX" -jobs -indeed -glassdoor')
    args = parser.parse_args()
    if sum((args.web_only, args.maps_only, args.directories_only)) > 1:
        parser.error('Choose only one of --web-only/--maps-only/--directories-only')
    if args.maps_only and args.directory:
        parser.error('--maps-only cannot be combined with --directory')
    settings = load()
    settings['openserp']['search_mode'] = args.search_mode
    directory_sources = list(dict.fromkeys(args.directory or (list(SOURCES) if args.directories_only else [])))
    RUN_ROOT = ROOT
    if args.run_directory:
        RUN_ROOT = args.run_directory.resolve()
        if not RUN_ROOT.is_relative_to((ROOT/'data/runs').resolve()):
            parser.error('--run-directory must be inside this project data/runs directory')
    if args.profile == 'expanded':
        profile = json.loads((ROOT/'config/profiles.json').read_text(encoding='utf-8'))['expanded']
        settings['locations'] = profile['locations']
        settings['categories'] = profile['categories']
        settings['max_leads'] = profile['max_leads']
        settings['openserp']['results_per_query'] = profile['results_per_query']
        settings['maps']['depth'] = profile['maps_depth']
        settings['maps']['timeout_seconds'] = profile['maps_timeout_seconds']
    if args.location:
        settings['locations'] = args.location
    if args.category:
        settings['categories'] = args.category
    if args.results_per_query is not None:
        if not 1 <= args.results_per_query <= 100:
            parser.error('--results-per-query must be between 1 and 100')
        settings['openserp']['results_per_query'] = args.results_per_query
    if args.maps_depth is not None:
        if not 1 <= args.maps_depth <= 20:
            parser.error('--maps-depth must be between 1 and 20')
        settings['maps']['depth'] = args.maps_depth
    settings['browser_enrichment'] = args.browser_enrichment
    settings['openserp']['log_directory'] = str(RUN_ROOT/'logs')
    settings['maps']['log_directory'] = str(RUN_ROOT/'logs')
    for folder in ("data/raw","logs","queries"):
        (RUN_ROOT/folder).mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),logging.FileHandler(RUN_ROOT/"logs/controller.log",encoding="utf-8")])
    client = OpenSERPClient(settings["openserp"])
    try:
        if args.test_web:
            client.start()
            payload, results = client.search(args.query,args.engine,10)
            save_json(RUN_ROOT/"data/openserp_test.json",payload)
            companies = [r for r in results if relevant(r,settings['categories'][0],settings['locations'][0],settings)]
            print(json.dumps({"engine":args.engine,"results":len(results),"relevant_companies":len(companies),"output":"data/openserp_test.json"},indent=2))
            return 0 if companies else 2
        if args.test_maps:
            results = maps_client.run(RUN_ROOT/"queries/maps_test.txt",RUN_ROOT/"data/maps_test.csv",settings["maps"])
            print(json.dumps({"businesses_with_websites":len(results),"output":"data/maps_test.csv"},indent=2))
            return 0 if results else 2
        report = {"started_at":datetime.now(timezone.utc).isoformat(),"searches":[],"errors":[]}
        if args.reuse_discovery:
            records = json.loads((RUN_ROOT/"data/discovery.json").read_text(encoding="utf-8"))
            prior_report = RUN_ROOT/'data/discovery_report.json'
            if prior_report.exists():
                prior = json.loads(prior_report.read_text(encoding='utf-8'))
                report['searches'] = prior.get('searches',[])
                report['errors'] = prior.get('errors',[])
                report['discovery_started_at'] = prior.get('started_at','')
            if args.maps_input:
                maps_records = maps_client.read(args.maps_input)
                records.extend(maps_records)
                report['maps_companies_with_websites'] = len(maps_records)
                report['maps_input'] = str(args.maps_input)
        else:
            records = []
            if not args.maps_only and not args.directories_only:
                try:
                    client.start()
                    records.extend(discover_web(settings,client,report))
                except Exception as error:
                    report["errors"].append("OpenSERP: "+str(error))
                    logging.exception("OpenSERP discovery failed")
            if directory_sources:
                try:
                    client.start()
                    records.extend(directory_client.discover(settings,client,report,RUN_ROOT,directory_sources))
                except Exception as error:
                    report['errors'].append('Directories: '+str(error))
                    logging.exception('Directory discovery failed; continuing other sources')
            if not args.web_only and not args.directories_only:
                logging.info('Google Maps discovery started. Website email extraction may take several minutes; web search throttling does not stop this stage.')
                queries = [category+" "+location for location in settings["locations"] for category in settings["categories"]]
                (RUN_ROOT/"queries/maps_run.txt").write_text("\n".join(queries)+"\n",encoding="utf-8")
                try:
                    maps_records = maps_client.read(args.maps_input) if args.maps_input else maps_client.run(RUN_ROOT/"queries/maps_run.txt",RUN_ROOT/"data/maps_discovery.csv",settings["maps"])
                    records.extend(maps_records)
                    logging.info('Google Maps discovery returned %d businesses with websites.',len(maps_records))
                    if not maps_records:
                        report['errors'].append('Maps returned zero businesses with websites; see logs/maps.log')
                except Exception as error:
                    report["errors"].append("Maps: "+str(error))
                    logging.exception("Maps discovery failed")
                    path = RUN_ROOT/"data/maps_discovery.csv"
                    if path.exists():
                        records.extend(maps_client.read(path))
        if args.append_discovery and not args.reuse_discovery and (RUN_ROOT/'data/discovery.json').exists():
            records = json.loads((RUN_ROOT/'data/discovery.json').read_text(encoding='utf-8')) + records
            report['appended_to_prior_discovery'] = True
            previous_report = RUN_ROOT/'data/discovery_report.json'
            if previous_report.exists():
                prior = json.loads(previous_report.read_text(encoding='utf-8'))
                report['searches'] = prior.get('searches',[]) + report['searches']
                report['errors'] = prior.get('errors',[]) + report['errors']
        save_json(RUN_ROOT/"data/discovery.json",records)
        report["raw_company_candidates"] = len(records)
        unique = deduplicate(records,settings["blocked_domains"])
        report["unique_domains"] = len(unique)
        if args.discover_only:
            save_json(RUN_ROOT/'data/run_report.json',report)
            save_json(RUN_ROOT/'data/discovery_report.json',report)
            print(json.dumps(report,indent=2))
            return 0 if unique else 2
        maximum = args.max_leads if args.max_leads is not None else settings["max_leads"]
        if maximum <= 0:
            parser.error("--max-leads must be positive")
        # Favor companies corroborated by both sources, then Maps, over query-only candidates.
        unique.sort(key=lambda r:(not ("google_maps" in r["discovery_source"] and "openserp" in r["discovery_source"]),
                                  "google_maps" not in r["discovery_source"],r["domain"]))
        unique = select_candidates(unique,settings['categories'],maximum)
        logging.info("Enriching %d companies (%d raw candidates)",len(unique),len(records))
        completed = []
        if args.skip_enriched and (RUN_ROOT/'data/enriched_companies.json').exists():
            saved = {r['domain']:r for r in json.loads((RUN_ROOT/'data/enriched_companies.json').read_text(encoding='utf-8'))}
            completed = [reuse_enriched(r,saved[r['domain']],settings) for r in unique if r['domain'] in saved]
            unique = [r for r in unique if r['domain'] not in saved]
            report['reused_enriched_companies'] = len(completed)
        total_companies = len(completed) + len(unique)
        with ThreadPoolExecutor(max_workers=settings["crawl"]["concurrency"]) as pool:
            jobs = {pool.submit(enrich,record,settings):record for record in unique}
            for future in as_completed(jobs):
                record = jobs[future]
                try:
                    completed.append(future.result())
                    logging.info("%s: score=%d emails=%d (%d/%d)",record["domain"],
                                 record["technical_support_score"],len(record["public_emails"]),len(completed),total_companies)
                    save_json(RUN_ROOT/"data/enriched_companies.json",completed)
                except Exception as error:
                    report["errors"].append(record["domain"]+": "+str(error))
                    logging.exception("Enrichment failed for %s",record["domain"])
        if not completed:
            report["errors"].append("No enriched companies; existing final CSV preserved")
            save_json(RUN_ROOT/"data/run_report.json",report)
            print(json.dumps(report,indent=2))
            return 2
        output = RUN_ROOT/settings["output"]
        output.parent.mkdir(parents=True,exist_ok=True)
        save_json(RUN_ROOT/'data/enriched_companies.json',completed)
        report.update(companies=len(completed),email_rows=export(completed,output),
                      companies_with_public_emails=sum(bool(r["public_emails"]) for r in completed),
                      csv=str(output),finished_at=datetime.now(timezone.utc).isoformat())
        save_json(RUN_ROOT/"data/run_report.json",report)
        print(json.dumps(report,indent=2))
        return 0
    finally:
        client.close()

if __name__ == "__main__":
    raise SystemExit(main())

