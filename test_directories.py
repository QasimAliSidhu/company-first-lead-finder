"""Profile identity, provenance, search union, and directory UI regression checks."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import load
from src.directory_sources import extract_profile, profile_url, indexed_business_name, indexed_website
from src.dedupe import deduplicate
from app import discover_web

class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.seed = dict(source='yelp', category='managed IT services', location='Dallas TX', query='IT Dallas')
        self.blocklist = load()['blocked_domains']

    def test_yelp_redirect_uses_company_website_and_keeps_profile_evidence(self):
        html = '<h1>Acme IT</h1><a href="/biz_redir?url=https%3A%2F%2Facme.com%2Fdallas">acme.com</a>'
        record = extract_profile(html, 'https://www.yelp.com/biz/acme-dallas', self.seed, self.blocklist)
        self.assertEqual(record['website'], 'https://acme.com/dallas')
        self.assertEqual(record['discovery_source'], 'directory:yelp')
        self.assertIn('yelp.com/biz/', record['source_url'])
        self.assertIn('unverified', record['location_evidence'])
        self.assertEqual(deduplicate([record], self.blocklist)[0]['domain'], 'acme.com')

    def test_json_ld_business_identity_excludes_directory_publisher(self):
        graph = {'@graph': [
            {'@type':'Organization','name':'Yellow Pages','url':'https://publisher.com'},
            {'@type':'LocalBusiness','name':'Acme IT','url':'https://acme.com','telephone':'123',
             'address':{'addressLocality':'Dallas','addressRegion':'TX'}}]}
        html = '<h1>Acme IT</h1><script type="application/ld+json">'+json.dumps(graph)+'</script>'
        seed = dict(self.seed, source='yellowpages')
        record = extract_profile(html, 'https://www.yellowpages.com/dallas-tx/mip/acme-123', seed, self.blocklist)
        self.assertEqual(record['website'], 'https://acme.com')
        self.assertEqual(record['location'], 'Dallas, TX')
        self.assertEqual(record['phone'], '123')

    def test_reject_search_pages_private_links_footer_and_ambiguous_profiles(self):
        html = '<h1>Acme</h1><a>Contact</a><footer><a href="https://publisher.com">Website</a></footer>'
        self.assertIsNone(extract_profile(html, 'https://www.yelp.com/biz/acme', self.seed, self.blocklist))
        for website in ['http://127.0.0.1', 'http://localhost', 'https://facebook.com/acme', 'https://acme.com:8000', 'https://user:pass@acme.com']:
            page = '<h1>Acme</h1><a href="'+website+'">Website</a>'
            self.assertIsNone(extract_profile(page, 'https://www.yelp.com/biz/acme', self.seed, self.blocklist))
        page = '<h1>Acme</h1><a href="https://one.com">Website</a><a href="https://two.com">Website</a>'
        self.assertIsNone(extract_profile(page, 'https://www.yelp.com/biz/acme', self.seed, self.blocklist))
        self.assertEqual(profile_url('https://www.yelp.com/search?find_desc=IT', 'yelp'), '')
        self.assertEqual(profile_url('https://evil.com/biz/acme', 'yelp'), '')
        self.assertEqual(profile_url('https://www.yellowpages.com/dallas-tx', 'yellowpages'), '')

    def test_goodfirms_recommendations_do_not_override_business_schema(self):
        business = {'@type':'LocalBusiness','name':'Acme IT','url':'https://acme.com',
                    'contactPoint':{'email':'sales@acme.com'},
                    'address':[{'addressLocality':'Dallas','addressRegion':'TX'}]}
        html = '<h1>Acme IT</h1><script type="application/ld+json">'+json.dumps(business)+'</script>'
        html += '<a data-company-name="Other Firm" href="https://other.com">Visit Website</a>'
        html += '<a href="https://advertiser.com">Visit Website</a>'
        record = extract_profile(html, 'https://www.goodfirms.co/company/acme-it', dict(self.seed,source='goodfirms'), self.blocklist)
        self.assertEqual(record['website'], 'https://acme.com')
        self.assertEqual(record['location'], 'Dallas, TX')
        self.assertEqual(record['public_emails'], ['sales@acme.com'])
        self.assertEqual(record['email_evidence']['sales@acme.com'], ['https://www.goodfirms.co/company/acme-it'])
        merged = deduplicate([dict(record,public_emails=[],email_evidence={},discovery_source='openserp:google'), record],self.blocklist)[0]
        self.assertEqual(merged['email_evidence'], record['email_evidence'])

    def test_search_all_combines_engines_and_fallback_remains_available(self):
        settings = load()
        settings.update(categories=['managed IT services'], locations=['Dallas TX'], search_templates=['{category} {location}'])
        settings['openserp'].update(engines=['google','bing'],query_delay_seconds=0)
        class Client:
            def search(self, query, engine, limit):
                rows = [{'url':f'https://{engine}-acme.com','title':'Managed IT services Dallas TX'}]
                return rows, rows
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'data/raw').mkdir(parents=True)
            with patch('app.RUN_ROOT', root):
                rows = discover_web(settings, Client(), {'searches':[]})
                self.assertEqual(len(rows), 2)
                settings['openserp']['search_mode'] = 'fallback'
                self.assertEqual(len(discover_web(settings, Client(), {'searches':[]})), 1)

    def test_indexed_profile_resolution_requires_brand_city_and_company_domain(self):
        name=indexed_business_name({'title':'Sagiss Reviews (3), Pricing - Clutch'},'clutch','Dallas TX')
        self.assertEqual(name,'Sagiss')
        candidate={'title':'Sagiss - Managed IT in Dallas','url':'https://www.sagiss.com','snippet':'Managed services'}
        self.assertEqual(indexed_website(candidate,name,'Dallas TX',self.blocklist),candidate['url'])
        self.assertEqual(indexed_website(dict(candidate,url='https://clutch.co/profile/sagiss'),name,'Dallas TX',self.blocklist),'')
        self.assertEqual(indexed_website(dict(candidate,title='Other Firm',url='https://other.com'),name,'Dallas TX',self.blocklist),'')
        self.assertEqual(indexed_website(dict(candidate,title='Sagiss Seattle'),name,'Dallas TX',self.blocklist),'')
        self.assertEqual(indexed_business_name({'title':'Dallas Managed IT Service - Clutch'},'clutch','Dallas TX'),'')

    def test_blocked_profile_recovers_via_metasearch_with_search_only_provenance(self):
        from src.directory_client import resolve_indexed_profiles
        settings=load()
        settings['openserp'].update(engines=['google'],query_delay_seconds=0)
        seeds={'https://clutch.co/profile/sagiss':[dict(source='clutch',category='MSP',location='Dallas TX',indexed_title='Sagiss Reviews (3), Pricing - Clutch')]}
        rows=[dict(title='Sagiss IT Dallas',url='https://sagiss.com',snippet='Managed services Dallas TX')]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'data/raw').mkdir(parents=True)
            report={}
            with patch('src.directory_client.search_profiles',return_value=(rows,rows)) as search:
                found=resolve_indexed_profiles(settings,None,seeds,[],{'google'},report,root)
            self.assertEqual(search.call_args.args[3],'ddgs')
            self.assertEqual(found[0]['discovery_source'],'directory_index:clutch')
            self.assertFalse(found[0]['directory_profile_fetched'])
            self.assertIn('require review',found[0]['location_evidence'])
            self.assertIn('clutch.co/profile/sagiss',found[0]['source_url'])
            self.assertEqual(report['directory_index_recovered_companies'],1)
            ambiguous=rows+[dict(title='Sagiss IT Dallas',url='https://sagiss.ca',snippet='Dallas TX')]
            with patch('src.directory_client.search_profiles',return_value=(ambiguous,ambiguous)):
                self.assertEqual(resolve_indexed_profiles(settings,None,seeds,[],{'google'},{},root),[])

    def test_guard_rejects_private_network_and_disallowed_robots(self):
        import asyncio
        from scrapy import Request
        from scrapy.exceptions import IgnoreRequest
        from src.directory_worker import PublicDirectoryGuard
        guard = PublicDirectoryGuard()
        try:
            with patch('src.directory_worker.public_url', return_value=False):
                with self.assertRaises(IgnoreRequest):
                    asyncio.run(guard.process_request(Request('https://www.yelp.com/biz/acme')))
            with patch('src.directory_worker.public_url', return_value=True), patch.object(guard.checker, 'allowed', return_value=False):
                with self.assertRaises(IgnoreRequest):
                    asyncio.run(guard.process_request(Request('https://www.yelp.com/biz/acme')))
        finally:
            guard.checker.session.close()

if __name__ == '__main__':
    unittest.main()
