"""Read-only repository-version and human-approval gates before any AWS access."""
import hashlib
import json
import os
import importlib.util
from pathlib import Path
import re
from urllib.parse import quote

from pipeline import require


def approval(github, policy):
    repo = os.environ.get('GITHUB_REPOSITORY')
    require(repo == 'VelnorLabs/.github', 'production approval must belong to central controller')
    require(os.environ.get('GITHUB_RUN_ATTEMPT') == '1', 'start a new dispatch for a fresh production approval; do not rerun')
    reviewer = policy.get('production_approver', '').lower()
    require(reviewer == 'velnor-ai', 'explicit production approver required')
    root = '/repos/' + repo
    environment = github(root + '/environments/production')
    rules = environment.get('protection_rules', [])
    reviewers = [r['reviewer'].get('login', '').lower() for rule in rules if rule.get('type') == 'required_reviewers'
                 for r in rule.get('reviewers', []) if r.get('type') == 'User']
    require(reviewers == [reviewer], 'production environment must require Velnor-ai approval')
    require(environment.get('can_admins_bypass') is False, 'production administrator bypass must be disabled')
    reviews = github(f"{root}/actions/runs/{os.environ['GITHUB_RUN_ID']}/approvals")
    matching = [review for review in reviews if review.get('user', {}).get('login', '').lower() == reviewer and
                any(env.get('name') == 'production' for env in review.get('environments', []))]
    require(matching and all(review.get('state') == 'approved' for review in matching), 'this run needs explicit Velnor-ai production approval')


def verify_reviewed_evidence(github, published, lock):
    """Recheck release PR and runs: immutable alone does not prove our publisher ran."""
    repo = lock['repository']
    matches = re.findall(r'Release PR: https://github.com/' + re.escape(repo) + r'/pull/([1-9][0-9]*)\n', published.get('body', ''))
    require(len(matches) == 1, 'reviewed release PR reference required')
    path = Path(__file__).resolve().parents[1] / 'releases/repository_release.py'
    spec = importlib.util.spec_from_file_location('repository_release_gate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.api = github
    try:
        require(module.reviewed_lock(repo, int(matches[0])) == lock, 'published evidence differs from reviewed PR')
        module.verify_runs(lock)
    except ValueError as exc:
        require(False, str(exc))


def versions(github, release, components=None):
    for name, component in release['components'].items():
        if components is not None and name not in components:
            continue
        version = component.get('version', '')
        require(isinstance(version, str) and re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version), 'repository release version required: ' + name)
        root = '/repos/' + component['repository']
        tag = 'release/v' + version
        published = github(f'{root}/releases/tags/{quote(tag, safe="")}')
        require(published.get('immutable') is True and not published.get('draft') and not published.get('prerelease'), 'published immutable release required: ' + name)
        ref = github(f'{root}/git/ref/tags/{tag}')
        require(ref.get('object', {}).get('type') == 'commit' and ref['object']['sha'] == component['source_sha'], 'repository tag SHA mismatch: ' + name)
        blocks = re.findall(r'```json\n(.*?)\n```', published.get('body', ''), re.S)
        require(len(blocks) == 1, 'release lock evidence missing: ' + name)
        lock = json.loads(blocks[0])
        canonical = json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()
        require(hashlib.sha256(canonical).hexdigest() == component.get('release_lock_sha256'), 'release lock checksum mismatch: ' + name)
        require(lock.get('repository') == component['repository'] and lock.get('version') == version and
                lock.get('source_sha') == component['source_sha'] and lock.get('artifacts') == component['artifacts'], 'release lock does not match platform component: ' + name)
        require(any(run.get('run_id') == component['ci_run_id'] and run.get('workflow') == component['ci_workflow'] for run in lock.get('validation_runs', [])), 'platform CI differs from release evidence')
        require(lock.get('development', {}).get('validated') is True, 'Development validation missing')
        verify_reviewed_evidence(github, published, lock)
