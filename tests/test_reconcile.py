from copy import deepcopy
from dataclasses import replace

import pytest

from netbox_vm_reconcile.identity import SafetyError

from .conftest import record


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"cluster_ref": "domain-c102", "host_ref": "host-202"},
            {"cluster": 20, "device": 102, "site": 2},
        ),
        ({"host_ref": "host-203"}, {"device": 103}),
        ({"name": "app-new"}, {"name": "app-new"}),
    ],
)
def test_migrations_and_idempotence(env, fields, expected):
    env.bind()
    original = deepcopy(env.nb.records[3219])
    env.migrate(**fields)
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches == [(3219, expected)]
    assert env.engine().reconcile(True) == 0
    assert len(env.nb.patches) == 1
    for key in set(original) - {"name", "cluster", "device", "site"}:
        assert env.nb.records[3219][key] == original[key]


def test_cluster_only_same_site(env):
    env.bind()
    env.migrate(cluster_ref="domain-c102", host_ref="host-201")
    env.nb.devices[101]["cluster"] = {"id": 20}
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches == [(3219, {"cluster": 20})]


def test_dry_run_no_writes(env):
    env.bind()
    before = env.cfg.database.read_bytes()
    env.migrate(cluster_ref="domain-c102", host_ref="host-202")
    assert env.engine().reconcile() == 0
    assert not env.nb.patches
    assert env.cfg.database.read_bytes() == before


def test_duplicate_detected(env, caplog):
    env.bind()
    env.migrate(cluster_ref="domain-c102", host_ref="host-202")
    env.nb.records[3567] = record(3567, cid=20, did=102, sid=2)
    assert env.engine().reconcile(True) == 2
    assert "DUPLICATE DETECTED" in caplog.text
    assert "3219" in caplog.text and "3567" in caplog.text
    assert not env.nb.patches
    del env.nb.records[3567]
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches[0][0] == 3219


@pytest.mark.parametrize("kind", ["ovirt", "opennebula", "standalone", "manual"])
def test_foreign_sources_untouched(env, kind):
    env.bind()
    env.migrate(name="renamed")
    env.nb.records[3219]["custom_fields"]["source"] = kind
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches


@pytest.mark.parametrize("field", ["bios_uuid", "instance_uuid"])
def test_changed_uuid_requires_manual_review(env, field):
    env.bind()
    env.migrate(**{field: "44444444-4444-4444-8444-444444444444", "name": "renamed"})
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches


def test_missing_mapped_vm(env):
    env.bind()
    env.nb.records.clear()
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches


@pytest.mark.parametrize("mutation", ["host", "cluster", "membership", "site", "scope"])
def test_invalid_placement(env, mutation):
    env.bind()
    env.migrate(name="new")
    if mutation == "host":
        env.nb.devices.clear()
    elif mutation == "cluster":
        env.nb.clusters.clear()
    elif mutation == "membership":
        env.nb.devices[101]["cluster"] = {"id": 20}
    elif mutation == "site":
        env.nb.devices[101]["site"] = None
    else:
        env.nb.clusters[10].update(scope_type="dcim.site", scope_id=2)
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches


def test_vmware_moved_during_run(env):
    env.bind()
    env.migrate(name="new")
    env.vw.refresh_override = env.vm
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches


def test_netbox_changed_during_run(env, monkeypatch):
    env.bind()
    env.migrate(name="new")
    read = env.nb.get_vm
    count = 0

    def concurrent(pk):
        nonlocal count
        count += 1
        result = read(pk)
        if count > 1:
            result["last_updated"] = "changed"
        return result

    monkeypatch.setattr(env.nb, "get_vm", concurrent)
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches


def test_partial_success_retains_successful_state(env):
    env.bind()
    second = replace(
        env.vm,
        instance_uuid="44444444-4444-4444-8444-444444444444",
        bios_uuid="55555555-5555-4555-8555-555555555555",
        name="other",
        moref="vm-2",
    )
    env.store.bind(second, 4000, 10)
    env.nb.records[4000] = record(4000, "other")
    env.nb.fail_ids.add(4000)
    env.vw.vms = [replace(env.vm, name="new"), replace(second, name="other-new")]
    assert env.engine().reconcile(True) == 2
    assert env.nb.patches == [(3219, {"name": "new"})]
    rows = {r["netbox_vm_id"]: r for r in env.store.rows()}
    assert rows[3219]["last_known_name"] == "new"
    assert rows[4000]["last_known_name"] == "other"


def test_vcenter_failure_prevents_writes(env, monkeypatch):
    env.bind()

    def fail():
        raise SafetyError("vCenter unavailable")

    monkeypatch.setattr(env.vw, "inventory", fail)
    with pytest.raises(SafetyError):
        env.engine()
    assert not env.nb.patches


def test_netbox_failure_prevents_writes(env, monkeypatch):
    def fail():
        raise SafetyError("NetBox unavailable")

    monkeypatch.setattr(env.nb, "snapshot", fail)
    with pytest.raises(SafetyError):
        env.engine()
    assert not env.nb.patches


def test_absent_vm_mapping_is_retained(env):
    env.bind()
    env.vw.vms = []
    assert env.engine().reconcile(True) == 0
    assert len(env.store.rows()) == 1


def test_check_reports_drift_without_patch(env):
    env.bind()
    env.migrate(name="new")
    assert env.engine().reconcile(check=True) == 2
    assert not env.nb.patches


def test_disabled_change_blocks_incomplete_patch(env):
    env.bind()
    env.migrate(cluster_ref="domain-c102", host_ref="host-202")
    env.cfg = replace(env.cfg, allow_site_change=False)
    assert env.engine().reconcile(True) == 2
    assert not env.nb.patches


def test_final_inventory_change_blocks_ssot(env, monkeypatch):
    env.bind()
    engine = env.engine()
    monkeypatch.setattr(env.vw, "inventory", lambda: [replace(env.vm, name="new")])
    assert engine.reconcile(True) == 2
    assert not env.nb.patches


def test_failure_before_patch_does_not_update_mapping(env):
    env.bind()
    env.migrate(name="new")
    env.nb.fail_ids.add(3219)
    before = env.cfg.database.read_bytes()
    assert env.engine().reconcile(True) == 2
    assert env.cfg.database.read_bytes() == before


def test_unmapped_conflicts_report_candidate_ids(env, caplog):
    env.nb.records[3567] = record(3567, cid=20)
    assert env.engine().reconcile() == 2
    assert "DUPLICATE DETECTED" in caplog.text
    assert "3219" in caplog.text and "3567" in caplog.text
