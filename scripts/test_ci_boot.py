"""Execute the real shared boot probe against controlled container/HTTP outcomes."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "scenario,success,probes",
    [
        ("slow", True, 3),
        ("never", False, 30),
        ("unhealthy", False, 30),
        ("exited", False, 0),
        ("wrong_ready", False, 1),
        ("ready_reset", False, 1),
    ],
)
def test_boot_probe(tmp_path, scenario, success, probes):
    workflow = yaml.safe_load((ROOT / ".github/workflows/_ci-template.yml").read_text())
    step = next(
        s
        for job in workflow["jobs"].values()
        for s in job.get("steps", [])
        if s.get("name") == "Container boot smoke test"
    )
    assert step["shell"] == "bash"
    assert "inputs.health-check" in step["if"]
    fake = (
        "#!"
        + sys.executable
        + "\n"
        + r"""
import os, sys
from pathlib import Path
root = Path(os.environ["PROBE_STATE"])
scenario = os.environ["PROBE_SCENARIO"]
command = Path(sys.argv[0]).name
with (root / "calls").open("a") as f:
    f.write(command + " " + " ".join(sys.argv[1:]) + "\n")
if command == "docker":
    if sys.argv[1] == "inspect":
        print("false" if scenario == "exited" else "true")
    elif sys.argv[1] == "logs":
        print("startup diagnostic")
elif command == "curl":
    assert "--connect-timeout" in sys.argv and "--max-time" in sys.argv
    if sys.argv[-1].endswith("/healthz"):
        counter = root / "health_count"
        n = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(n))
        if scenario == "never" or (scenario == "slow" and n < 3):
            sys.exit(52)
        print("503" if scenario == "unhealthy" else "200")
    elif scenario == "ready_reset":
        sys.exit(56)
    else:
        print("200" if scenario == "wrong_ready" else "503")
"""
    )
    for name in ("docker", "curl", "sleep"):
        path = tmp_path / name
        path.write_text(fake)
        path.chmod(0o755)
    script = tmp_path / "probe.sh"
    script.write_text(step["run"])
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PROBE_STATE": str(tmp_path),
        "PROBE_SCENARIO": scenario,
        "SERVICE_NAME": "test-service",
        "PORT": "8080",
        "IMAGE_SHA": "a" * 40,
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    calls = (tmp_path / "calls").read_text()
    assert calls.count("/healthz") == probes
    assert calls.count("/readyz") == (
        1 if scenario in ("slow", "wrong_ready", "ready_reset") else 0
    )
    assert "docker rm -f smoke-test-service" in calls
    assert ("startup diagnostic" in result.stdout) == (not success)
