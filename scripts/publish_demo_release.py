"""Publish the single dated demo release; no personal access token is stored.
Invoked by GitHub Actions using its repository-scoped GITHUB_TOKEN.
"""
from datetime import datetime, timezone
from pathlib import Path
import hashlib,json,os,sys,urllib.request,urllib.error,urllib.parse

def main():
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'docs/release-plan.json').read_text())
    now=datetime.now(timezone.utc)
    due=datetime.fromisoformat(manifest['publish_at_utc'])
    deadline=datetime.fromisoformat(manifest['deadline_utc'])
    validate=os.environ.get('VALIDATE_ONLY','true').lower()=='true'
    for asset in manifest['assets']:
        data=(root/asset['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest()==asset['sha256'],'Demo asset differs from the reviewed recording'
    if validate:
        print('Validated release manifest and both reviewed demo assets. No release created.');return
    if not due<=now<deadline:
        print('Outside this one-day release window. No release created.');return
    repo=os.environ['GITHUB_REPOSITORY'];token=os.environ['GITHUB_TOKEN']
    def api(method,path,body=None,content_type='application/json'):
        payload=json.dumps(body).encode() if isinstance(body,(dict,list)) else body
        url=path if path.startswith('https://uploads.github.com/') else 'https://api.github.com'+path
        request=urllib.request.Request(url,data=payload,method=method,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':content_type,'X-GitHub-Api-Version':'2022-11-28','User-Agent':'DatedWorkflowDemoRelease'})
        with urllib.request.urlopen(request,timeout=90) as response:return json.load(response)
    # The auth token can write only this repository. Never change repository visibility.
    releases=api('GET',f'/repos/{repo}/releases?per_page=100')
    release=next((r for r in releases if r['tag_name']==manifest['tag']),None)
    if release and not release['draft']:
        print('This demo release is already published. No duplicate created.');return
    if not release:
        release=api('POST',f'/repos/{repo}/releases',dict(tag_name=manifest['tag'],target_commitish=os.environ.get('GITHUB_SHA','main'),name=manifest['title'],body=(root/'docs/RELEASE.md').read_text(),draft=True,prerelease=True))
    for asset in manifest['assets']:
        existing=next((a for a in release['assets'] if a['name']==asset['name']),None)
        if existing:
            assert existing['size']==(root/asset['path']).stat().st_size and existing['state']=='uploaded';continue
        uploaded=api('POST',release['upload_url'].split('{')[0]+'?name='+urllib.parse.quote(asset['name']), (root/asset['path']).read_bytes(),asset['content_type'])
        assert uploaded['state']=='uploaded' and uploaded['size']==(root/asset['path']).stat().st_size
    published=api('PATCH',f"/repos/{repo}/releases/{release['id']}",dict(draft=False,prerelease=True))
    print('Published demo release: '+published['html_url'])

if __name__=='__main__':main()
