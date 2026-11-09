"""Execute the actual release lookup shell against isolated command doubles."""
import os
from pathlib import Path
import subprocess
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/_ci-template.yml'
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
SOURCE = 'https://github.com/VelnorLabs/velnor-user-api'


def steps():
    return yaml.safe_load(WORKFLOW.read_text())['jobs']['build-sign-push']['steps']


def run_lookup(tmp_path, account='113831183566', mode='existing', revision=SHA, source=SOURCE):
    aws = tmp_path / 'aws'
    aws.write_text('''#!/bin/sh
if [ "$LOOKUP_MODE" = existing ]; then
  printf '%s\\n' "$TEST_DIGEST"
elif [ "$LOOKUP_MODE" = missing ]; then
  echo 'An error occurred (ImageNotFoundException)' >&2; exit 254
else
  echo 'An error occurred (AccessDeniedException)' >&2; exit 254
fi
''')
    docker = tmp_path / 'docker'
    docker.write_text('''#!/bin/sh
if [ "$1" = pull ]; then exit 0; fi
case "$*" in
  *image.revision*) printf '%s\\n' "$TEST_REVISION" ;;
  *image.source*) printf '%s\\n' "$TEST_SOURCE" ;;
  *) exit 2 ;;
esac
''')
    aws.chmod(0o755)
    docker.chmod(0o755)
    output = tmp_path / 'output'
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               RELEASE_ACCOUNT=account, LOOKUP_MODE=mode, TEST_DIGEST=DIGEST, TEST_REVISION=revision,
               TEST_SOURCE=source, RELEASE_SHA=SHA, RELEASE_SOURCE=SOURCE,
               RELEASE_REPOSITORY='velnor-user-api', ECR_REGISTRY='registry.invalid',
               GITHUB_OUTPUT=str(output))
    script = next(s['run'] for s in steps() if s.get('id') == 'existing-image')
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    return result.returncode, output.read_text() if output.exists() else ''


def test_matching_existing_image_resumes(tmp_path):
    assert run_lookup(tmp_path) == (0, 'exists=true\n')


def test_missing_image_builds(tmp_path):
    assert run_lookup(tmp_path, mode='missing') == (0, 'exists=false\n')


def test_access_denied_does_not_build_or_overwrite(tmp_path):
    code, output = run_lookup(tmp_path, mode='denied')
    assert code != 0 and not output


def test_wrong_source_sha_is_rejected(tmp_path):
    code, output = run_lookup(tmp_path, revision='c' * 40)
    assert code != 0 and not output


def test_wrong_repository_is_rejected(tmp_path):
    code, output = run_lookup(tmp_path, source='https://github.com/other/repository')
    assert code != 0 and not output


def test_resume_still_requires_signature_and_sbom():
    ss = steps()
    named = {s['name']: s for s in ss}
    for name in ['Build Docker image', 'Push image to ECR', 'Sign image with cosign (KMS)']:
        assert named[name]['if'] == "steps.existing-image.outputs.exists == 'false'"
    verify = named['Verify exact release signature (including resumed images)']
    assert verify['if'] == "vars.AWS_ACCOUNT_ID == '113831183566'"
    assert 'continue-on-error' not in verify
    assert 'steps.image-ref.outputs.image-uri' in verify['run']
    for name in ['Generate SBOM for signed image (Syft)', 'Attest SBOM with cosign (KMS)']:
        assert 'if' not in named[name] and 'continue-on-error' not in named[name]
    resolve = named['Resolve exact release digest']['run']
    assert '"$digest" == "$(cat release-digest.txt)"' in resolve
    assert 'cosign sign' not in verify['run']


def test_other_accounts_keep_original_build_path(tmp_path):
    assert run_lookup(tmp_path, account="999999999999", mode="denied") == (0, "exists=false\n")
