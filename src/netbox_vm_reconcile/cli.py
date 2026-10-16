"""Noninteractive CLI. Every mutation requires an explicit --apply."""

import argparse
import json
import logging
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .config import load
from .identity import SafetyError, uuid
from .logging_config import configure
from .mappings import Store, lock
from .netbox import NetBox
from .reconcile import Engine
from .vmware import VMware


def common_options() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    mode = common.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    mode.add_argument("--apply", action="store_true", default=argparse.SUPPRESS)
    return common


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="netbox-vm-reconcile", parents=[common_options()])
    p.set_defaults(
        config="/etc/netbox-vm-reconcile/config.yaml", verbose=False, dry_run=False, apply=False
    )
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("check", "bootstrap", "reconcile", "conflicts", "inventory"):
        commands.add_parser(name, parents=[common_options()])
    mappings = commands.add_parser("mappings", parents=[common_options()])
    subs = mappings.add_subparsers(dest="action", required=True)
    for name in ("init", "migrate", "list", "export", "backup", "bind"):
        sub = subs.add_parser(name, parents=[common_options()])
        if name == "backup":
            sub.add_argument("destination", type=Path)
        if name == "bind":
            sub.add_argument("--source", required=True)
            sub.add_argument("--instance-uuid", required=True)
            sub.add_argument("--netbox-id", type=int, required=True)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    configure(args.verbose)
    os.umask(0o077)
    try:
        if args.apply and args.dry_run:
            raise SafetyError("--apply and --dry-run cannot be combined")
        if args.apply and args.command in {"check", "conflicts", "inventory"}:
            raise SafetyError("Diagnostic commands do not support --apply")
        if args.command == "mappings" and args.action != "bind":
            config = load(args.config, credentials=False)
        else:
            config = load(args.config)
        db = config.database
        with lock(db.with_suffix(db.suffix + ".lock")):
            if args.command == "mappings" and args.action in {"init", "migrate"}:
                if not args.apply:
                    raise SafetyError("State initialization/migration requires --apply")
                if args.action == "init":
                    Store.initialize(db)
                else:
                    Store.migrate(db)
                return 0
            if args.command == "inventory":
                with VMware(config.vcenters) as vmware:
                    print(json.dumps([asdict(vm) for vm in vmware.inventory()], indent=2))
                return 0
            store = Store(db, writable=args.apply)
            try:
                if args.command == "mappings":
                    if args.action in {"list", "export"}:
                        print(store.export())
                        return 0
                    if args.action == "backup":
                        if not args.apply:
                            raise SafetyError("Writing a backup requires --apply")
                        store.backup(args.destination.absolute())
                        return 0
                netbox = NetBox(config)
                try:
                    with VMware(config.vcenters) as vmware:
                        engine = Engine(config, store, vmware, netbox)
                        if args.command == "bootstrap":
                            return engine.bootstrap(args.apply)
                        if args.command == "mappings":
                            engine.bind(
                                args.source, uuid(args.instance_uuid), args.netbox_id, args.apply
                            )
                            return 0
                        return engine.reconcile(args.apply, check=args.command == "check")
                finally:
                    netbox.close()
            finally:
                store.close()
    except (SafetyError, OSError, sqlite3.Error) as exc:
        # OSError can include credential-bearing filenames. Only safety messages are displayed.
        message = str(exc) if isinstance(exc, SafetyError) else "Local state/file operation failed"
        logging.getLogger(__name__).error("%s", message)
        return 2
    except Exception:
        logging.getLogger(__name__).error("Unexpected failure; synchronization must remain stopped")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
