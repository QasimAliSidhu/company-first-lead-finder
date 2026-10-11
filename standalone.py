"""Independent, account-protected local UI. No ChatGPT service or account required."""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time

from flask import Flask, abort, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from src.config import ROOT, load, lines
from src.domain_utils import normalize_domain, blocked
from src.relevance import relevant
from src.location_catalog import catalog, valid_locations
from src.directory_sources import SOURCES

STATE = ROOT/'data/standalone'
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='company-crawl')

@contextmanager
def connect(state):
    db = sqlite3.connect(state/'accounts.sqlite3', timeout=20)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()

def initialize(state):
    state.mkdir(parents=True,exist_ok=True)
    key = state/'session.key'
    try:
        with key.open('x',encoding='ascii') as handle:
            handle.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with connect(state) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(address TEXT NOT NULL,at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,options TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
        ''')

def account(state, username, password, reset=False):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}',username):
        raise ValueError('Username must be 3–64 letters, numbers, dots, underscores or hyphens.')
    if len(password)<12 or len(password)>256:
        raise ValueError('Use a password of 12–256 characters.')
    initialize(state)
    hashed = generate_password_hash(password,method='pbkdf2:sha256:600000')
    with connect(state) as db:
        if reset:
            user = db.execute('SELECT id FROM users WHERE username=?',(username.lower(),)).fetchone()
            if not user:
                raise ValueError('Account does not exist.')
            db.execute('UPDATE users SET password_hash=? WHERE id=?',(hashed,user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        else:
            db.execute('INSERT INTO users(username,password_hash) VALUES (?,?)',(username.lower(),hashed))

def execute_job(state, job_id, options):
    folder = ROOT/'data/runs'/job_id
    folder.mkdir(parents=True,exist_ok=True)
    with connect(state) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?",(job_id,))
    command = [sys.executable,str(ROOT/'app.py'),'--run-directory',str(folder),
               '--max-leads',str(options['max_leads']),'--results-per-query','30','--maps-depth','3']
    for location in options['locations']:
        command.extend(['--location',location])
    for category in options['categories']:
        command.extend(['--category',category])
    if options['source']=='web':
        command.append('--web-only')
    if options['source']=='maps':
        command.append('--maps-only')
    if options['source']=='directories':
        command.append('--directories-only')
    if options['source'] in ('directories','all'):
        for source in options.get('directories', list(SOURCES)):
            command.extend(['--directory', source])
    if options['browser']:
        command.append('--browser-enrichment')
    try:
        environment = dict(os.environ,PYTHONIOENCODING='utf-8')
        with (folder/'console.log').open('w',encoding='utf-8') as log:
            process = subprocess.Popen(command,cwd=ROOT,env=environment,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            (folder/'runner.pid').write_text(str(process.pid),encoding='ascii')
            try:
                code = process.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,check=False)
                else:
                    process.kill()
                process.wait()
                raise RuntimeError('The one-hour crawl limit was reached.')
        report_path = folder/'data/run_report.json'
        report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
        if code or not report.get('companies'):
            raise RuntimeError('No completed company collection. See the crawl log for source errors.')
        status,error = 'completed',''
    except Exception as failure:
        status,error = 'failed',str(failure)[:300]
    with connect(state) as db:
        db.execute('UPDATE jobs SET status=?,error=? WHERE id=?',(status,error,job_id))

def find_runner(job_id):
    """Match the exact controller and run directory, never an unrelated Python process."""
    import psutil
    target = (ROOT/'data/runs'/job_id).resolve()
    matches = []
    for process in psutil.process_iter(['pid','cmdline','exe']):
        try:
            args = process.info['cmdline'] or []
            if str(ROOT/'app.py') not in args or '--run-directory' not in args:
                continue
            directory = Path(args[args.index('--run-directory')+1]).resolve()
            if directory==target:
                matches.append(process)
        except (psutil.Error,ValueError,IndexError):
            continue
    # Windows virtualenv launchers may have a child with the same arguments.
    parents = set()
    for process in matches:
        try:
            parents.add(process.ppid())
        except psutil.Error:
            pass
    return next((process for process in matches if process.pid not in parents),None)

def adopt_job(state, row, process):
    import psutil
    try:
        remaining = max(1,3600-(time.time()-row['created']))
        process.wait(timeout=remaining)
        folder = ROOT/'data/runs'/row['id']/'data'
        report = json.loads((folder/'run_report.json').read_text(encoding='utf-8'))
        if not report.get('finished_at') or not report.get('companies') or not (folder/'technical_support_leads.csv').exists():
            raise RuntimeError('Collection did not produce a completed company database. See the crawl log.')
        status,error='completed',''
    except psutil.TimeoutExpired:
        for child in process.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        try:
            process.kill()
        except psutil.Error:
            pass
        status,error='failed','The one-hour crawl limit was reached.'
    except Exception as failure:
        status,error='failed',str(failure)[:300]
    with connect(state) as db:
        db.execute('UPDATE jobs SET status=?,error=? WHERE id=?',(status,error,row['id']))

def resume_jobs(state):
    with connect(state) as db:
        rows=[dict(row) for row in db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,created")]
    for row in rows:
        if row['status']=='queued':
            POOL.submit(execute_job,state,row['id'],json.loads(row['options']))
            continue
        process=find_runner(row['id'])
        if process:
            POOL.submit(adopt_job,state,row,process)
        else:
            try:
                folder=ROOT/'data/runs'/row['id']/'data'
                report=json.loads((folder/'run_report.json').read_text(encoding='utf-8'))
                complete=bool(report.get('finished_at') and report.get('companies') and (folder/'technical_support_leads.csv').exists())
            except (OSError,ValueError):
                complete=False
            with connect(state) as db:
                db.execute('UPDATE jobs SET status=?,error=? WHERE id=?',
                    ('completed' if complete else 'failed','' if complete else 'The crawl controller stopped before completion.',row['id']))

def tail(path, limit=10000):
    try:
        with path.open('rb') as handle:
            handle.seek(max(0,path.stat().st_size-limit))
            return handle.read().decode('utf-8',errors='replace')
    except OSError:
        return ''

def crawl_progress(job):
    folder=ROOT/'data/runs'/job['id']
    options=job['options']
    settings=load()
    web_domains=set()
    engines=set()
    for path in (folder/'data/raw').glob('*.json'):
        try:
            payload=json.loads(path.read_text(encoding='utf-8'))
            results=payload if isinstance(payload,list) else payload.get('results',[])
            for result in results:
                if any(relevant(result,category,location,settings) for category in options['categories'] for location in options['locations']):
                    domain=normalize_domain(result.get('url',''))
                    if domain:
                        web_domains.add(domain)
            for engine in ('google','duckduckgo','bing'):
                if '_'+engine+'_' in path.name:
                    engines.add(engine)
        except (OSError,ValueError,TypeError):
            pass
    maps_count=0
    directory_count=0
    directory_errors=[]
    directory_output=folder/'data/directory_discovery.json'
    if directory_output.exists():
        try:
            directory_data=json.loads(directory_output.read_text(encoding='utf-8'))
            directory_count=len({normalize_domain(r['website']) for r in directory_data.get('records',[])})
            directory_errors=directory_data.get('errors',[])
        except (OSError,ValueError,KeyError,TypeError):
            pass
    maps_csv=folder/'data/maps_discovery.csv'
    if maps_csv.exists():
        try:
            with maps_csv.open(encoding='utf-8-sig',newline='') as handle:
                maps_count=sum(bool(row.get('website')) for row in csv.DictReader(handle))
        except (OSError,csv.Error):
            pass
    records=[]
    enriched=folder/'data/enriched_companies.json'
    if enriched.exists():
        try:
            records=json.loads(enriched.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            pass
    if job['status']=='completed':
        stage='Completed'
    elif job['status']=='failed':
        stage='Stopped'
    elif records:
        stage='Enriching company websites'
    elif maps_csv.exists() or (folder/'queries/maps_run.txt').exists():
        stage='Google Maps discovery and public email extraction'
    elif (folder/'queries/directory_profiles.json').exists():
        stage='Business directory discovery'
    elif web_domains:
        stage='Web company discovery'
    else:
        stage='Waiting in queue' if job['status']=='queued' else 'Starting web discovery'
    log=tail(folder/'console.log')
    note=''
    if '429' in log and 'google' in log.lower():
        note='Google web search throttled. DuckDuckGo supplied fallback results.' if 'duckduckgo' in engines else 'Google web search throttled; trying the configured fallback engines.'
    if directory_errors:
        note += (' ' if note else '') + str(len(directory_errors))+' directory profiles unavailable or had no usable business website; see directory activity.'
    return dict(stage=stage,web_companies=len(web_domains),maps_records=maps_count,
                directory_companies=directory_count,
                enriched_companies=len(records),public_emails=len({email for record in records for email in record.get('public_emails',[])}),note=note)

def create_app(state=STATE, submit=None):
    initialize(state)
    hosting_file = state/'hosting.json'
    public_authority = ''
    if hosting_file.exists():
        public_authority = json.loads(hosting_file.read_text(encoding='utf-8')).get('public_authority','')
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d{1,5})?',public_authority):
            raise ValueError('Invalid public hosting authority.')
    app = Flask(__name__,template_folder=str(ROOT/'local_ui'))
    app.config.update(SECRET_KEY=(state/'session.key').read_text(encoding='ascii'),
                      MAX_CONTENT_LENGTH=16384,SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict',PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
                      TRUSTED_HOSTS=['127.0.0.1','localhost'])
    if public_authority:
        app.config.update(SESSION_COOKIE_SECURE=True,SESSION_COOKIE_NAME='lead_finder_session')
        app.config['TRUSTED_HOSTS'].append(public_authority.split(':')[0])
        # Tailscale terminates TLS and forwards only to the loopback listener.
        # Recognize the configured external authority without trusting arbitrary
        # forwarded host/protocol headers; CSRF origins must still match it.
        original_wsgi = app.wsgi_app
        def public_https(environ,start_response):
            if environ.get('HTTP_HOST','').lower()==public_authority:
                environ['wsgi.url_scheme']='https'
            return original_wsgi(environ,start_response)
        app.wsgi_app=public_https
    app.config['TEMPLATES_AUTO_RELOAD']=True
    launcher = submit or (lambda job_id, options: POOL.submit(execute_job,state,job_id,options))

    def user_id():
        token = session.get('sid','')
        with connect(state) as db:
            row = db.execute('SELECT user_id FROM sessions WHERE token_hash=? AND expires>?',
                (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return row['user_id'] if row else None

    def csrf():
        return session.setdefault('csrf',secrets.token_urlsafe(32))

    def reject_form(message):
        if request.path=='/login':
            session['csrf']=secrets.token_urlsafe(32)
            return render_template('login.html',csrf=csrf(),error=message),403
        return jsonify(error=message),403

    @app.before_request
    def authorize():
        if request.method not in ('GET','HEAD','POST'):
            abort(405)
        if request.method=='POST':
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf','')
            expected = session.get('csrf')
            if not expected or not secrets.compare_digest(str(supplied),str(expected)):
                return reject_form('This form expired or cookies were unavailable. Please sign in using the refreshed form below.')
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return reject_form('The sign-in page needs a refresh. Please use the refreshed form below at the same address.')
        if request.path not in ('/login','/health') and not user_id():
            return (jsonify(error='Please sign in.'),401) if request.path.startswith('/api/') else redirect('/login')

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY','Referrer-Policy':'same-origin',
            'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
        if request.path=='/leads':
            response.headers['X-Frame-Options']='SAMEORIGIN'
            response.headers['Content-Security-Policy']=response.headers['Content-Security-Policy'].replace("frame-ancestors 'none'","frame-ancestors 'self'")
        return response

    @app.get('/health')
    def health():
        return jsonify(status='ok',mode='independent-company-finder')

    @app.route('/login',methods=['GET','POST'])
    def login():
        error = ''
        if request.method=='POST':
            address = request.remote_addr or ''
            now = time.time()
            with connect(state) as db:
                db.execute('DELETE FROM login_attempts WHERE at<?',(now-900,))
                attempts = db.execute('SELECT count(*) FROM login_attempts WHERE address=?',(address,)).fetchone()[0]
                if attempts>=10:
                    return render_template('login.html',csrf=csrf(),error='Too many attempts. Try again in 15 minutes.'),429
                db.execute('INSERT INTO login_attempts VALUES (?,?)',(address,now))
                username = request.form.get('username','')[:64].lower()
                user = db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
                password = request.form.get('password','')
                if user and len(password)<=256 and check_password_hash(user['password_hash'],password):
                    session.clear()
                    token = secrets.token_urlsafe(32)
                    session.update(sid=token,csrf=secrets.token_urlsafe(32))
                    session.permanent = True
                    db.execute('INSERT INTO sessions VALUES (?,?,?)',
                        (hashlib.sha256(token.encode()).hexdigest(),user['id'],now+43200))
                    db.execute('DELETE FROM login_attempts WHERE address=?',(address,))
                    return redirect('/')
            error = 'Incorrect username or password.'
        return render_template('login.html',csrf=csrf(),error=error)

    @app.post('/logout')
    def logout():
        with connect(state) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(session.get('sid','').encode()).hexdigest(),))
        session.clear()
        return jsonify(ok=True)

    @app.get('/')
    def home():
        return render_template('home.html')

    @app.get('/leads')
    def leads_view():
        selected=request.args.get('job')
        if selected:
            with connect(state) as db:
                if not db.execute("SELECT id FROM jobs WHERE id=? AND user_id=? AND status='completed'",(selected,user_id())).fetchone():
                    abort(404)
        page = (ROOT/'site/worker/page.html').read_text(encoding='utf-8')
        # Imported files belong to this browser user; don't retain another account's localStorage import.
        page = page.replace("const value=localStorage.getItem(STORE);","const value=null;")
        page = page.replace('Your imported company database','Your browser import')
        page = page.replace('Collected company database','Your latest completed company collection')
        if selected:
            page=page.replace("fetch('/api/leads',","fetch('/api/leads?job="+selected+"',")
            page=page.replace('Your latest completed company collection','Your selected company collection')
        return page

    @app.get('/api/me')
    def me():
        with connect(state) as db:
            user = db.execute('SELECT username FROM users WHERE id=?',(user_id(),)).fetchone()
        return jsonify(username=user['username'],csrf=csrf(),categories=lines('categories_all.txt'))

    @app.get('/api/locations')
    def locations_menu():
        return jsonify(catalog())

    @app.get('/api/jobs')
    def jobs():
        with connect(state) as db:
            rows = db.execute('SELECT * FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT 30',(user_id(),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['options'] = json.loads(item['options'])
            folder=ROOT/'data/runs'/item['id']
            item['log']=tail(folder/'console.log')
            maps_activity=tail(folder/'logs/maps.log',6000)
            if maps_activity:
                item['log']+='\n\nRecent Google Maps activity:\n'+maps_activity
            directory_activity=tail(folder/'logs/directories.log',4000)
            if directory_activity:
                item['log']+='\n\nRecent directory activity:\n'+directory_activity
            item['progress']=crawl_progress(item)
            result.append(item)
        return jsonify(jobs=result)

    @app.post('/api/jobs')
    def start_job():
        data = request.get_json(silent=True) or {}
        if not isinstance(data,dict):
            return jsonify(error='Expected crawl options.'),400
        locations = data.get('locations',[])
        categories = data.get('categories',[])
        if not isinstance(locations,list) or not 1<=len(locations)<=3 or not all(isinstance(s,str) and s in valid_locations() for s in locations):
            return jsonify(error='Choose 1–3 US cities from the state and city dropdowns.'),400
        if len(set(locations)) != len(locations):
            return jsonify(error='Choose different cities for each location.'),400
        if not isinstance(categories,list) or not 1<=len(categories)<=5 or not all(s in lines('categories_all.txt') for s in categories):
            return jsonify(error='Choose 1–5 configured industries.'),400
        maximum = data.get('max_leads',50)
        if type(maximum)!=int or not 1<=maximum<=500 or data.get('source') not in ('web','maps','both','directories','all') or type(data.get('browser',False))!=bool:
            return jsonify(error='Invalid source, browser option or company limit (1–500).'),400
        directories=data.get('directories',list(SOURCES))
        if not isinstance(directories,list) or not all(isinstance(s,str) and s in SOURCES for s in directories) or len(directories)>len(SOURCES):
            return jsonify(error='Choose supported business directories.'),400
        if data['source'] in ('all','directories') and not directories:
            return jsonify(error='Select at least one business directory.'),400
        job_id = secrets.token_hex(16)
        options = dict(locations=locations,categories=categories,max_leads=maximum,source=data['source'],browser=data.get('browser',False))
        if data['source'] in ('all','directories'):
            options['directories']=list(dict.fromkeys(directories))
        with connect(state) as db:
            # One queue avoids racing shared Go services and caps per-user pending work.
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT count(*) FROM jobs WHERE user_id=? AND status IN ('queued','running')",(user_id(),)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if active>=2 or total>=10:
                return jsonify(error='Crawl queue is full. Wait for an active collection to finish.'),429
            db.execute('INSERT INTO jobs(id,user_id,status,created,options) VALUES (?,?,?,?,?)',
                (job_id,user_id(),'queued',time.time(),json.dumps(options)))
        launcher(job_id,options)
        return jsonify(job_id=job_id,status='queued'),202

    @app.get('/api/leads')
    def data():
        with connect(state) as db:
            selected=request.args.get('job')
            if selected:
                row=db.execute("SELECT id FROM jobs WHERE id=? AND user_id=? AND status='completed'",(selected,user_id())).fetchone()
                if not row:
                    abort(404)
            else:
                row = db.execute("SELECT id FROM jobs WHERE user_id=? AND status='completed' ORDER BY created DESC LIMIT 1",(user_id(),)).fetchone()
        if row:
            folder = ROOT/'data/runs'/row['id']/'data'
            records = json.loads((folder/'enriched_companies.json').read_text(encoding='utf-8'))
            report = json.loads((folder/'run_report.json').read_text(encoding='utf-8'))
            return jsonify(mode='company-discovery',companies=records,collected_at=report['finished_at'],
                blocked_domains=sorted(load()['blocked_domains']),source_summary='Your local crawl',
                discovery_note='Your latest completed collection. Review source evidence and errors in crawl history.')
        return jsonify(json.loads((ROOT/'site/worker/leads.json').read_text(encoding='utf-8')))

    return app

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--create-user')
    parser.add_argument('--reset-user')
    parser.add_argument('--port',type=int,default=8787)
    args = parser.parse_args()
    if args.create_user or args.reset_user:
        password = getpass.getpass('New password (at least 12 characters): ')
        if password != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords did not match.')
        try:
            account(STATE,args.create_user or args.reset_user,password,reset=bool(args.reset_user))
        except (ValueError,sqlite3.IntegrityError) as error:
            raise SystemExit(str(error))
        print('Account saved. Each user signs in with their own username and password.')
        return
    if not 1024<=args.port<=65535:
        parser.error('Choose a port from 1024 to 65535.')
    app = create_app()
    resume_jobs(STATE)
    from waitress import serve
    print(f'Independent lead finder: http://127.0.0.1:{args.port}',flush=True)
    serve(app,host='127.0.0.1',port=args.port,threads=6)

if __name__=='__main__':
    main()
