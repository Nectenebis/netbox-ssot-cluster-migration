import json
from dataclasses import replace

import pytest

from netbox_vm_reconcile.identity import inventory_diff


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "renamed"),
        ("host_ref", "host-202"),
        ("host_name", "mesx001"),
        ("cluster_ref", "domain-c102"),
        ("cluster_name", "prod62"),
        ("moref", "vm-2"),
        ("bios_uuid", "44444444-4444-4444-8444-444444444444"),
    ],
)
def test_inventory_diff_reports_exact_changed_fields(env, field, value):
    changed = replace(env.vm, **{field: value})
    events = inventory_diff([env.vm], [changed])
    assert events == [
        {
            "kind": "modified",
            "source": env.vm.source,
            "name": changed.name,
            "vcenter_uuid": env.vm.vcenter_uuid,
            "instance_uuid": env.vm.instance_uuid,
            "fields": {field: {"old": getattr(env.vm, field), "new": value}},
        }
    ]


def test_uuid_change_is_not_inferred_to_be_the_same_vm(env):
    new = replace(env.vm, instance_uuid="44444444-4444-4444-8444-444444444444")
    events = inventory_diff([env.vm], [new])
    assert {event["kind"] for event in events} == {"added", "removed"}
    assert len(events) == 2
    assert {event["vm"]["instance_uuid"] for event in events} == {
        env.vm.instance_uuid,
        new.instance_uuid,
    }


def test_inventory_diff_is_independent_of_collection_order(env):
    other = replace(env.vm, instance_uuid="44444444-4444-4444-8444-444444444444")
    assert inventory_diff([env.vm, other], [other, env.vm]) == []


@pytest.mark.parametrize("kind", ["added", "removed"])
def test_added_and_removed_vm_details(env, kind):
    before, after = ([], [env.vm]) if kind == "added" else ([env.vm], [])
    (event,) = inventory_diff(before, after)
    assert event["kind"] == kind
    assert event["vm"]["instance_uuid"] == env.vm.instance_uuid
    assert event["vm"]["name"] == env.vm.name
    assert event["vm"]["host_ref"] == env.vm.host_ref


def test_final_inventory_error_names_the_vm_and_changed_host(env, monkeypatch, caplog):
    env.bind()
    engine = env.engine()
    monkeypatch.setattr(
        env.vw,
        "inventory",
        lambda: [
            replace(
                env.vm,
                host_ref="host-202",
                host_name="mesx001",
            )
        ],
    )
    before = env.cfg.database.read_bytes()
    assert engine.reconcile() == 2
    details = [
        json.loads(r.message.split(": ", 1)[1])
        for r in caplog.records
        if r.message.startswith("VMware inventory difference: ")
    ]
    (event,) = details
    assert event["source"] == env.vm.source and event["name"] == env.vm.name
    assert event["instance_uuid"] == env.vm.instance_uuid
    assert event["fields"]["host_ref"] == {"old": "host-201", "new": "host-202"}
    assert "SSoT must remain stopped" in caplog.text
    assert not env.nb.patches
    assert env.cfg.database.read_bytes() == before


def test_persistently_inconsistent_pre_patch_inventory_is_logged_and_not_applied(env, caplog):
    env.bind()
    env.migrate(name="new-name")
    env.vw.refresh_override = replace(env.vw.vms[0], host_ref="host-202")
    assert env.engine().reconcile(True) == 2
    assert '"host_ref": {"old": "host-201", "new": "host-202"}' in caplog.text
    assert "attempt=2/3" in caplog.text and "attempt=3/3" in caplog.text
    assert "placement kept changing after 3 attempts" in caplog.text
    assert not env.nb.patches
