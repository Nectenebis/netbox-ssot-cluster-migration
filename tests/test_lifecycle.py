import logging
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from netbox_vm_reconcile.identity import SafetyError

from .conftest import record


def another(vm, number=1, **changes):
    return replace(
        vm,
        instance_uuid=str(UUID(int=1000 + number)),
        bios_uuid=str(UUID(int=2000 + number)),
        moref=f"vm-new-{number}",
        name=f"new-vm-{number}",
        **changes,
    )


@pytest.mark.parametrize("command", ["bootstrap", "reconcile"])
@pytest.mark.parametrize("apply", [False, True])
def test_new_vm_absent_from_netbox_is_left_for_ssot(env, caplog, command, apply):
    caplog.set_level(logging.INFO)
    env.nb.records.clear()
    env.nb.clusters.clear()
    env.nb.devices.clear()  # SSoT may still need to create a new Cluster and ESXi.
    before = env.cfg.database.read_bytes()
    assert getattr(env.engine(), command)(apply) == 0
    assert "NEW VM" in caplog.text and "left for SSoT" in caplog.text
    assert not env.store.rows()
    assert not env.nb.patches
    assert env.cfg.database.read_bytes() == before


def test_ssot_creates_vm_then_next_reconcile_registers_and_preserves_identity(env):
    env.nb.records.clear()
    assert env.engine().reconcile(True) == 0
    assert not env.store.rows()
    env.nb.records[3219] = record()  # Simulate a successful subsequent SSoT run.
    assert env.engine().reconcile(True) == 0
    assert env.store.rows()[0]["netbox_vm_id"] == 3219
    assert not env.nb.patches
    env.migrate(name="renamed", cluster_ref="domain-c102", host_ref="host-202")
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches == [(3219, {"name": "renamed", "cluster": 20, "device": 102, "site": 2})]
    assert env.engine().reconcile(True) == 0
    assert len(env.nb.patches) == 1


@pytest.mark.parametrize("command", ["bootstrap", "reconcile"])
def test_automatic_binding_dry_run_does_not_write_state(env, command):
    before = env.cfg.database.read_bytes()
    assert getattr(env.engine(), command)() == 0
    assert not env.store.rows()
    assert not env.nb.patches
    assert env.cfg.database.read_bytes() == before


@pytest.mark.parametrize("command", ["bootstrap", "reconcile"])
@pytest.mark.parametrize("apply", [False, True])
def test_deleted_vm_mapping_cleanup_never_touches_netbox(env, command, apply):
    env.bind()
    before = env.cfg.database.read_bytes()
    original = deepcopy(env.nb.records)
    env.vw.vms = []
    assert getattr(env.engine(), command)(apply) == 0
    assert len(env.store.rows()) == (0 if apply else 1)
    assert env.nb.records == original
    assert not env.nb.patches
    assert env.store.vcenters() == env.vw.identities()  # vCenter pin is retained.
    if not apply:
        assert env.cfg.database.read_bytes() == before


def test_pending_vm_does_not_prevent_repair_of_known_vm(env):
    env.bind()
    env.vw.vms = [replace(env.vm, host_ref="host-203"), another(env.vm)]
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches == [(3219, {"device": 103})]
    assert len(env.store.rows()) == 1


def test_excluded_template_is_not_mistaken_for_deleted_vm(env):
    env.bind()
    env.vw.vms = []
    env.vw.excluded_keys.add(env.vm.key)
    assert env.engine().reconcile(True) == 0
    assert len(env.store.rows()) == 1
    assert not env.nb.patches


def test_absent_vm_reappearing_as_template_before_commit_is_not_deleted(env, monkeypatch):
    env.bind()
    env.vw.vms = []
    engine = env.engine()

    def final_inventory():
        env.vw.excluded_keys.add(env.vm.key)
        return []

    monkeypatch.setattr(env.vw, "inventory", final_inventory)
    assert engine.reconcile(True) == 0
    assert len(env.store.rows()) == 1


@pytest.mark.parametrize("failure", ["initial", "final"])
def test_vcenter_failure_never_removes_missing_mapping(env, monkeypatch, failure):
    env.bind()
    env.vw.vms = []
    before = env.cfg.database.read_bytes()

    def unavailable():
        raise SafetyError("vCenter unavailable")

    if failure == "initial":
        monkeypatch.setattr(env.vw, "inventory", unavailable)
        with pytest.raises(SafetyError, match="unavailable"):
            env.engine()
    else:
        engine = env.engine()
        monkeypatch.setattr(env.vw, "inventory", unavailable)
        assert engine.reconcile(True) == 2
    assert env.cfg.database.read_bytes() == before
    assert len(env.store.rows()) == 1


def test_inventory_drift_discards_bindings_and_cleanup(env, monkeypatch):
    env.bind()
    new = another(env.vm)
    env.vw.vms = [new]
    env.nb.records[4000] = record(4000, new.name)
    before = env.cfg.database.read_bytes()
    engine = env.engine()
    monkeypatch.setattr(env.vw, "inventory", lambda: [replace(new, name="changed-again")])
    assert engine.reconcile(True) == 2
    assert env.cfg.database.read_bytes() == before
    assert [r["netbox_vm_id"] for r in env.store.rows()] == [3219]


@pytest.mark.parametrize("retained_identity", ["name", "bios"])
def test_changed_uuid_is_not_treated_as_new_and_old_mapping_is_retained(env, retained_identity):
    env.bind()
    changes = {
        "instance_uuid": str(UUID(int=1001)),
        "name": "renamed",
        "bios_uuid": str(UUID(int=2001)),
    }
    changes["name" if retained_identity == "name" else "bios_uuid"] = (
        env.vm.name if retained_identity == "name" else env.vm.bios_uuid
    )
    env.migrate(**changes)
    for _ in range(2):
        assert env.engine().reconcile(True) == 2
        assert env.store.rows()[0]["vm_instance_uuid"] == env.vm.instance_uuid
        assert not env.nb.patches


@pytest.mark.parametrize("cid", [10, 20])
def test_foreign_name_is_allowed_only_in_a_different_target_cluster(env, cid):
    env.nb.records[3219] = record(source="ovirt", cid=cid)
    assert env.engine().reconcile(True) == (2 if cid == 10 else 0)
    assert not env.store.rows()
    assert not env.nb.patches


def test_netbox_candidate_appearing_during_pending_discovery_requires_retry(env, monkeypatch):
    env.nb.records.clear()
    monkeypatch.setattr(env.nb, "named", lambda name: [record()])
    assert env.engine().reconcile(True) == 2
    assert not env.store.rows()
    assert not env.nb.patches


@pytest.mark.parametrize("count", [1, 639])
def test_bulk_bootstrap_and_repeated_seen_updates_use_one_transaction(env, count):
    vms = [another(env.vm, number=i) for i in range(count)]
    env.vw.vms = vms
    env.nb.records = {10000 + i: record(10000 + i, vm.name) for i, vm in enumerate(vms)}
    statements = []
    env.store.db.set_trace_callback(statements.append)
    assert env.engine().bootstrap(True) == 0
    assert len(env.store.rows()) == count
    assert sum(s == "COMMIT" for s in statements) == 1
    assert sum(s.startswith("BEGIN") for s in statements) == 1
    statements.clear()
    assert env.engine().reconcile(True) == 0
    assert sum(s == "COMMIT" for s in statements) == 1
    assert sum(s.startswith("BEGIN") for s in statements) == 1
    assert not env.nb.patches


def test_network_checks_are_never_inside_sqlite_write_transaction(env, monkeypatch):
    for obj, method in (
        (env.vw, "inventory"),
        (env.vw, "refresh"),
        (env.nb, "snapshot"),
        (env.nb, "get_vm"),
        (env.nb, "named"),
    ):
        original = getattr(obj, method)

        def checked(*args, original=original, **kwargs):
            assert not env.store.db.in_transaction
            return original(*args, **kwargs)

        monkeypatch.setattr(obj, method, checked)
    assert env.engine().bootstrap(True) == 0


def test_binding_cleanup_and_seen_batch_rolls_back_on_unique_conflict(env):
    env.bind()
    before = env.store.rows()
    new1, new2 = another(env.vm, 1), another(env.vm, 2)
    with pytest.raises(SafetyError, match="rolled back"):
        env.store.apply_batch(
            [(new1, 4000, 10), (new2, 4000, 10)],
            [(*env.vm.key, 3219)],
            [(replace(env.vm, name="changed"), 20)],
            {"other": "44444444-4444-4444-8444-444444444444"},
        )
    assert env.store.rows() == before
    assert "other" not in env.store.vcenters()


def test_bindings_removal_and_seen_share_one_commit(env):
    env.bind()
    new = another(env.vm)
    statements = []
    env.store.db.set_trace_callback(statements.append)
    env.store.apply_batch([(new, 4000, 10)], [(*env.vm.key, 3219)], [(new, 20)])
    assert [r["netbox_vm_id"] for r in env.store.rows()] == [4000]
    assert env.store.rows()[0]["last_known_cluster_id"] == 20
    assert sum(s == "COMMIT" for s in statements) == 1


def test_sqlite_failure_does_not_undo_a_confirmed_netbox_patch(env, monkeypatch):
    import sqlite3

    env.bind()
    env.migrate(name="renamed")
    batch = env.store.apply_batch

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("state unavailable")

    monkeypatch.setattr(env.store, "apply_batch", fail)
    with pytest.raises(sqlite3.OperationalError):
        env.engine().reconcile(True)
    assert env.nb.records[3219]["name"] == "renamed"
    assert env.store.rows()[0]["last_known_name"] == "mnetbox"
    monkeypatch.setattr(env.store, "apply_batch", batch)
    assert env.engine().reconcile(True) == 0
    assert len(env.nb.patches) == 1
    assert env.store.rows()[0]["last_known_name"] == "renamed"


def test_same_preflight_command_handles_create_register_delete_cycle(env, monkeypatch):
    from contextlib import nullcontext

    from netbox_vm_reconcile import cli

    monkeypatch.setattr(cli, "load", lambda *a, **k: env.cfg)
    monkeypatch.setattr(cli, "VMware", lambda configs: nullcontext(env.vw))
    monkeypatch.setattr(cli, "NetBox", lambda config: env.nb)
    monkeypatch.setattr(env.nb, "close", lambda: None, raising=False)
    env.nb.records.clear()
    assert cli.main(["reconcile", "--apply"]) == 0  # ExecStartPre lets SSoT create it.
    assert not env.store.rows()
    env.nb.records[3219] = record()
    assert cli.main(["reconcile", "--apply"]) == 0  # Next run enrolls it automatically.
    assert env.store.rows()[0]["netbox_vm_id"] == 3219
    env.vw.vms = []
    assert cli.main(["reconcile", "--apply"]) == 0
    assert not env.store.rows()
    assert 3219 in env.nb.records  # Only SSoT may remove the NetBox VM later.
    assert not env.nb.patches


def test_netbox_error_prevents_cleanup_of_unrelated_absent_mapping(env, monkeypatch):
    env.bind()
    second = another(env.vm)
    env.store.bind(second, 4000, 10)
    env.nb.records[4000] = record(4000, second.name)
    env.vw.vms = [second]
    before = env.store.rows()

    def fail(pk):
        raise SafetyError("NetBox API unavailable")

    monkeypatch.setattr(env.nb, "get_vm", fail)
    assert env.engine().reconcile(True) == 2
    assert env.store.rows() == before
