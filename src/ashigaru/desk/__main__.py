"""ashigaru-desk: run the service and do what has to happen on its host.

`mint-key` is the bootstrap: the first admin key cannot come from the API,
because every API call needs a key. It writes the token to a new file, mode
0600, the form a podman secret or a mounted key file takes; it never prints it.
"""

import argparse
import json
import os
import sqlite3
import sys

from . import auth, importer, store
from .model import Role


def _open(path: str) -> sqlite3.Connection:
    conn = store.connect(path)
    store.migrate(conn)
    return conn


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(args.db), host=args.host, port=args.port, proxy_headers=True)
    return 0


def import_ledger(args: argparse.Namespace) -> int:
    with open(args.ledger, encoding="utf-8") as ledger:
        summary = importer.import_ledger(_open(args.db), ledger)
    print(
        f"{summary.lines} lines: {summary.applied} applied, {summary.skipped} already imported; "
        f"{len(summary.created)} items created, {len(summary.updated)} updated"
    )
    return 0


def mint_key(args: argparse.Namespace) -> int:
    conn = _open(args.db)
    try:
        descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{args.out} exists; mint-key writes only a new file", file=sys.stderr)
        return 1
    with os.fdopen(descriptor, "w", encoding="utf-8") as out, store.transaction(conn):
        if store.get_identity(conn, args.identity) is None:
            store.create_identity(conn, args.identity, args.role)
        key_id, token = auth.mint(conn, args.identity, args.label)
        out.write(token + "\n")
    print(f"key {key_id} for {args.identity} written to {args.out}")
    return 0


def openapi(args: argparse.Namespace) -> int:
    from .api import create_app

    json.dump(create_app(":memory:").openapi(), sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ashigaru-desk")
    commands = parser.add_subparsers(required=True)

    run = commands.add_parser("serve", help="run the REST API")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8080)
    run.add_argument("--db", required=True, help="the SQLite file; created and migrated if new")
    run.set_defaults(func=serve)

    load = commands.add_parser("import", help="fold a JSONL agents ledger in; safe to re-run")
    load.add_argument("ledger")
    load.add_argument("--db", required=True, help="the SQLite file; created and migrated if new")
    load.set_defaults(func=import_ledger)

    key = commands.add_parser("mint-key", help="create the identity if missing and write a new key to --out")
    key.add_argument("identity")
    key.add_argument("--role", choices=[role.value for role in Role], required=True)
    key.add_argument("--label")
    key.add_argument("--out", required=True, help="a new file to write the token to, mode 0600")
    key.add_argument("--db", required=True, help="the SQLite file; created and migrated if new")
    key.set_defaults(func=mint_key)

    commands.add_parser("openapi", help="print the API contract").set_defaults(func=openapi)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
