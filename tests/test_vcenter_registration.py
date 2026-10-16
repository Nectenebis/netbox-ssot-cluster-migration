import json
import sqlite3
from dataclasses import replace

import pytest

from netbox_vm_reconcile.identity import SafetyError
from netbox_vm_reconcile.mappings import Store

from .conftest import VC


@pytest.fixture
def unregistered(env):
    with env.store.db:
        env.store.db.execute("DELETE FROM vcenters")
    env.cfg = replace(env.cfg, vcenters=[replace(env.cfg.vcenters[0], expected_uuid=None)])
    return env


def test_bootstrap_registers_observed_uuid_but_dry_run_does_not(unregistered):
    env = unregistered
    before = env.cfg.database.read_bytes()
    assert env.engine().bootstrap() == 0
    assert env.cfg.database.read_bytes() == before
    assert env.store.vcenters() == {}
    assert env.engine().bootstrap(True) == 0
    assert env.store.vcenters() == {"mvcenter": VC}
    assert env.engine().reconcile(True) == 0
    assert json.loads(env.store.export())["vcenters"] == {"mvcenter": VC}


def test_regular_apply_cannot_silently_register_vcenter(unregistered):
    with pytest.raises(SafetyError, match="Unregistered vCenter"):
        unregistered.engine().reconcile(True)
    assert not unregistered.store.vcenters()
    assert not unregistered.nb.patches


def test_vcenter_is_registered_even_when_inventory_empty(unregistered):
    unregistered.vw.vms = []
    assert unregistered.engine().bootstrap(True) == 0
    assert unregistered.store.vcenters() == {"mvcenter": VC}


@pytest.mark.parametrize("command", ["bootstrap", "reconcile"])
def test_changed_uuid_blocks_all_writes_without_yaml_pin(env, command):
    env.bind()
    env.cfg = replace(env.cfg, vcenters=[replace(env.cfg.vcenters[0], expected_uuid=None)])
    changed = "44444444-4444-4444-8444-444444444444"
    env.migrate(vcenter_uuid=changed)
    before = env.cfg.database.read_bytes()
    with pytest.raises(SafetyError, match="Registered vCenter UUID/source changed"):
        getattr(env.engine(), command)(True)
    assert env.cfg.database.read_bytes() == before
    assert not env.nb.patches


def test_same_uuid_cannot_be_registered_under_two_source_names(env):
    with pytest.raises(SafetyError, match="another registered source"):
        env.store.register_vcenters({"another-name": VC})
    assert env.store.vcenters() == {"mvcenter": VC}


def test_registration_of_multiple_sources_is_atomic(env):
    with pytest.raises(SafetyError):
        env.store.register_vcenters({"new": "44444444-4444-4444-8444-444444444444", "conflict": VC})
    assert env.store.vcenters() == {"mvcenter": VC}


def legacy_database(path, vm, conflicting=False):
    # Reproduce the original v1 CHECK constraint and the original mappings table.
    from netbox_vm_reconcile.mappings import SCHEMA

    mapping_schema = SCHEMA[SCHEMA.index("CREATE TABLE mappings") :]
    db = sqlite3.connect(path)
    db.executescript(
        """CREATE TABLE metadata (version INTEGER NOT NULL CHECK (version = 1));
        INSERT INTO metadata VALUES (1);"""
        + mapping_schema
    )
    with db:
        db.execute(
            """INSERT INTO mappings (vcenter_uuid, vm_instance_uuid, vm_bios_uuid,
            netbox_vm_id, source_name, last_known_name, last_known_cluster_id)
            VALUES (?, ?, ?, 3219, ?, ?, 10)""",
            (vm.vcenter_uuid, vm.instance_uuid, vm.bios_uuid, vm.source, vm.name),
        )
        if conflicting:
            db.execute(
                """INSERT INTO mappings (vcenter_uuid, vm_instance_uuid, vm_bios_uuid,
                netbox_vm_id, source_name, last_known_name, last_known_cluster_id)
                VALUES ('another-uuid', 'another-vm', 'another-bios', 4000, ?, 'other', 10)""",
                (vm.source,),
            )
    rows = db.execute("SELECT * FROM mappings").fetchall()
    db.close()
    return rows


def test_migrate_v1_preserves_mappings_and_learns_uuid_from_saved_state(env, tmp_path):
    path = tmp_path / "legacy.db"
    before = legacy_database(path, env.vm)
    with pytest.raises(SafetyError, match="mappings migrate"):
        Store(path)
    Store.migrate(path)
    db = Store(path)
    assert db.vcenters() == {"mvcenter": VC}
    assert db.db.execute("SELECT * FROM mappings").fetchall()[0]["netbox_vm_id"] == 3219
    assert [tuple(r) for r in db.db.execute("SELECT * FROM mappings")] == before
    db.close()
    Store.migrate(path)  # Repeatable, no reset.


def test_conflicting_legacy_identity_rolls_back_migration(env, tmp_path):
    path = tmp_path / "conflicting-legacy.db"
    before = legacy_database(path, env.vm, conflicting=True)
    with pytest.raises(SafetyError, match="rolled back"):
        Store.migrate(path)
    db = sqlite3.connect(path)
    assert db.execute("SELECT version FROM metadata").fetchone()[0] == 1
    assert db.execute("SELECT * FROM mappings").fetchall() == before
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='vcenters'").fetchall()
    db.close()


def test_migration_never_creates_missing_state(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        Store.migrate(path)
    assert not path.exists()


def test_cli_migration_requires_apply_and_preserves_legacy_mapping(env, tmp_path, monkeypatch):
    from netbox_vm_reconcile import cli

    path = tmp_path / "legacy-cli.db"
    before = legacy_database(path, env.vm)
    monkeypatch.setattr(cli, "load", lambda *a, **k: replace(env.cfg, database=path))
    assert cli.main(["mappings", "migrate", "--dry-run"]) == 2
    assert cli.main(["mappings", "migrate", "--apply"]) == 0
    migrated = Store(path)
    assert [tuple(r) for r in migrated.db.execute("SELECT * FROM mappings")] == before
    assert migrated.vcenters() == {"mvcenter": VC}
    migrated.close()
