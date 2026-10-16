import sqlite3
from pathlib import Path

import pytest

from netbox_vm_reconcile import cli
from netbox_vm_reconcile.identity import SafetyError, uuid
from netbox_vm_reconcile.mappings import Store, lock


def test_missing_db_is_never_automatically_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(SafetyError, match="State missing"):
        Store(path, True)
    assert not path.exists()


def test_corrupt_db_rejected(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(SafetyError):
        Store(path)
    assert path.read_bytes() == b"not a sqlite database"


def test_initialization_never_overwrites(env):
    before = env.cfg.database.read_bytes()
    with pytest.raises(FileExistsError):
        Store.initialize(env.cfg.database)
    assert env.cfg.database.read_bytes() == before


def test_one_to_one_constraints(env):
    from dataclasses import replace

    env.bind()
    with pytest.raises(SafetyError):
        env.store.bind(env.vm, 4000, 10)
    with pytest.raises(SafetyError):
        env.store.bind(replace(env.vm, instance_uuid="another"), 3219, 10)
    assert len(env.store.rows()) == 1


def test_backup_export(env, tmp_path):
    import json

    env.bind()
    destination = tmp_path / "backup.db"
    env.store.backup(destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    restored = Store(destination)
    assert restored.rows() == env.store.rows()
    restored.close()
    with pytest.raises(FileExistsError):
        env.store.backup(destination)
    assert json.loads(env.store.export())["mappings"][0]["netbox_vm_id"] == 3219


def test_read_only_store_cannot_mutate(env):
    ro = Store(env.cfg.database)
    with pytest.raises(sqlite3.OperationalError):
        ro.bind(env.vm, 3219, 10)
    ro.close()


def test_process_lock(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "lock"
    code = (
        "from pathlib import Path; from netbox_vm_reconcile.mappings import lock; "
        f"\nwith lock(Path({str(path)!r})): pass"
    )
    with lock(path):
        child = subprocess.run([sys.executable, "-c", code], capture_output=True)
        assert child.returncode != 0
        assert b"Another reconciler" in child.stderr
    with lock(path):
        pass


@pytest.mark.parametrize(
    "args",
    [
        ["--config", "custom.yaml", "reconcile", "--dry-run"],
        ["reconcile", "--config", "custom.yaml", "--dry-run"],
    ],
)
def test_cli_options_work_before_or_after_command(args):
    parsed = cli.parser().parse_args(args)
    assert parsed.config == "custom.yaml" and parsed.dry_run and not parsed.apply


def test_cli_default_safe():
    assert not cli.parser().parse_args(["reconcile"]).apply


def test_cli_failure_propagates_nonzero(monkeypatch, caplog):
    def fail(path):
        raise SafetyError("vCenter unavailable")

    monkeypatch.setattr(cli, "load", fail)
    assert cli.main(["reconcile", "--apply"]) == 2


def test_systemd_execstartpre_has_no_failure_override():
    path = Path(__file__).parents[1] / "packaging/systemd/netbox-ssot-reconcile.conf"
    text = path.read_text()
    assert "ExecStartPre=/opt/" in text
    assert "ExecStartPre=-" not in text
    assert "\nExecStart=" not in text
    assert "--apply" in text


@pytest.mark.parametrize("value", [None, "", "bad", "00000000-0000-0000-0000-000000000000"])
def test_uuid_missing_or_invalid(value):
    with pytest.raises(SafetyError):
        uuid(value)
