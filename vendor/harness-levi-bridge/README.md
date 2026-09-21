# harness levi bridge

The **Claude Code OAuth bridge** from the LEVI harness: an Anthropic-compatible
HTTP proxy that lets trajectory generation run on a Claude Code Pro/Max
subscription instead of a metered `ANTHROPIC_API_KEY`.

Start here:

| File | Read it for |
|---|---|
| **[USAGE_AND_INTEGRATION.md](USAGE_AND_INTEGRATION.md)** | Install, run, configure, integrate, troubleshoot. Start here. |
| **[TRAJECTORY_GENERATION.md](TRAJECTORY_GENERATION.md)** | How the bridge + harness + OpenHands actually produce trajectories, end to end. |

Sixty-second version:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export AURORA_CC_PYTHON="$PWD/.venv/bin/python"     # required outside the harness layout
./claude_code_bridge.sh check                       # -> [bridge] credentials OK
./claude_code_bridge.sh start                       # -> listening on :8765
curl -s localhost:8765/healthz
```

Then point your LLM config's `base_url` at the bridge — `claude-code-oauth.json`
is a ready-made one. Two things bite people first: the launcher's default
Python path (hence `AURORA_CC_PYTHON`), and the container-to-host address
(`172.17.0.1` on Linux, `host.docker.internal` on macOS Docker Desktop). Both
are covered in the usage doc.

Requires a Claude Code subscription already logged in via the `claude` CLI on
this machine. Read §9 of the usage doc before first use — a bridge self-refresh
can log that CLI out if you don't set `AURORA_CC_WRITE_BACK_KEYCHAIN=1` or use
a dedicated account.
