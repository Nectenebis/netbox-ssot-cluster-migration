import logging
from dataclasses import replace

import pytest

from netbox_vm_reconcile.reconcile import RECONCILE_ATTEMPTS


@pytest.mark.parametrize(
    "destination, expected",
    [
        ({"host_ref": "host-203"}, {"name": "new", "device": 103}),
        (
            {"host_ref": "host-202", "cluster_ref": "domain-c102"},
            {"name": "new", "cluster": 20, "device": 102, "site": 2},
        ),
    ],
)
def test_move_before_patch_replans_and_writes_only_current_placement(
    env, monkeypatch, caplog, destination, expected
):
    env.bind()
    env.migrate(name="new")
    calls = 0

    def move_once(vm):
        nonlocal calls
        calls += 1
        if calls == 1:
            env.vw.vms = [replace(vm, **destination)]
        return env.vw.vms[0]

    monkeypatch.setattr(env.vw, "refresh", move_once)
    caplog.set_level(logging.INFO)
    statements = []
    env.store.db.set_trace_callback(statements.append)
    assert env.engine().reconcile(True) == 0
    assert calls == 2
    assert env.nb.patches == [(3219, expected)]
    assert env.store.rows()[0]["last_known_cluster_id"] == expected.get("cluster", 10)
    assert sum(s == "COMMIT" for s in statements) == 1
    assert "attempt=2/3" in caplog.text
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("initial_patch", [False, True])
def test_move_detected_in_final_inventory_is_corrected_before_success(
    env, monkeypatch, initial_patch
):
    env.bind()
    if initial_patch:
        env.migrate(host_ref="host-203")
    engine = env.engine()
    reads = 0

    def inventory():
        nonlocal reads
        reads += 1
        if reads == 1:
            env.vw.vms = [replace(env.vm, host_ref="host-202", cluster_ref="domain-c102")]
        return env.vw.vms

    monkeypatch.setattr(env.vw, "inventory", inventory)
    assert engine.reconcile(True) == 0
    expected = (3219, {"cluster": 20, "device": 102, "site": 2})
    assert env.nb.patches == ([(3219, {"device": 103}), expected] if initial_patch else [expected])
    assert env.store.rows()[0]["last_known_cluster_id"] == 20
    assert reads == 3  # Final read, refreshed initial inventory, stable final read.


def test_moving_new_bootstrap_candidate_is_registered_after_refresh(env, monkeypatch):
    calls = 0

    def move_once(vm):
        nonlocal calls
        calls += 1
        if calls == 1:
            env.vw.vms = [replace(vm, host_ref="host-203")]
        return env.vw.vms[0]

    monkeypatch.setattr(env.vw, "refresh", move_once)
    assert env.engine().reconcile(True) == 0
    assert env.store.rows()[0]["netbox_vm_id"] == 3219
    assert env.nb.patches == [(3219, {"device": 103})]


@pytest.mark.parametrize("detection", ["before_patch", "final_inventory"])
def test_continuous_motion_is_bounded_and_does_not_commit_cleanup(
    env, monkeypatch, caplog, detection
):
    env.bind()
    absent = replace(
        env.vm,
        instance_uuid="44444444-4444-4444-8444-444444444444",
        bios_uuid="55555555-5555-4555-8555-555555555555",
        name="deleted-vm",
    )
    env.store.bind(absent, 4000, 10)
    env.migrate(name="new")
    before = env.cfg.database.read_bytes()
    engine = env.engine()
    calls = 0

    def move():
        vm = env.vw.vms[0]
        env.vw.vms = [replace(vm, host_ref="host-203" if vm.host_ref == "host-201" else "host-201")]
        return env.vw.vms[0]

    def refresh(vm):
        nonlocal calls
        calls += 1
        return move()

    def inventory():
        nonlocal calls
        calls += 1
        if calls % 2 == 1:  # Every final read changes; every retry initial read stays stable.
            move()
        return env.vw.vms

    monkeypatch.setattr(
        env.vw,
        detection == "before_patch" and "refresh" or "inventory",
        refresh if detection == "before_patch" else inventory,
    )
    assert engine.reconcile(True) == 2
    assert calls == (RECONCILE_ATTEMPTS if detection == "before_patch" else 5)
    assert "after 3 attempts" in caplog.text
    assert env.cfg.database.read_bytes() == before
    assert len(env.store.rows()) == 2
    if detection == "before_patch":
        assert not env.nb.patches


@pytest.mark.parametrize(
    "change",
    [
        {"instance_uuid": "44444444-4444-4444-8444-444444444444"},
        {"bios_uuid": "44444444-4444-4444-8444-444444444444"},
        {"name": "unexpected-rename"},
        {"moref": "vm-replaced"},
    ],
)
def test_identity_or_name_change_is_not_retried(env, monkeypatch, caplog, change):
    env.bind()
    env.migrate(host_ref="host-203")
    calls = 0

    def refresh(vm):
        nonlocal calls
        calls += 1
        return replace(vm, **change)

    monkeypatch.setattr(env.vw, "refresh", refresh)
    assert env.engine().reconcile(True) == 2
    assert calls == 1
    assert not env.nb.patches
    assert "retrying" not in caplog.text


@pytest.mark.parametrize("conflict", ["ownership", "duplicate", "missing_device", "disabled"])
def test_retry_rechecks_protections_before_patch(env, monkeypatch, conflict):
    from .conftest import record

    env.bind()
    env.migrate(name="new")
    if conflict == "disabled":
        env.cfg = replace(env.cfg, allow_host_change=False)

    def refresh(vm):
        env.vw.vms = [replace(vm, host_ref="host-203")]
        if conflict == "ownership":
            env.nb.records[3219]["custom_fields"]["source"] = "foreign"
        elif conflict == "duplicate":
            env.nb.records[4000] = record(4000, name="new")
        elif conflict == "missing_device":
            del env.nb.devices[103]
        return env.vw.vms[0]

    monkeypatch.setattr(env.vw, "refresh", refresh)
    before = env.cfg.database.read_bytes()
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches
    assert env.cfg.database.read_bytes() == before


def test_dry_run_still_reports_final_inventory_drift_without_writes(env, monkeypatch, caplog):
    env.bind()
    engine = env.engine()
    before = env.cfg.database.read_bytes()

    def inventory():
        env.vw.vms = [replace(env.vm, host_ref="host-203")]
        return env.vw.vms

    monkeypatch.setattr(env.vw, "inventory", inventory)
    assert engine.reconcile() == 2
    assert not env.nb.patches
    assert env.cfg.database.read_bytes() == before
    assert "retrying" not in caplog.text


def test_second_retry_can_succeed_with_the_latest_placement(env, monkeypatch, caplog):
    env.bind()
    env.migrate(name="new")
    calls = 0

    def refresh(vm):
        nonlocal calls
        calls += 1
        if calls == 1:
            env.vw.vms = [replace(vm, host_ref="host-203")]
        elif calls == 2:
            env.vw.vms = [replace(vm, host_ref="host-202", cluster_ref="domain-c102")]
        return env.vw.vms[0]

    monkeypatch.setattr(env.vw, "refresh", refresh)
    assert env.engine().reconcile(True) == 0
    assert calls == 3
    assert "attempt=3/3" in caplog.text
    assert env.nb.patches == [(3219, {"name": "new", "cluster": 20, "device": 102, "site": 2})]


def test_inventory_failure_on_retry_stops_without_committing_state(env, monkeypatch):
    from netbox_vm_reconcile.identity import SafetyError

    env.bind()
    env.migrate(name="new")
    before = env.cfg.database.read_bytes()
    engine = env.engine()
    env.vw.refresh_override = replace(env.vw.vms[0], host_ref="host-203")

    def unavailable():
        raise SafetyError("vCenter unavailable during retry")

    monkeypatch.setattr(env.vw, "inventory", unavailable)
    with pytest.raises(SafetyError, match="unavailable during retry"):
        engine.reconcile(True)
    assert not env.nb.patches
    assert env.cfg.database.read_bytes() == before


def test_final_motion_mixed_with_another_vm_identity_change_is_not_retried(
    env, monkeypatch, caplog
):
    env.bind()
    pending = replace(
        env.vm,
        instance_uuid="44444444-4444-4444-8444-444444444444",
        bios_uuid="55555555-5555-4555-8555-555555555555",
        moref="vm-2",
        name="new-vm",
    )
    env.vw.vms.append(pending)
    engine = env.engine()

    def inventory():
        env.vw.vms = [replace(env.vm, host_ref="host-203"), replace(pending, name="renamed")]
        return env.vw.vms

    monkeypatch.setattr(env.vw, "inventory", inventory)
    assert engine.reconcile(True) == 2
    assert not env.nb.patches
    assert "retrying" not in caplog.text
