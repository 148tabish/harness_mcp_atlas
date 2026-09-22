import sys
import importlib
import re
from pathlib import Path

sys.path.insert(0, "/app")

from fastmcp import FastMCP
from fastmcp.server.middleware.logging import LoggingMiddleware
from filesystem_server import mcp as filesystem_mcp

main = FastMCP("light-servers")
# fastmcp 3 takes the server first and the namespace second: mount(server, prefix).
main.mount(filesystem_mcp, "filesystem")

# Log every MCP request through the aggregated server, arguments included, so a
# trajectory can be reconstructed from container logs alone. Registered before
# the mounts below only for readability -- middleware wraps the server, not the
# sub-servers, so it sees calls to all of them whenever they are mounted.
#
# Payloads are truncated at 500 chars: a light-server response can be an entire
# table, and an untruncated log would bury the call sequence in world state.
#
# This covers the server_main.py single-process path ONLY. entrypoint.sh runs a
# different topology -- 161 independent `fastmcp run app.py` processes that
# never import this file -- so it gets no tool-call logging from this line.
# Covering that path means touching each app.py individually.
main.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=500))

# Only the servers entrypoint.sh declares are ever imported. Globbing for app.py
# would turn any file that lands in the tree - a stray copy, a mounted volume, a
# planted module - into code this process runs with the servers' access. The
# entrypoint's SERVER_CMDS table is already the one place a server is declared,
# so the allowlist is read from it rather than kept as a second list to drift.
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# Two launch forms exist: `fastmcp run /app/<kind>/<name>/app.py` for most, and
# `python3 -m <kind>.<name>.app` for the ones that need their own launcher.
_DECLARED = re.compile(r'SERVER_CMDS\["[^"]+"\]=".*?(?:/app/(servers|software)/([^/"]+)/app\.py'
                       r'|-m (servers|software)\.([^.\s"]+)\.app)')


def _declared_servers(entrypoint: Path = Path("/app/entrypoint.sh")) -> list[tuple[str, str]]:
    """(kind, name) for every server the entrypoint declares, names checked."""
    out = []
    for path_kind, path_name, mod_kind, mod_name in _DECLARED.findall(entrypoint.read_text()):
        kind, name = (path_kind, path_name) if path_kind else (mod_kind, mod_name)
        if _NAME.match(name):
            out.append((kind, name))
        else:
            print(f"[light-servers] refuse {kind}/{name}: not a plain module name", flush=True)
    return sorted(set(out))


for _kind, _name in _declared_servers():
    try:
        _mod = importlib.import_module(f"{_kind}.{_name}.app")
        _sub = getattr(_mod, "mcp", None)
        if _sub is not None:
            main.mount(_sub, _name if _kind == "servers" else _name.lower())
    except Exception as _e:
        print(f"[light-servers] skip {_kind}/{_name}: {_e}", flush=True)

# anything with an app.py the entrypoint does not declare is reported, not run
for _app_path in sorted(Path("/app/servers").glob("*/app.py")) + sorted(Path("/app/software").glob("*/app.py")):
    _pair = (_app_path.parent.parent.name, _app_path.parent.name)
    if _pair not in set(_declared_servers()):
        print(f"[light-servers] ignore undeclared {_pair[0]}/{_pair[1]}/app.py", flush=True)

if __name__ == "__main__":
    main.run(transport="streamable-http", port=9000, host="0.0.0.0")
