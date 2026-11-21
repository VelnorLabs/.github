import copy
from datetime import datetime

import pytest

from pipeline import GateError, enforce_window
from release_delta import compare


def snapshot(sha="a" * 40):
    return {"schema_version": 1, "release_id": "rc-1", "components": {"web": {
        "repository": "VelnorLabs/velnor-web", "source_sha": sha, "artifacts": []}},
        "operations": {"web": {"configuration": "v1"}}, "stages": {"web": ["web"]}}


def test_initial_is_explicit():
    with pytest.raises(GateError):
        compare(None, snapshot())
    assert compare(None, snapshot(), initial=True)["components"][0]["status"] == "added"


def test_unpinned_is_never_unchanged():
    value = snapshot(None)
    assert compare(value, value)["components"][0]["status"] == "unresolved"


def test_changed_sha_has_compare_link():
    report = compare(snapshot(), snapshot("b" * 40))
    assert report["components"][0]["status"] == "source-changed"
    assert report["components"][0]["compare_url"].endswith("a" * 40 + "..." + "b" * 40)


def test_config_only_delta_is_visible():
    old, new = snapshot(), snapshot()
    new["operations"]["web"]["configuration"] = "v2"
    report = compare(old, new)
    assert report["components"][0]["status"] == "unchanged"
    assert report["operations"][0]["status"] == "changed"


def test_artifact_only_delta_is_visible():
    old, new = snapshot(), snapshot()
    new["components"]["web"]["artifacts"] = [{"sha256": "different"}]
    assert compare(old, new)["components"][0]["status"] == "artifact-changed"


def test_removal_and_repository_change():
    old, new = snapshot(), snapshot()
    new["components"]["api"] = copy.deepcopy(new["components"].pop("web"))
    report = compare(old, new)
    assert {x["status"] for x in report["components"]} == {"added", "removed"}
    new = snapshot()
    new["components"]["web"]["repository"] = "VelnorLabs/velnor-admin-web"
    assert compare(old, new)["components"][0]["status"] == "repository-changed"


@pytest.mark.parametrize("now,allowed", [("2026-11-10T05:59:59+00:00", False),
                                         ("2026-11-10T06:00:00+00:00", True),
                                         ("2026-11-11T12:00:00+00:00", True)])
def test_tuesday_chicago_window(now, allowed):
    policy = {"schema_version": 1, "production_not_before": "2026-11-10T00:00:00-06:00", "timezone": "America/Chicago"}
    if allowed:
        enforce_window(policy, datetime.fromisoformat(now))
    else:
        with pytest.raises(GateError, match="held until"):
            enforce_window(policy, datetime.fromisoformat(now))


def test_version_only_delta_is_visible():
    from release_delta import compare, markdown
    import copy
    base={'schema_version':1,'components':{'web':{'repository':'VelnorLabs/velnor-web','source_sha':'a'*40,'artifacts':[],'version':'1.0.0'}}}
    candidate=copy.deepcopy(base)
    candidate['components']['web']['version']='1.0.1'
    report=compare(base,candidate)
    assert report['components'][0]['status']=='release-version-changed'
    assert '1.0.0' in markdown(report) and '1.0.1' in markdown(report)
