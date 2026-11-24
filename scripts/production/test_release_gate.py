import copy
import hashlib
import json
import pytest
import release_gate as g
from pipeline import GateError

@pytest.fixture
def approval_api(monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY','VelnorLabs/.github')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT','1')
    monkeypatch.setenv('GITHUB_RUN_ID','12')
    environment={'can_admins_bypass':False,'protection_rules':[{'type':'required_reviewers','reviewers':[{'type':'User','reviewer':{'login':'Velnor-ai'}}]}]}
    reviews=[{'state':'approved','user':{'login':'velnor-ai'},'environments':[{'name':'production'}]}]
    return environment,reviews

def test_approval(approval_api):
    env,reviews=approval_api
    g.approval(lambda path: reviews if path.endswith('/approvals') else env, {'production_approver':'Velnor-ai'})

@pytest.mark.parametrize('mutation', ['missing','bypass','wrong-user','rejected','wrong-environment','rerun'])
def test_fail_closed(approval_api,monkeypatch,mutation):
    env,reviews=approval_api
    if mutation=='missing': env['protection_rules']=[]
    if mutation=='bypass': env['can_admins_bypass']=True
    if mutation=='wrong-user': reviews[0]['user']['login']='someone'
    if mutation=='rejected': reviews[0]['state']='rejected'
    if mutation=='wrong-environment': reviews[0]['environments']=[{'name':'development'}]
    if mutation=='rerun': monkeypatch.setenv('GITHUB_RUN_ATTEMPT','2')
    with pytest.raises(GateError):
        g.approval(lambda path:reviews if path.endswith('/approvals') else env, {'production_approver':'Velnor-ai'})

@pytest.mark.parametrize('mutation',[None,'mutable','sha','checksum','artifact'])
def test_version_identity(mutation, monkeypatch):
    monkeypatch.setattr(g, "verify_reviewed_evidence", lambda *args: None)
    lock={'repository':'VelnorLabs/velnor-web','version':'1.0.0','source_sha':'a'*40,'artifacts':[], 'validation_runs':[{'run_id':1,'workflow':'ci'}], 'development':{'validated':True}}
    c={**lock,'ci_run_id':1,'ci_workflow':'ci','release_lock_sha256':hashlib.sha256(json.dumps(lock,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    published={'immutable':True,'body':'```json\n'+json.dumps(lock)+'\n```'}
    ref={'object':{'type':'commit','sha':'a'*40}}
    if mutation=='mutable': published['immutable']=False
    if mutation=='sha': ref['object']['sha']='b'*40
    if mutation=='checksum': c['release_lock_sha256']='b'*64
    if mutation=='artifact': c['artifacts']=[{'kind':'ecr'}]
    api=lambda path: published if '/releases/' in path else ref
    if mutation:
        with pytest.raises(GateError): g.versions(api,{'components':{'web':c}})
    else: g.versions(api,{'components':{'web':c}})


def test_missing_release_pr_rejected():
    with pytest.raises(GateError, match='PR reference'):
        g.verify_reviewed_evidence(lambda p: {}, {'body':''}, {'repository':'VelnorLabs/velnor-web'})
