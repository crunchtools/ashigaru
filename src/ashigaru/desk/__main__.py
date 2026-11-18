"""ashigaru-desk: run the service and do what has to happen on its host.

`mint-key` is the bootstrap: the first admin key cannot come from the API,
because every API call needs a key. It writes the token to a new file, mode
0600, the form a podman secret or a mounted key file takes; it never prints it.

`serve` and `health` read their settings from the flags, else from SETTINGS'
environment variables, which is how the container image configures them.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from http import HTTPStatus

from . import auth, importer, store
from .forges import DEFAULT, FORGES
from .model import Role

SETTINGS = {
    "ASHIGARU_DESK_DB": "the SQLite file (serve, health); the image sets /data/desk.db",
    "ASHIGARU_BIND": "the address serve listens on; 127.0.0.1 by default, 0.0.0.0 in the image",
    "ASHIGARU_PORT": "the port serve listens on; 8080 by default",
    "ASHIGARU_TRUSTED_PROXIES": (
        "comma-separated proxy addresses or CIDRs whose X-Forwarded-For and X-Forwarded-Proto are"
        " believed; none by default, so a direct client cannot forge its address or scheme"
    ),
}
SETTINGS["ASHIGARU_WEBHOOK_SECRET"] = f"the {DEFAULT} webhook secret, kept for the first deploy (serve)"
SETTINGS["ASHIGARU_WEBHOOK_SECRET_FILE"] = f"a file of {DEFAULT} webhook secrets, one per line (serve)"
for _forge in FORGES:
    SETTINGS[f"ASHIGARU_FORGE_TOKEN_{_forge.upper()}"] = (
        f"a {_forge} API token that can read collaborator permissions (serve); without it, intake is held"
    )
    SETTINGS[f"ASHIGARU_FORGE_TOKEN_{_forge.upper()}_FILE"] = f"a file holding that {_forge} token (serve)"
    SETTINGS[f"ASHIGARU_FORGE_AUTHORS_{_forge.upper()}"] = (
        f"comma-separated {_forge} accounts, such as the app's bot, whose own issues and PRs are "
        "trusted as a writer's, so auto-claimable: a lane label on one sets its lane and mode auto (serve)"
    )
    SETTINGS[f"ASHIGARU_WEBHOOK_SECRET_{_forge.upper()}"] = f"the {_forge} webhook secret (serve)"
    SETTINGS[f"ASHIGARU_WEBHOOK_SECRET_{_forge.upper()}_FILE"] = (
        f"a file of {_forge} webhook secrets, one per line, for rotation (serve)"
    )
"""Every environment variable the service reads, documented in docs/desk-api.md under Deploy."""


def setting(name: str, fallback: str = "") -> str:
    if name not in SETTINGS:
        raise KeyError(f"{name} is not a documented setting")
    return os.environ.get(name, fallback)


def proxies(value: str) -> list[str]:
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def _secrets_in(variable: str) -> list[str]:
    """The secret in `variable` and those in the file `<variable>_FILE` names, one per line."""
    values = [setting(variable)]
    path = setting(f"{variable}_FILE")
    if path:
        with open(path, encoding="utf-8") as secrets:
            values += secrets.read().splitlines()
    return values


def webhook_secrets() -> dict[str, list[bytes]]:
    """Each forge's secrets from ASHIGARU_WEBHOOK_SECRET_<FORGE> and its file; the bare
    ASHIGARU_WEBHOOK_SECRET and its file count for DEFAULT. Never from the database or a flag."""
    found: dict[str, list[bytes]] = {}
    for forge in FORGES:
        values = _secrets_in(f"ASHIGARU_WEBHOOK_SECRET_{forge.upper()}")
        if forge == DEFAULT:
            values += _secrets_in("ASHIGARU_WEBHOOK_SECRET")
        found[forge] = [value.strip().encode() for value in values if value.strip()]
    return found


def forge_tokens() -> dict[str, str]:
    """Each forge's API token from ASHIGARU_FORGE_TOKEN_<FORGE>, else the first line of its file."""
    found: dict[str, str] = {}
    for forge in FORGES:
        values = [value.strip() for value in _secrets_in(f"ASHIGARU_FORGE_TOKEN_{forge.upper()}")]
        found[forge] = next((value for value in values if value), "")
    return found


def forge_authors() -> dict[str, list[str]]:
    """Each forge's trusted authors from ASHIGARU_FORGE_AUTHORS_<FORGE>."""
    return {forge: proxies(setting(f"ASHIGARU_FORGE_AUTHORS_{forge.upper()}")) for forge in FORGES}


def _open(path: str) -> sqlite3.Connection:
    conn = store.connect(path)
    store.migrate(conn)
    return conn


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    app = create_app(
        args.db,
        trusted_proxies=proxies(args.trusted_proxies),
        webhook_secrets=webhook_secrets(),
        forge_tokens=forge_tokens(),
        forge_authors=forge_authors(),
    )
    # The app applies the proxy headers itself, and only from trusted_proxies; uvicorn's own
    # handling would trust 127.0.0.1 by default.
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, server_header=False)
    return 0


def health(args: argparse.Namespace) -> int:
    """The image's HEALTHCHECK: 0 when the local /healthz answers 200."""
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    url = f"http://{'[' + host + ']' if ':' in host else host}:{args.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 0 if response.status == HTTPStatus.OK else 1
    except OSError as error:
        print(f"{url}: {error}", file=sys.stderr)
        return 1


def import_ledger(args: argparse.Namespace) -> int:
    with open(args.ledger, encoding="utf-8") as ledger:
        summary = importer.import_ledger(_open(args.db), ledger)
    print(
        f"{summary.lines} lines: {summary.applied} applied, {summary.skipped} already imported; "
        f"{len(summary.created)} items created, {len(summary.updated)} updated, {len(summary.keyed)} keyed"
    )
    return 0


def mint_key(args: argparse.Namespace) -> int:
    conn = _open(args.db)
    existing = store.get_identity(conn, args.identity)
    if existing and existing["role"] != args.role and not args.change_role:
        print(
            f"{args.identity} is {existing['role']}, not {args.role}; pass --change-role to change it",
            file=sys.stderr,
        )
        return 1
    try:
        descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{args.out} exists; mint-key writes only a new file", file=sys.stderr)
        return 1
    with os.fdopen(descriptor, "w", encoding="utf-8") as out, store.transaction(conn):
        if existing is None:
            store.create_identity(conn, args.identity, args.role)
        elif existing["role"] != args.role:
            store.set_role(conn, args.identity, args.role)
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

    db = setting("ASHIGARU_DESK_DB")
    listen = argparse.ArgumentParser(add_help=False)
    listen.add_argument("--host", default=setting("ASHIGARU_BIND", "127.0.0.1"), help="env ASHIGARU_BIND")
    listen.add_argument(
        "--port", type=int, default=int(setting("ASHIGARU_PORT", "8080")), help="env ASHIGARU_PORT"
    )

    run = commands.add_parser("serve", parents=[listen], help="run the REST API")
    run.add_argument(
        "--db", default=db or None, required=not db, help="the SQLite file; env ASHIGARU_DESK_DB"
    )
    run.add_argument(
        "--trusted-proxies",
        default=setting("ASHIGARU_TRUSTED_PROXIES"),
        help="comma-separated addresses or CIDRs; env ASHIGARU_TRUSTED_PROXIES",
    )
    run.set_defaults(func=serve)

    commands.add_parser(
        "health", parents=[listen], help="exit 0 when the local /healthz answers"
    ).set_defaults(func=health)

    load = commands.add_parser("import", help="fold a JSONL agents ledger in; safe to re-run")
    load.add_argument("ledger")
    load.add_argument("--db", required=True, help="the SQLite file; created and migrated if new")
    load.set_defaults(func=import_ledger)

    key = commands.add_parser("mint-key", help="create the identity if missing and write a new key to --out")
    key.add_argument("identity")
    key.add_argument("--role", choices=[role.value for role in Role], required=True)
    key.add_argument("--label")
    key.add_argument("--change-role", action="store_true", help="change an existing identity's role")
    key.add_argument("--out", required=True, help="a new file to write the token to, mode 0600")
    key.add_argument("--db", required=True, help="the SQLite file; created and migrated if new")
    key.set_defaults(func=mint_key)

    commands.add_parser("openapi", help="print the API contract").set_defaults(func=openapi)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
