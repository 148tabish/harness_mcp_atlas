"""CLI entry: python -m bbridge [--host --port --check]."""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .bridge import Config, build_app
from .credentials import CredentialsError, EnvKeyProvider


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bbridge")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8767)
    p.add_argument("--log-level", default="info")
    p.add_argument("--check", action="store_true", help="Print config summary and exit.")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = Config()
    provider = EnvKeyProvider()

    print(f"[bbridge] upstream = {cfg.upstream_url}")
    print(f"[bbridge] model_arn = {cfg.model_arn or '(MISSING: set BB_MODEL_ARN)'}")
    print(f"[bbridge] region = {cfg.region}")
    print(f"[bbridge] reasoning_effort_override = {cfg.reasoning_effort or '(from thinking budget)'}")
    print(f"[bbridge] bridge_secret_set = {bool(cfg.bridge_secret)}")

    rc = 0
    try:
        key = provider.get()
        print(f"[bbridge] api_key = {key[:6]}...{key[-4:]} ({len(key)} chars)")
    except CredentialsError as e:
        print(f"[bbridge] ERROR: {e}", file=sys.stderr)
        rc = 2
    if not cfg.model_arn:
        print("[bbridge] ERROR: BB_MODEL_ARN is not set", file=sys.stderr)
        rc = 2

    if args.check:
        print("[bbridge] check OK" if rc == 0 else "[bbridge] check FAILED")
        return rc

    print(f"[bbridge] listening on http://{args.host}:{args.port}")
    uvicorn.run(build_app(provider, cfg), host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
