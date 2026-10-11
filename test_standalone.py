"""Auth, CSRF, crawl validation and per-account data isolation regression checks."""
import json
import tempfile
import unittest
from pathlib import Path
from src.config import ROOT
from standalone import create_app, account, connect, crawl_progress, adopt_job

class StandaloneTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'data/runs').mkdir(parents=True,exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.submitted = []
        self.app = create_app(self.state,submit=lambda jid,options:self.submitted.append((jid,options)))
        self.app.config['TESTING'] = True
        account(self.state,'alice','long-test-password-alice')
        account(self.state,'bob','long-test-password-bob')
        self.alice = self.app.test_client()
        self.bob = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def login(self,client,username):
        page = client.get('/login')
        self.assertEqual(page.headers['Referrer-Policy'],'same-origin')
        with client.session_transaction() as session:
            token = session['csrf']
        response = client.post('/login',data={'username':username,'password':'long-test-password-'+username,'csrf':token},headers={'Origin':'http://localhost'})
        self.assertEqual(response.status_code,302)
        return client.get('/api/me').json['csrf']

    def test_auth_and_host_validation(self):
        self.assertEqual(self.alice.get('/api/leads').status_code,401)
        self.assertEqual(self.alice.get('/').status_code,302)
        self.assertEqual(self.alice.get('/health',headers={'Host':'evil.test'}).status_code,400)
        self.assertEqual(self.alice.post('/login',data={'username':'alice','password':'long-test-password-alice'}).status_code,403)

    def test_configured_https_host_login_and_secure_cookie(self):
        authority='lead-finder.example.test:8443'
        (self.state/'hosting.json').write_text(json.dumps({'public_authority':authority}),encoding='utf-8')
        app=create_app(self.state,submit=lambda jid,options:None)
        app.config['TESTING']=True
        client=app.test_client()
        # Proxy delivers HTTP with the original external Host.
        page=client.get('/login',base_url='http://'+authority)
        self.assertEqual(page.status_code,200)
        self.assertIn('Secure',page.headers['Set-Cookie'])
        self.assertIn('lead_finder_session=',page.headers['Set-Cookie'])
        with client.session_transaction(base_url='https://'+authority) as session:
            token=session['csrf']
        response=client.post('/login',base_url='http://'+authority,data={'username':'alice','password':'long-test-password-alice','csrf':token},headers={'Origin':'https://'+authority})
        self.assertEqual(response.status_code,302)
        self.assertEqual(client.get('/api/me',base_url='https://'+authority).status_code,200)
        self.assertEqual(client.get('/health',base_url='https://evil.test',headers={'X-Forwarded-Host':authority,'X-Forwarded-Proto':'https'}).status_code,400)
        self.assertEqual(client.post('/logout',base_url='https://'+authority,headers={'Origin':'https://evil.test','X-CSRF-Token':token}).status_code,403)

    def test_login_rejects_foreign_origins_and_refreshes_expired_form(self):
        response = self.alice.post('/login',data={'username':'alice','password':'long-test-password-alice','csrf':'expired'})
        self.assertEqual(response.status_code,403)
        self.assertIn('refreshed form',response.get_data(as_text=True))
        with self.alice.session_transaction() as session:
            csrf = session['csrf']
        response = self.alice.post('/login',data={'username':'alice','password':'long-test-password-alice','csrf':csrf},headers={'Origin':'null'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.alice.get('/api/me').status_code,401)
        self.login(self.alice,'alice')

    def test_queue_and_results_are_private_to_account(self):
        a = self.login(self.alice,'alice')
        self.login(self.bob,'bob')
        options = dict(locations=['Dallas TX'],categories=['managed IT services'],max_leads=2,source='web',browser=False)
        self.assertEqual(self.alice.post('/api/jobs',json=options).status_code,403)
        response = self.alice.post('/api/jobs',json=options,headers={'X-CSRF-Token':a})
        self.assertEqual(response.status_code,202)
        self.assertEqual(len(self.submitted),1)
        self.assertEqual(len(self.alice.get('/api/jobs').json['jobs']),1)
        self.assertEqual(self.bob.get('/api/jobs').json['jobs'],[])
        self.assertEqual(self.alice.post('/api/jobs',json={**options,'locations':['127.0.0.1']},headers={'X-CSRF-Token':a}).status_code,400)
        self.assertEqual(self.alice.post('/api/jobs',json=options,headers={'X-CSRF-Token':a,'Origin':'https://evil.test'}).status_code,403)
        # A completed Alice collection must never become Bob's latest dataset.
        job_id = response.json['job_id']
        with tempfile.TemporaryDirectory(dir=ROOT/'data/runs') as folder:
            path = Path(folder)/'data'
            path.mkdir()
            (path/'enriched_companies.json').write_text(json.dumps([{'domain':'private-alice.test'}]),encoding='utf-8')
            (path/'run_report.json').write_text(json.dumps({'finished_at':'2026-10-06'}),encoding='utf-8')
            with connect(self.state) as db:
                db.execute("UPDATE jobs SET id=?,status='completed' WHERE id=?",(Path(folder).name,job_id))
            self.assertEqual(self.alice.get('/api/leads').json['companies'][0]['domain'],'private-alice.test')
            self.assertNotIn('private-alice.test',{c['domain'] for c in self.bob.get('/api/leads').json['companies']})
            self.assertEqual(self.alice.get('/api/leads?job='+Path(folder).name).status_code,200)
            self.assertEqual(self.bob.get('/api/leads?job='+Path(folder).name).status_code,404)
            self.assertEqual(self.bob.get('/leads?job='+Path(folder).name).status_code,404)

    def test_us_location_catalog_and_validation(self):
        self.assertEqual(self.alice.get('/api/locations').status_code,401)
        token=self.login(self.alice,'alice')
        menu=self.alice.get('/api/locations').json
        self.assertEqual(menu['country']['code'],'US')
        self.assertEqual(len(menu['states']),51)
        self.assertGreater(sum(len(state['cities']) for state in menu['states']),30000)
        self.assertIn('Austin',next(state for state in menu['states'] if state['code']=='TX')['cities'])
        options=dict(locations=['Dallas ZZ'],categories=['MSP'],max_leads=2,source='web',browser=False)
        headers={'X-CSRF-Token':token}
        self.assertEqual(self.alice.post('/api/jobs',json=options,headers=headers).status_code,400)
        options['locations']=['Dallas TX','Dallas TX']
        self.assertEqual(self.alice.post('/api/jobs',json=options,headers=headers).status_code,400)
        options['locations']=["O'Fallon MO"]
        self.assertEqual(self.alice.post('/api/jobs',json=options,headers=headers).status_code,202)

    def test_directory_options_and_larger_collection_limit(self):
        token=self.login(self.alice,'alice')
        headers={'X-CSRF-Token':token}
        options=dict(locations=['Dallas TX'],categories=['MSP'],max_leads=500,source='all',directories=['yelp','clutch'],browser=False)
        response=self.alice.post('/api/jobs',json=options,headers=headers)
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.submitted[-1][1]['directories'],['yelp','clutch'])
        for changes in ({'directories':[]},{'directories':['unknown']},{'directories':['yelp',{}]},{'max_leads':501}):
            self.assertEqual(self.alice.post('/api/jobs',json={**options,**changes},headers=headers).status_code,400)

    def test_progress_and_completion_survive_ui_server_restart(self):
        from unittest.mock import Mock
        options=dict(locations=['Austin TX'],categories=['MSP'],max_leads=10,source='both',browser=False)
        with tempfile.TemporaryDirectory(dir=ROOT/'data/runs') as folder:
            root=Path(folder)
            (root/'data/raw').mkdir(parents=True)
            (root/'data/raw/MSP_Austin_TX_duckduckgo_query.json').write_text(json.dumps([{'url':'https://acme.com','title':'Acme MSP Austin','snippet':'IT support'}]),encoding='utf-8')
            (root/'console.log').write_text('google failed: 429',encoding='utf-8')
            (root/'data/enriched_companies.json').write_text(json.dumps([{'domain':'acme.com','public_emails':['info@acme.com']}]),encoding='utf-8')
            row=dict(id=root.name,created=__import__('time').time(),status='running',options=options)
            progress=crawl_progress(row)
            self.assertEqual(progress['web_companies'],1)
            self.assertEqual(progress['enriched_companies'],1)
            self.assertEqual(progress['stage'],'Enriching company websites')
            self.assertIn('DuckDuckGo supplied',progress['note'])
            (root/'data/run_report.json').write_text(json.dumps({'finished_at':'2026-10-06','companies':1}),encoding='utf-8')
            (root/'data/technical_support_leads.csv').write_text('company_name\nAcme\n',encoding='utf-8')
            with connect(self.state) as db:
                db.execute('INSERT INTO jobs(id,user_id,status,created,options) VALUES (?,?,?,?,?)',(root.name,1,'running',row['created'],json.dumps(options)))
            process=Mock()
            adopt_job(self.state,row,process)
            process.wait.assert_called_once()
            with connect(self.state) as db:
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(root.name,)).fetchone()[0],'completed')

    def test_logout_revokes_session_and_reset_revokes_existing_login(self):
        a = self.login(self.alice,'alice')
        self.assertEqual(self.alice.post('/logout',headers={'X-CSRF-Token':a}).status_code,200)
        self.assertEqual(self.alice.get('/api/me').status_code,401)
        self.login(self.alice,'alice')
        account(self.state,'alice','another-long-password',reset=True)
        self.assertEqual(self.alice.get('/api/me').status_code,401)

    def test_login_attempts_are_limited_and_passwords_are_hashed(self):
        self.alice.get('/login')
        with self.alice.session_transaction() as session:
            token = session['csrf']
        for _ in range(10):
            self.alice.post('/login',data={'username':'alice','password':'incorrect','csrf':token})
        self.assertEqual(self.alice.post('/login',data={'username':'alice','password':'incorrect','csrf':token}).status_code,429)
        with connect(self.state) as db:
            row = db.execute("SELECT password_hash FROM users WHERE username='alice'").fetchone()
            self.assertTrue(row[0].startswith('pbkdf2:sha256:600000$'))
            self.assertNotIn('long-test-password-alice',row[0])

if __name__=='__main__':
    (ROOT/'data/runs').mkdir(parents=True,exist_ok=True)
    unittest.main()
