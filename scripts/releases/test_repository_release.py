import copy
import pytest
import repository_release as r

@pytest.fixture
def lock():
    return dict(schema_version=1, repository='VelnorLabs/velnor-web', version='1.0.0',
                source_sha='a'*40, native_versions={}, artifacts=[], change_summary='Initial release',
                development={'validated':True, 'evidence':'Onboarding smoke run', 'limitations':[]},
                validation_runs=[{'run_id':12,'workflow':'.github/workflows/ci.yml','required_jobs':['Test']}])

def test_valid(lock):
    r.validate(lock, lock['repository'])

@pytest.mark.parametrize('field,value', [('version','01.0.0'),('version','1.0.0;touch /tmp/no'),('version','1.0.0-rc.1'),('source_sha','main'),('artifacts',[{'kind':'ecr','uri':'image:latest'}]),('development',{'validated':False}),('validation_runs',[])])
def test_invalid(lock, field, value):
    lock[field]=value
    with pytest.raises(ValueError): r.validate(lock,lock['repository'])

def test_skipped_validation_rejected(lock, monkeypatch):
    run={'repository':{'full_name':lock['repository']},'head_sha':lock['source_sha'], 'head_branch':'main', 'event':'push','path':'.github/workflows/ci.yml','status':'completed','conclusion':'success','run_attempt':1}
    monkeypatch.setattr(r,'api',lambda path: {'jobs':[{'name':'Test','conclusion':'skipped'}]} if '/jobs?' in path else run)
    with pytest.raises(ValueError,match='skipped'): r.verify_runs(lock)

@pytest.mark.parametrize('field,value',[('head_sha','b'*40),('head_branch','feature'),('event','pull_request'),('path','.github/workflows/other.yml'),('conclusion','failure')])
def test_wrong_validation_rejected(lock,monkeypatch,field,value):
    run={'repository':{'full_name':lock['repository']},'head_sha':lock['source_sha'], 'head_branch':'main', 'event':'push','path':'.github/workflows/ci.yml','status':'completed','conclusion':'success','run_attempt':1}
    run[field]=value
    monkeypatch.setattr(r,'api',lambda path:run)
    with pytest.raises(ValueError): r.verify_runs(lock)

def test_publish_from_branch_rejected(monkeypatch):
    monkeypatch.setenv('GITHUB_REF','refs/heads/feature')
    with pytest.raises(ValueError): r.publish('VelnorLabs/velnor-web',1)

@pytest.fixture
def reviewed(lock,monkeypatch):
    import base64
    import json
    pr={'merged':True,'base':{'ref':'main','repo':{'full_name':lock['repository']}},
        'head':{'sha':'b'*40,'repo':{'full_name':lock['repository']}},'user':{'login':'author'},'merge_commit_sha':'c'*40}
    review={'state':'APPROVED','commit_id':'b'*40,'user':{'login':'reviewer'}}
    files=[{'filename':r.LOCK,'status':'modified'}]
    permission={'permission':'write'}
    def api(path):
        if '/contents/' in path: return {'content':base64.b64encode(json.dumps(lock).encode()).decode()}
        if '/compare/' in path: return {'status':'ahead'}
        if path.endswith('/permission'): return permission
        if '/files?' in path: return files
        if '/reviews?' in path: return [review]
        return pr
    monkeypatch.setattr(r,'api',api)
    return pr,review,files,permission

def test_reviewed_lock(lock,reviewed):
    assert r.reviewed_lock(lock['repository'],1)==lock

@pytest.mark.parametrize('mutation',['stale','self','dismissed','unmerged','wrong-base','code-change','reader'])
def test_invalid_review(lock,reviewed,mutation):
    pr,review,files,permission=reviewed
    if mutation=='stale': review['commit_id']='d'*40
    if mutation=='self': review['user']['login']='author'
    if mutation=='dismissed': review['state']='DISMISSED'
    if mutation=='unmerged': pr['merged']=False
    if mutation=='wrong-base': pr['base']['ref']='feature'
    if mutation=='code-change': files.append({'filename':'app.py','status':'modified'})
    if mutation=='reader': permission['permission']='read'
    with pytest.raises(ValueError): r.reviewed_lock(lock['repository'],1)
