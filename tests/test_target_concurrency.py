import logging
from dataclasses import replace

import pytest

from netbox_vm_reconcile.identity import object_id

from .conftest import record


def test_own_migration_counter_updates_do_not_block_next_vm(env, monkeypatch):
    env.bind()
    second = replace(
        env.vm,
        name="drs-vm",
        instance_uuid="44444444-4444-4444-8444-444444444444",
        bios_uuid="55555555-5555-4555-8555-555555555555",
        moref="vm-2",
        cluster_ref="domain-c102",
        host_ref="host-202",
    )
    env.store.bind(second, 4000, 20)
    env.nb.records[4000] = record(4000, "drs-vm", cid=20, did=104, sid=2)
    env.nb.devices[104] = {"id": 104, "cluster": {"id": 20}, "site": {"id": 2}}
    env.nb.clusters[10]["virtual_machine_count"] = 1
    env.nb.clusters[20]["virtual_machine_count"] = 1
    env.nb.devices[102]["virtual_machine_count"] = 0
    env.vw.vms = [replace(env.vm, cluster_ref="domain-c102", host_ref="host-202"), second]
    patch = env.nb.patch_vm

    def update_counts(pk, changes):
        old_cluster = object_id(env.nb.records[pk]["cluster"])
        old_device = object_id(env.nb.records[pk]["device"])
        patch(pk, changes)
        if "cluster" in changes and changes["cluster"] != old_cluster:
            env.nb.clusters[old_cluster]["virtual_machine_count"] -= 1
            env.nb.clusters[changes["cluster"]]["virtual_machine_count"] += 1
        if "device" in changes and changes["device"] != old_device:
            env.nb.devices[changes["device"]]["virtual_machine_count"] += 1

    monkeypatch.setattr(env.nb, "patch_vm", update_counts)
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches == [
        (3219, {"cluster": 20, "device": 102, "site": 2}),
        (4000, {"device": 102}),
    ]
    assert env.nb.clusters[20]["virtual_machine_count"] == 2
    assert env.nb.devices[102]["virtual_machine_count"] == 2


@pytest.mark.parametrize("kind", ["Cluster", "Device"])
@pytest.mark.parametrize("field", ["virtual_machine_count", "last_updated", "comments"])
def test_unrelated_target_changes_do_not_block_patch(env, monkeypatch, kind, field):
    env.bind()
    env.migrate(name="new")
    read = env.nb.placement

    def changed(cid, did):
        cluster, device = read(cid, did)
        target = cluster if kind == "Cluster" else device
        target[field] = 999 if field == "virtual_machine_count" else "updated"
        return cluster, device

    monkeypatch.setattr(env.nb, "placement", changed)
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches == [(3219, {"name": "new"})]


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("Cluster", "id", 999),
        ("Cluster", "name", "changed"),
        ("Cluster", "scope_type", "dcim.site"),
        ("Cluster", "scope_id", 2),
        ("Device", "id", 999),
        ("Device", "name", "changed"),
        ("Device", "cluster", {"id": 20}),
        ("Device", "site", {"id": 2}),
        ("Cluster", "source", "foreign"),
        ("Device", "source_id", "host-other"),
        ("Device", "tags", []),
    ],
)
def test_relevant_target_changes_still_block_and_explain_fields(
    env, monkeypatch, caplog, kind, field, value
):
    env.bind()
    env.migrate(name="new")
    for target in (env.nb.clusters[10], env.nb.devices[101]):
        target.update(
            name="target",
            custom_fields={"source": "mvcenter", "source_id": "reference"},
            tags=[{"name": "vmware"}, {"name": "Source: mvcenter"}],
        )
    read = env.nb.placement

    def changed(cid, did):
        cluster, device = read(cid, did)
        target = cluster if kind == "Cluster" else device
        if field in {"source", "source_id"}:
            target["custom_fields"][field] = value
        else:
            target[field] = value
        return cluster, device

    monkeypatch.setattr(env.nb, "placement", changed)
    assert env.engine().reconcile(True) == 2
    assert f"Target {kind} changed" in caplog.text
    assert f'"{field if field != "tags" else "owned"}"' in caplog.text
    assert '"old"' in caplog.text and '"new"' in caplog.text
    assert not env.nb.patches


def test_relation_display_labels_and_unrelated_tags_are_not_conflicts(env, monkeypatch):
    env.bind()
    env.migrate(name="new")
    read = env.nb.placement

    def changed(cid, did):
        cluster, device = read(cid, did)
        device["cluster"].update(name="cluster label", url="https://example.test/cluster/")
        device["site"].update(name="site label", display="site label")
        device["tags"] = [{"name": "operator-note"}]
        cluster["custom_fields"] = {"unmanaged": {"value": "do not touch"}}
        return cluster, device

    monkeypatch.setattr(env.nb, "placement", changed)
    assert env.engine().reconcile(True) == 0


@pytest.mark.parametrize("apply", [False, True])
def test_patch_plan_reports_old_and_new_values(env, caplog, apply):
    caplog.set_level(logging.INFO)
    env.bind()
    env.migrate(cluster_ref="domain-c102", host_ref="host-202")
    assert env.engine().reconcile(apply) == 0
    plan = next(r.message for r in caplog.records if "CHANGES=" in r.message)
    assert "'cluster': {'old': 10, 'new': 20}" in plan
    assert "'device': {'old': 101, 'new': 102}" in plan
    assert "'site': {'old': 1, 'new': 2}" in plan
    assert ("APPLY PLAN" if apply else "DRY-RUN") in plan
    assert any(r.message.startswith("APPLIED ") for r in caplog.records) is apply


def test_failed_patch_does_not_log_applied(env, caplog):
    caplog.set_level(logging.INFO)
    env.bind()
    env.migrate(name="new")
    env.nb.fail_ids.add(3219)
    assert env.engine().reconcile(True) == 2
    assert "APPLY PLAN" in caplog.text
    assert not any(r.message.startswith("APPLIED ") for r in caplog.records)
