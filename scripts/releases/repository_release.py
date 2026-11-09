#!/usr/bin/env python3
"""Publish reviewed repository snapshots; never build or deploy application code."""
import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

VERSION = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
SHA = r'[0-9a-f]{40}'
LOCK = '.release/release-lock.json'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def api(path, method='GET', data=None):
    token = os.environ['RELEASE_GOVERNANCE_READ_TOKEN'] if path.endswith('/immutable-releases') else os.environ['GH_TOKEN']
    request = Request('https://api.github.com' + path, method=method,
                      data=canonical(data).encode() if data is not None else None,
                      headers={'Authorization': 'Bearer ' + token,
                               'Accept': 'application/vnd.github+json',
                               'X-GitHub-Api-Version': '2022-11-28'})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def pages(path):
    result = []
    for page in range(1, 101):
        batch = api(f'{path}?per_page=100&page={page}')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ValueError('pagination limit reached; refusing incomplete evidence')


def validate(lock, repo):
    require(lock.get('schema_version') == 1 and lock.get('repository') == repo, 'wrong schema or repository')
    require(re.fullmatch(VERSION, lock.get('version', '')), 'stable SemVer required')
    require(re.fullmatch(SHA, lock.get('source_sha', '')), 'full source SHA required')
    require(isinstance(lock.get('native_versions'), dict), 'native package versions must be recorded explicitly')
    require(isinstance(lock.get('artifacts'), list), 'artifact inventory required (empty only for source-only releases)')
    for item in lock['artifacts']:
        require(item.get('kind') in ('ecr', 's3'), 'artifacts must use ECR digest or versioned S3')
        if item['kind'] == 'ecr':
            require(re.fullmatch(r'\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9/_-]+@sha256:[a-f0-9]{64}', item.get('uri', '')), 'image digest required')
        else:
            require(item.get('bucket') and item.get('key') and item.get('version_id') not in (None, '', 'null') and
                    re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '')), 'S3 version and checksum required')
    runs = lock.get('validation_runs')
    require(isinstance(runs, list) and runs, 'Development validation runs required')
    require(any(r.get('workflow') == '.github/workflows/ci.yml' for r in runs), 'main CI required')
    for run in runs:
        require(type(run.get('run_id')) is int and run['run_id'] > 0, 'run ID required')
        require(re.fullmatch(r'\.github/workflows/[a-zA-Z0-9_.-]+\.ya?ml', run.get('workflow', '')), 'workflow path required')
        require(isinstance(run.get('required_jobs'), list) and run['required_jobs'] and
                all(isinstance(j, str) and j.strip() for j in run['required_jobs']), 'explicit successful job names required')
    require(lock.get('development', {}).get('validated') is True and
            lock['development'].get('evidence') and lock['development'].get('limitations') is not None,
            'reviewed Development evidence and limitations required')
    require(isinstance(lock.get('change_summary'), str) and lock['change_summary'].strip(), 'change summary required')


def verify_runs(lock):
    root = '/repos/' + lock['repository']
    for evidence in lock['validation_runs']:
        run = api(f"{root}/actions/runs/{evidence['run_id']}")
        require(run.get('repository', {}).get('full_name') == lock['repository'] and
                run.get('head_sha') == lock['source_sha'] and run.get('head_branch') == 'main' and
                run.get('event') in ('push', 'workflow_dispatch') and run.get('path') == evidence['workflow'] and
                run.get('status') == 'completed' and run.get('conclusion') == 'success', 'validation run does not prove exact main SHA')
        jobs = []
        for page in range(1, 101):
            batch = api(f"{root}/actions/runs/{evidence['run_id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page={page}")['jobs']
            jobs.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError('job pagination exceeded')
        for name in evidence['required_jobs']:
            matches = [j for j in jobs if j['name'] == name]
            require(len(matches) == 1 and matches[0].get('conclusion') == 'success', 'required job missing, skipped, or failed: ' + name)


def reviewed_lock(repo, number):
    root = '/repos/' + repo
    pr = api(f'{root}/pulls/{number}')
    require(pr.get('merged') and pr['base']['ref'] == 'main' and
            pr['base']['repo']['full_name'] == repo and pr['head']['repo']['full_name'] == repo, 'release PR must be merged to this repository main')
    files = pages(f'{root}/pulls/{number}/files')
    require(len(files) == 1 and files[0]['filename'] == LOCK and files[0]['status'] in ('added', 'modified'), 'release PR must change only the release lock')
    latest = {}
    for review in pages(f'{root}/pulls/{number}/reviews'):
        if review['state'] in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[review['user']['login'].lower()] = review
    approvals = [r for login, r in latest.items() if r['state'] == 'APPROVED' and
                 r['commit_id'] == pr['head']['sha'] and login != pr['user']['login'].lower()]
    require(approvals and not any(r['state'] == 'CHANGES_REQUESTED' for r in latest.values()), 'current-head independent PR approval required')
    # Require an approval from someone with write access, not any public reader.
    require(any(api(f"{root}/collaborators/{quote(r['user']['login'])}/permission").get('permission') in
                ('write', 'maintain', 'admin') for r in approvals), 'repository writer approval required')
    def content(ref):
        result = api(f'{root}/contents/{LOCK}?ref={ref}')
        return base64.b64decode(result['content'])
    require(content(pr['head']['sha']) == content(pr['merge_commit_sha']), 'merged lock differs from reviewed lock')
    lock = json.loads(content(pr['merge_commit_sha']))
    validate(lock, repo)
    comparison = api(f"{root}/compare/{lock['source_sha']}...{pr['merge_commit_sha']}")
    require(comparison.get('status') in ('ahead', 'identical'), 'source must be ancestor of merged release PR')
    return lock


def publish(repo, number):
    require(os.environ.get('GITHUB_REF') == 'refs/heads/main' and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'publishing requires manual dispatch from main')
    lock = reviewed_lock(repo, number)
    verify_runs(lock)
    root = '/repos/' + repo
    require(api(root + '/immutable-releases').get('enabled') is True, 'enable GitHub immutable releases before publishing')
    tag = 'release/v' + lock['version']
    # Never overwrite, including failed partial attempts. Operators must inspect those.
    for path in (f'{root}/git/ref/tags/{tag}', f'{root}/releases/tags/{quote(tag, safe="")}'):
        try:
            api(path)
        except HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            raise ValueError('version already exists; choose a new version')
    digest = hashlib.sha256(canonical(lock).encode()).hexdigest()
    body = 'Reviewed repository release. No production deployment.\n\n' + f'Release PR: https://github.com/{repo}/pull/{number}\nLock SHA256: {digest}\n\n```json\n{canonical(lock)}\n```'
    result = api(root + '/releases', 'POST', {'tag_name': tag, 'target_commitish': lock['source_sha'],
                 'name': tag, 'body': body, 'draft': False, 'prerelease': False, 'make_latest': 'false'})
    require(result.get('immutable') is True, 'release was not immutable; block promotion and investigate')
    print(result['html_url'])
    print('lock_sha256=' + digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('validate', 'publish'))
    parser.add_argument('--repository', required=True)
    parser.add_argument('--lock', default=LOCK)
    parser.add_argument('--pr', type=int)
    args = parser.parse_args()
    require(re.fullmatch(r'VelnorLabs/velnor-[a-z0-9-]+', args.repository), 'unsupported repository')
    if args.operation == 'validate':
        if not Path(args.lock).exists():
            print('No release proposed; no version is created.')
            return
        validate(json.loads(Path(args.lock).read_text()), args.repository)
    else:
        require(args.pr and args.pr > 0, 'merged release PR number required')
        publish(args.repository, args.pr)


if __name__ == '__main__':
    main()
