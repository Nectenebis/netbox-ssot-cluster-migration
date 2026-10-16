"""Explicit state initialization, durable one-to-one mappings, and process exclusion."""

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .identity import VM, SafetyError

VC_SCHEMA = """CREATE TABLE vcenters (
 source_name TEXT PRIMARY KEY, vcenter_uuid TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""

SCHEMA = (
    """
CREATE TABLE metadata (version INTEGER NOT NULL CHECK (version = 2));
INSERT INTO metadata VALUES (2);
"""
    + VC_SCHEMA
    + ";"
    + """
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
)


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
            version = self.db.execute("SELECT version FROM metadata").fetchall()[0][0]
            if version == 1:
                raise SafetyError(
                    "SQLite schema v1: back up state, then run mappings migrate --apply"
                )
            if version != 2:
                raise SafetyError("Unsupported SQLite schema")
            self.db.execute("SELECT * FROM mappings LIMIT 0")
            self.db.execute("SELECT * FROM vcenters LIMIT 0")
        except SafetyError:
            self.db.close()
            raise
        except (sqlite3.Error, IndexError) as exc:
            if hasattr(self, "db"):
                self.db.close()
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

    @staticmethod
    def migrate(path: Path) -> None:
        """Explicit, transactional v1 upgrade. Never register a UUID from the network here."""
        db = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SafetyError("SQLite integrity check failed; restore a backup")
            version = db.execute("SELECT version FROM metadata").fetchone()[0]
            if version == 2:
                return
            if version != 1:
                raise SafetyError("Unsupported SQLite schema")
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(VC_SCHEMA)
                db.execute("""INSERT INTO vcenters (source_name, vcenter_uuid)
                    SELECT DISTINCT source_name, vcenter_uuid FROM mappings""")
                db.execute("DROP TABLE metadata")
                db.execute("CREATE TABLE metadata (version INTEGER NOT NULL CHECK (version = 2))")
                db.execute("INSERT INTO metadata VALUES (2)")
        except sqlite3.IntegrityError as exc:
            raise SafetyError(
                "Legacy state has conflicting vCenter identities; migration rolled back"
            ) from exc
        finally:
            db.close()

    def vcenters(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT source_name, vcenter_uuid FROM vcenters"))

    def register_vcenters(self, identities: dict[str, str]) -> None:
        try:
            with self.db:
                for source, observed in identities.items():
                    row = self.db.execute(
                        "SELECT vcenter_uuid FROM vcenters WHERE source_name=?", (source,)
                    ).fetchone()
                    if row and row[0] != observed:
                        raise SafetyError(f"Registered vCenter UUID changed for source={source}")
                    if not row:
                        self.db.execute(
                            "INSERT INTO vcenters (source_name, vcenter_uuid) VALUES (?, ?)",
                            (source, observed),
                        )
        except sqlite3.IntegrityError as exc:
            raise SafetyError("vCenter UUID already belongs to another registered source") from exc

    def rows(self) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM mappings ORDER BY source_name, netbox_vm_id")
        ]

    def bind(self, vm: VM, netbox_id: int, cluster_id: int) -> None:
        if self.vcenters().get(vm.source) != vm.vcenter_uuid:
            raise SafetyError("VM must belong to a registered vCenter identity")
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
        return json.dumps(
            {"schema_version": 2, "vcenters": self.vcenters(), "mappings": self.rows()}, indent=2
        )
