"""Start the bridge the chosen model needs, then run harbor against a built task."""
from __future__ import annotations

import base64
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from .common import (Failure, HARBOR_BIN, JOBS_DIR, ROOT, VENDOR, load_models,
                     read_env_file, run, say)

AGENT = "openhands_bridge_agent:OpenHandsSDKBridge"


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _port_owner(url: str) -> tuple[str, str] | None:
    """(pid, cwd) of whatever already listens on the port in `url`, if anything."""
    port = url.rsplit(":", 1)[-1].split("/")[0]
    proc = run(f"ss -ltnp 2>/dev/null | grep ':{port} '", capture=True, check=False)
    match = re.search(r"pid=(\d+)", proc.stdout or "")
    if not match:
        return None
    pid = match.group(1)
    try:
        return pid, os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return pid, ""


def start_bridge(route_cfg: dict) -> dict[str, str]:
    """Bring the route's bridge up and return the env the agent needs to reach it."""
    d = ROOT / route_cfg["dir"]
    if not d.is_dir():
        raise Failure(f"bridge directory missing: {d}")

    # A bridge from another checkout answering on this port would run code this
    # harness has never seen - a different runner patch, a different thinking
    # setting - and the run would look fine while grading something else.
    owner = _port_owner(route_cfg["health"])
    if owner and Path(owner[1]).resolve() != d.resolve():
        if os.environ.get("HARNESS_ALLOW_FOREIGN_BRIDGE") != "1":
            raise Failure(
                f"port for {route_cfg['health']} is held by pid {owner[0]} running from "
                f"{owner[1] or 'an unknown directory'}, not this harness's {d}.\n"
                f"  stop it and re-run, or set HARNESS_ALLOW_FOREIGN_BRIDGE=1 to use it as is.")
        say(f"WARNING: using the bridge already running from {owner[1]}")
        return _bridge_env(route_cfg, d)

    if not _healthy(route_cfg["health"]):
        say(f"starting bridge in {d.name}")
        run(route_cfg["start"], cwd=d, capture=True)
    if not _healthy(route_cfg["health"]):
        raise Failure(f"bridge did not answer {route_cfg['health']}; see {d}/logs/")
    say(f"bridge up at {route_cfg['base_url']}")
    return _bridge_env(route_cfg, d)


def _bridge_env(route_cfg: dict, d: Path) -> dict[str, str]:
    api_key = route_cfg.get("api_key", "")
    from_env = route_cfg.get("api_key_from_env")
    if from_env:
        api_key = read_env_file(d / ".env").get(from_env, "")
        if not api_key:
            raise Failure(f"{from_env} not found in {d}/.env")
    return {"LLM_BASE_URL": route_cfg["base_url"], "LLM_API_KEY": api_key}


def judge_env(judge_model: str) -> dict[str, str]:
    """The codex login the rubric judge needs inside the verifier container."""
    auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    env = {"JUDGE_MODEL": judge_model}
    if auth.is_file():
        env["CODEX_AUTH_B64"] = base64.b64encode(auth.read_bytes()).decode()
    else:
        say(f"WARNING: no {auth}; the rubric channel will be UNSCORED (reward = Channel A)")
    return env


def clear_stale_containers() -> None:
    """Trial containers a killed run left behind would hold the ports."""
    if not shutil.which("docker"):
        return
    proc = run("docker ps -a --format '{{.Names}}'", capture=True, check=False)
    names = [n for n in (proc.stdout or "").splitlines() if "__env-" in n]
    if names:
        say(f"removing {len(names)} leftover trial containers")
        run(["docker", "rm", "-f", *names], capture=True, check=False)


def harbor_run(task_dir: Path, model_key: str, job_name: str, attempts: int,
               concurrent: int, retries: int, effort: str | None,
               judge_model: str) -> Path:
    """Run the job and return its folder under out/jobs/."""
    cfg = load_models()
    model = (cfg.get("models") or {}).get(model_key)
    if not model:
        raise Failure(f"unknown model {model_key!r}; config/models.yaml has: "
                      f"{sorted((cfg.get('models') or {}))}")
    route = (cfg.get("routes") or {})[model["route"]]

    env = {**start_bridge(route), **judge_env(judge_model),
           "PYTHONPATH": str(VENDOR / "harness-levi-bridge" / "harbor_agent")}
    if not HARBOR_BIN.exists():
        raise Failure(f"no harbor CLI at {HARBOR_BIN}; run ./setup.sh first")

    cmd = [str(HARBOR_BIN), "run", "-p", str(task_dir.resolve()),
           "-a", AGENT, "-m", model["model"],
           "-k", str(attempts), "-n", str(concurrent), "-r", str(retries),
           "-y", "--job-name", job_name]
    if effort:
        cmd += ["--ak", f"reasoning_effort={effort}"]

    clear_stale_containers()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    say(f"harbor run: {model['model']} k={attempts} n={concurrent} job={job_name}")
    run(cmd, cwd=JOBS_DIR.parent, env=env, check=False)   # a failed trial is not a failed job
    job = JOBS_DIR / job_name
    if not job.is_dir():
        raise Failure(f"harbor produced no job folder at {job}")
    return job
