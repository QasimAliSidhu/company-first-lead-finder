"""Regression checks for company-first discovery and public-email scoring."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.config import load
from src.domain_utils import normalize_domain, blocked
from src.email_classifier import classify
from src.email_extractor import extract
from src.dedupe import deduplicate
from src.scorer import score
from src.support_signal_detector import detect
from src.exporter import export
from src.website_crawler import public_url
from src.relevance import relevant
from app import select_candidates, reuse_enriched, discover_web

class PipelineTests(unittest.TestCase):
    def test_google_throttling_falls_back_and_is_not_retried_for_each_query(self):
        import requests
        settings=load()
        settings.update(categories=['MSP'],locations=['Austin TX'],search_templates=['{category} {location}','{category} {location} contact'])
        settings['openserp'].update(engines=['google','duckduckgo'],query_delay_seconds=0)
        calls=[]
        class Client:
            def search(self,query,engine,limit):
                calls.append(engine)
                if engine=='google':
                    response=requests.Response()
                    response.status_code=429
                    raise requests.HTTPError('Too Many Requests',response=response)
                rows=[{'url':'https://acme.com','title':'Acme MSP Austin TX','snippet':'Managed IT support'}]
                return rows,rows
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'data/raw').mkdir(parents=True)
            report={'searches':[]}
            with patch('app.RUN_ROOT',root),self.assertLogs(level='WARNING') as logs:
                results=discover_web(settings,Client(),report)
        self.assertEqual(len(results),2)
        self.assertEqual(calls,['google','duckduckgo','duckduckgo'])
        self.assertIn('Continuing with duckduckgo',logs.output[0])
    def test_browser_failure_preserves_public_static_html(self):
        from src.crawl4ai_client import Crawl4AICrawler
        crawler = Crawl4AICrawler(load()['crawl'])
        async def failed(url):
            raise ValueError('Browser timeout')
        crawler.render = failed
        with patch('src.website_crawler.WebsiteCrawler.fetch',return_value=('https://acme.com/contact','<p>info@acme.com</p>')):
            self.assertEqual(crawler.fetch('https://acme.com/contact')[1],'<p>info@acme.com</p>')
        self.assertIn('static HTML retained',crawler.render_errors[0])
        crawler.session.close()
        crawler.loop.close()
    def test_cached_enrichment_keeps_new_discovery_evidence(self):
        settings = load()
        cached = dict(domain='acme.com',website='https://acme.com/',company_name='Acme',
                      category='MSP',location='Austin',public_emails=['info@acme.com'],
                      discovery_source='openserp:duckduckgo',source_url='https://acme.com/austin',
                      discovery_query='MSP Austin',signal_evidence={},email_evidence={'info@acme.com':['https://acme.com/contact']},
                      pages_crawled=['https://acme.com/contact'],collected_at='2026-10-01')
        cached['search_categories'] = ['SaaS','MSP']
        cached['discovery_queries'] = ['SaaS Austin']
        found = dict(website='https://acme.com/',company_name='Acme',category='IT support',location='Dallas',
                     discovery_source='google_maps',source_url='https://google.com/maps/acme',
                     discovery_query='IT support Dallas',public_emails=['support@acme.com'])
        merged = reuse_enriched(found,cached,settings)
        self.assertIn('google_maps',merged['discovery_source'])
        self.assertIn('IT support Dallas',merged['discovery_queries'])
        self.assertIn('SaaS Austin',merged['discovery_queries'])
        self.assertIn('SaaS',merged['search_categories'])
        self.assertEqual(merged['collected_at'],'2026-10-01')
        self.assertEqual(merged['email_evidence']['info@acme.com'],['https://acme.com/contact'])
        self.assertIn('support@acme.com',merged['email_evidence'])
    def test_sample_keeps_industry_coverage_and_cap(self):
        records = [dict(domain='msp.com',search_categories=['MSP']),dict(domain='other-msp.com',search_categories=['MSP']),dict(domain='cyber.com',search_categories=['Cyber'])]
        selected = select_candidates(records,['MSP','Cyber'],3)
        self.assertEqual(len(selected),3)
        self.assertIn('cyber.com',{r['domain'] for r in selected})
    def test_rejects_unrelated_or_directory_results(self):
        settings = load()
        self.assertFalse(relevant({'url':'https://imdb.com/title/it','title':'IT (2017)','snippet':'A movie'},'IT support companies','Austin TX',settings))
        self.assertFalse(relevant({'url':'https://directory.test/austin','title':'Top 20 Austin IT support providers','snippet':''},'IT support companies','Austin TX',settings))
        self.assertTrue(relevant({'url':'https://acme.com/austin','title':'Acme IT support in Austin','snippet':'Managed services'},'IT support companies','Austin TX',settings))

    def test_root_domain_and_platform_blocking(self):
        self.assertEqual(normalize_domain("https://www.example.co.uk/about"), "example.co.uk")
        self.assertTrue(blocked("https://uk.indeed.com/company/a", {"indeed.com"}))
        self.assertFalse(blocked("https://indeed-services.com", {"indeed.com"}))
        self.assertEqual(normalize_domain("http://127.0.0.1"), "")

    def test_merge_branches_without_losing_contact(self):
        data = [
            dict(website="https://www.acme.com/about", company_name="Acme | Home",
                 category="SaaS",location="Austin query",public_emails=["hello@acme.com"],
                 discovery_source="openserp:google",source_url="https://acme.com/about"),
            dict(website="http://acme.com/contact",company_name="Acme LLC",
                 category="Software",location="123 Main St, Austin TX",
                 public_emails=["support@acme.com"],phone="123",
                 discovery_source="google_maps",source_url="https://google.com/maps/place/acme"),
        ]
        merged = deduplicate(data, set())
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]["company_name"],"Acme LLC")
        self.assertEqual(len(merged[0]["public_emails"]),2)
        self.assertEqual(merged[0]["phone"],"123")

    def test_email_types_and_no_guessing(self):
        self.assertEqual(classify("careers@acme.com"),"RECRUITING_HIGH")
        self.assertEqual(classify("support@acme.com"),"SUPPORT")
        self.assertEqual(classify("alice@acme.com"),"UNKNOWN")
        self.assertEqual(extract("mailto:INFO@acme.com?subject=Hello logo@2x.png"),["info@acme.com"])
        self.assertEqual(extract("No email is published"),[])

    def test_score_does_not_require_vacancy(self):
        evidence,_ = detect([{"url":"https://acme.com/",
                              "text":"Managed IT services. Technical support from our help desk. 24/7 support. Remote support."}],{})
        value,priority,breakdown=score(evidence,["careers@acme.com"],load()["scoring"])
        self.assertEqual(value,25)
        self.assertEqual(priority,"HIGH")
        self.assertNotIn("technical_support_vacancy",breakdown)
        self.assertEqual(score({},["support@acme.com"],load()["scoring"])[0],0)
        evidence,_ = detect([{'url':'https://acme.com/careers','text':'Technical support services. Apply now for a marketing role.'}],{})
        self.assertNotIn('technical_support_vacancy',evidence)

    def test_export_multiple_emails_and_missing_email(self):
        records = [
            {"company_name":"=unsafe","domain":"acme.com","public_emails":["hello@acme.com","support@acme.com"],"technical_support_score":10},
            {"company_name":"Other","domain":"other.com","technical_support_score":5},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"leads.csv"
            self.assertEqual(export(records,path),3)
            with path.open(encoding="utf-8-sig",newline="") as handle:
                rows=list(csv.DictReader(handle))
            self.assertEqual(rows[0]["company_name"],"'=unsafe")
            self.assertEqual(rows[2]["public_email"],"")

    def test_private_hosts_are_not_crawled(self):
        with patch("socket.getaddrinfo",return_value=[(2,1,6,"",("127.0.0.1",0))]):
            self.assertFalse(public_url("http://localhost/contact"))
        self.assertFalse(public_url("file:///secret"))

if __name__ == "__main__":
    unittest.main()

