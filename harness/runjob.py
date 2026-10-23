"""Start the bridge the chosen model needs, then run harbor against a built task."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .common import (Failure, HARBOR_BIN, JOBS_DIR, ROOT, VENDOR, load_models,
                     read_env_file, run, say)

AGENT = "openhands_bridge_agent:OpenHandsSDKBridge"

# config/models.yaml carries two URLs per route: `health`, the bridge as this host
# reaches it (loopback; every bridge binds 0.0.0.0), and `base_url`, the bridge as a
# task container reaches it (host.docker.internal, which Docker Desktop resolves itself
# and the compose template aliases to the docker0 gateway on a Linux engine). Nothing
# is rewritten per OS any more; the two helpers only name which URL is which.


def _host_url(url: str) -> str:
    """The bridge address as this host reaches it (health probes)."""
    return url


def _container_url(url: str) -> str:
    """The bridge address as a task container reaches it (LLM_BASE_URL)."""
    return url


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _port_of(url: str) -> int:
    """The port in a configured URL, as a number or a refusal."""
    port = urllib.parse.urlsplit(url).port
    if port is None or not 0 < port < 65536:
        raise Failure(f"no usable port in {url!r}")
    return port


def _port_owner(url: str) -> tuple[str, str] | None:
    """(pid, cwd) of whatever already listens on the port in `url`, if anything.

    The port comes from config, so it is parsed to an integer and matched in
    Python; nothing from the URL ever reaches a shell.
    """
    port = _port_of(url)
    if shutil.which("ss"):                                   # Linux
        proc = run(["ss", "-ltnp"], capture=True, check=False)
        line = next((ln for ln in (proc.stdout or "").splitlines()
                     if len(ln.split()) > 3 and ln.split()[3].rsplit(":", 1)[-1] == str(port)), "")
        match = re.search(r"pid=(\d+)", line)
        if not match:
            return None
        pid = match.group(1)
        try:
            return pid, os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return pid, ""
    if shutil.which("lsof"):                                 # macOS, BSD
        proc = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
                   capture=True, check=False)
        pid = next((ln[1:] for ln in (proc.stdout or "").splitlines() if ln.startswith("p")), "")
        if not pid:
            return None
        cwd = run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture=True, check=False)
        return pid, next((ln[1:] for ln in (cwd.stdout or "").splitlines() if ln.startswith("n")), "")
    return None


def start_bridge(route_cfg: dict) -> dict[str, str]:
    """Bring the route's bridge up and return the env the agent needs to reach it."""
    d = ROOT / route_cfg["dir"]
    if not d.is_dir():
        raise Failure(f"bridge directory missing: {d}")

    # A bridge from another checkout answering on this port would run code this
    # harness has never seen - a different runner patch, a different thinking
    # setting - and the run would look fine while grading something else.
    # There is no override: a switch that skips this check is the check's own
    # bypass, and whoever can set it can put any bridge between the agent and
    # the model.
    owner = _port_owner(route_cfg["health"])
    if owner and Path(owner[1]).resolve() != d.resolve():
        raise Failure(
            f"port for {route_cfg['health']} is held by pid {owner[0]} running from "
            f"{owner[1] or 'an unknown directory'}, not this harness's {d}.\n"
            f"  stop it (kill {owner[0]}) and re-run; this harness starts its own.")

    health = _host_url(route_cfg["health"])
    if not _healthy(health):
        say(f"starting bridge in {d.name}")
        run(shlex.split(route_cfg["start"]), cwd=d, capture=True)
    if not _healthy(health):
        raise Failure(f"bridge did not answer {health}; see {d}/logs/")
    say(f"bridge up at {_container_url(route_cfg['base_url'])} (as the task containers reach it)")
    return _bridge_env(route_cfg, d)


def _bridge_env(route_cfg: dict, d: Path) -> dict[str, str]:
    api_key = route_cfg.get("api_key", "")
    from_env = route_cfg.get("api_key_from_env")
    if from_env:
        api_key = read_env_file(d / ".env").get(from_env, "")
        if not api_key:
            raise Failure(f"{from_env} not found in {d}/.env")
    return {"LLM_BASE_URL": _container_url(route_cfg["base_url"]), "LLM_API_KEY": api_key}


def judge_env(judge_model: str, transport: str = "codex") -> dict[str, str]:
    """What the rubric judge needs inside the verifier container.

    JUDGE_MODEL and JUDGE_TRANSPORT name the judge; the login travels as an env var,
    since a separate verifier has no host mounts: the codex ChatGPT login as base64 of
    ~/.codex/auth.json, the claude-code login as CLAUDE_CODE_OAUTH_TOKEN. Without the
    login (or a model) the verifier scores the pytest channel alone and says so in
    judge.json.
    """
    env = {"JUDGE_MODEL": judge_model or "", "JUDGE_TRANSPORT": transport}
    if not judge_model:
        say("WARNING: no judge model (--judge-model / JUDGE_MODEL / models.yaml); "
            "the rubric channel will be UNSCORED (reward = pytest)")
    if transport == "codex":
        auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        if auth.is_file():
            env["CODEX_AUTH_B64"] = base64.b64encode(auth.read_bytes()).decode()
        else:
            say(f"WARNING: no {auth}; the rubric channel will be UNSCORED (reward = pytest)")
    elif transport == "claude-code":
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if token:
            env["JUDGE_CLAUDE_TOKEN"] = token
        else:
            say("WARNING: no CLAUDE_CODE_OAUTH_TOKEN for the claude-code judge; "
                "the rubric channel will be UNSCORED (reward = pytest)")
    return env


def _heartbeat(job: Path, attempts: int, stop: threading.Event, every: float = 30.0) -> None:
    """A plain progress line every `every` seconds, read from the job folder."""
    reported: set[str] = set()
    while not stop.wait(every):
        trials = [p for p in job.iterdir() if p.is_dir()] if job.is_dir() else []
        done = [p for p in trials if (p / "result.json").is_file()]
        for p in done:
            if p.name in reported:
                continue
            reported.add(p.name)
            try:
                reward = json.loads((p / "verifier" / "reward.json").read_text()).get("reward")
            except (OSError, ValueError):
                reward = None
            say(f"  trial {p.name.split('__')[-1]} finished: reward {reward}")
        say(f"progress: {len(done)}/{attempts} trials done, "
            f"{len(trials) - len(done)} running")


def _live_harbor_runs() -> list[str]:
    """Command lines of every `harbor run` alive on this machine, other than ours."""
    proc = run(["ps", "-eo", "pid=,args="], capture=True, check=False)
    me = str(os.getpid())
    return [ln.strip() for ln in (proc.stdout or "").splitlines()
            if "harbor run " in ln and ln.split()[0] != me]


def clear_stale_containers() -> None:
    """Remove trial containers a killed run left behind, and nothing still in use.

    A container name says which trial it belongs to but not whether that trial
    is finished, and other operators run jobs on this machine too. So cleanup
    happens only when no other harbor run is alive: with one alive, a container
    that looks left over may be the live environment of someone else's trial.
    """
    if not shutil.which("docker"):
        return
    proc = run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture=True, check=False)
    names = [n for n in (proc.stdout or "").splitlines() if "__env-" in n]
    if not names:
        return
    live = _live_harbor_runs()
    if live:
        say(f"leaving {len(names)} trial containers alone: {len(live)} other harbor "
            f"run(s) are alive and may own them")
        return
    say(f"removing {len(names)} leftover trial containers")
    run(["docker", "rm", "-f", *names], capture=True, check=False)


def clear_unused_networks() -> None:
    """Drop trial networks nothing is attached to.

    Every trial creates its own docker network, and docker hands out a limited
    range of address pools. Networks a killed run left behind keep their pool,
    so a later run eventually fails to start with "could not find an available,
    non-overlapping IPv4 address pool". Only networks with no containers
    attached are removed, so a live trial's network is never touched.
    """
    if not shutil.which("docker"):
        return
    proc = run(["docker", "network", "ls", "--format", "{{.ID}} {{.Name}}"],
               capture=True, check=False)
    idle = []
    for line in (proc.stdout or "").splitlines():
        net_id, _, name = line.partition(" ")
        if "__env_default" not in name:
            continue
        count = run(["docker", "network", "inspect", net_id, "--format", "{{len .Containers}}"],
                    capture=True, check=False)
        if (count.stdout or "").strip() == "0":
            idle.append(net_id)
    if idle:
        say(f"removing {len(idle)} trial networks with nothing attached")
        run(["docker", "network", "rm", *idle], capture=True, check=False)


def harbor_run(task_dir: Path, model_key: str, job_name: str, attempts: int,
               concurrent: int, retries: int, effort: str | None,
               judge_model: str, judge_transport: str = "codex",
               setup_timeout_multiplier: float | None = None) -> Path:
    """Run the job and return its folder under out/jobs/."""
    cfg = load_models()
    model = (cfg.get("models") or {}).get(model_key)
    if not model:
        raise Failure(f"unknown model {model_key!r}; config/models.yaml has: "
                      f"{sorted((cfg.get('models') or {}))}")
    route = (cfg.get("routes") or {})[model["route"]]

    env = {**start_bridge(route), **judge_env(judge_model, judge_transport),
           "PYTHONPATH": str(VENDOR / "harness-levi-bridge" / "harbor_agent")}
    if not HARBOR_BIN.exists():
        raise Failure(f"no harbor CLI at {HARBOR_BIN}; run ./setup.sh first")

    cmd = [str(HARBOR_BIN), "run", "-p", str(task_dir.resolve()),
           "-a", AGENT, "-m", model["model"],
           "-k", str(attempts), "-n", str(concurrent), "-r", str(retries),
           "-y", "--job-name", job_name]
    if effort:
        cmd += ["--ak", f"reasoning_effort={effort}"]
    if setup_timeout_multiplier and setup_timeout_multiplier != 1:
        # the agent's SDK is downloaded afresh in every trial (~100+ MB); harbor's 360 s
        # budget is for a fast link, a slow one needs more or every trial dies at setup
        cmd += ["--agent-setup-timeout-multiplier", str(setup_timeout_multiplier)]

    clear_stale_containers()
    clear_unused_networks()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    say(f"harbor run: {model['model']} k={attempts} n={concurrent} job={job_name}")
    job = JOBS_DIR / job_name
    stop = threading.Event()
    beat = None
    if not sys.stdout.isatty():
        # no terminal: harbor's live progress cannot draw, so say it in plain lines
        beat = threading.Thread(target=_heartbeat, args=(job, attempts, stop), daemon=True)
        beat.start()
    try:
        run(cmd, cwd=JOBS_DIR.parent, env=env, check=False)   # a failed trial is not a failed job
    finally:
        stop.set()
        if beat:
            beat.join(timeout=5)
    if not job.is_dir():
        raise Failure(f"harbor produced no job folder at {job}")
    return job
