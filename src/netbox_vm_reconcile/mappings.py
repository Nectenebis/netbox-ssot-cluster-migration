"""Explicit state initialization, durable one-to-one mappings, and process exclusion."""

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .identity import VM, SafetyError

SCHEMA = """
CREATE TABLE metadata (version INTEGER NOT NULL CHECK (version = 1));
INSERT INTO metadata VALUES (1);
CREATE TABLE mappings (
 vcenter_uuid TEXT NOT NULL, vm_instance_uuid TEXT NOT NULL, vm_bios_uuid TEXT NOT NULL,
 netbox_vm_id INTEGER NOT NULL UNIQUE CHECK (netbox_vm_id > 0), source_name TEXT NOT NULL,
 last_known_name TEXT NOT NULL, last_known_cluster_id INTEGER NOT NULL,
 last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (vcenter_uuid, vm_instance_uuid)
);
CREATE INDEX mappings_source ON mappings(source_name);
"""


@contextmanager
def lock(path: Path):
    """All CLI operations share this persistent inode; never unlink a lock file."""
    try:
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SafetyError("Another reconciler holds the state lock") from exc
            yield
    except OSError as exc:
        raise SafetyError("State directory or lock unavailable") from exc


class Store:
    def __init__(self, path: Path, writable: bool = False):
        self.path = path
        try:
            mode = "rw" if writable else "ro"
            self.db = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA synchronous=FULL")
            if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SafetyError("SQLite integrity check failed; restore a backup")
            if self.db.execute("SELECT version FROM metadata").fetchall()[0][0] != 1:
                raise SafetyError("Unsupported SQLite schema")
            self.db.execute("SELECT * FROM mappings LIMIT 0")
        except (sqlite3.Error, IndexError) as exc:
            raise SafetyError(
                "State missing, corrupt, or unreadable; explicitly init/restore it"
            ) from exc

    @staticmethod
    def initialize(path: Path) -> None:
        # O_EXCL prevents replacement of any pre-existing state.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(path)
        try:
            with db:
                db.executescript(SCHEMA)
        finally:
            db.close()

    def close(self) -> None:
        self.db.close()

    def rows(self) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM mappings ORDER BY source_name, netbox_vm_id")
        ]

    def bind(self, vm: VM, netbox_id: int, cluster_id: int) -> None:
        try:
            with self.db:
                self.db.execute(
                    """INSERT INTO mappings
                    (vcenter_uuid, vm_instance_uuid, vm_bios_uuid, netbox_vm_id, source_name,
                     last_known_name, last_known_cluster_id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (*vm.key, vm.bios_uuid, netbox_id, vm.source, vm.name, cluster_id),
                )
        except sqlite3.IntegrityError as exc:
            raise SafetyError("Mapping already bound; automatic rebind is forbidden") from exc

    def seen(self, vm: VM, cluster_id: int) -> None:
        with self.db:
            self.db.execute(
                """UPDATE mappings SET last_known_name=?, last_known_cluster_id=?,
                last_seen=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE vcenter_uuid=? AND vm_instance_uuid=?""",
                (vm.name, cluster_id, *vm.key),
            )

    def backup(self, destination: Path) -> None:
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
        finally:
            target.close()

    def export(self) -> str:
        return json.dumps({"schema_version": 1, "mappings": self.rows()}, indent=2)
