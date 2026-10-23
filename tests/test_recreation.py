import logging
from copy import deepcopy
from dataclasses import replace

import pytest

from netbox_vm_reconcile.identity import SafetyError

from .conftest import record


def recreate(env, **changes):
    vm = replace(
        env.vm,
        instance_uuid="44444444-4444-4444-8444-444444444444",
        bios_uuid="55555555-5555-4555-8555-555555555555",
        moref="vm-new",
        **changes,
    )
    env.vw.vms = [vm]
    return vm


@pytest.mark.parametrize("command", ["bootstrap", "reconcile"])
@pytest.mark.parametrize("apply", [False, True])
def test_recreated_vm_defers_to_ssot_and_only_removes_old_mapping(env, caplog, command, apply):
    env.bind()
    recreate(env)
    original = deepcopy(env.nb.records)
    before = env.cfg.database.read_bytes()
    statements = []
    env.store.db.set_trace_callback(statements.append)
    caplog.set_level(logging.INFO)
    assert getattr(env.engine(), command)(apply) == 0
    assert len(env.store.rows()) == (0 if apply else 1)
    assert env.nb.records == original
    assert not env.nb.patches
    assert "RECREATED VM" in caplog.text and "pending SSoT=1; errors=0" in caplog.text
    assert sum(s == "COMMIT" for s in statements) == int(apply)
    if not apply:
        assert env.cfg.database.read_bytes() == before


@pytest.mark.parametrize("ssot_action", ["reuse", "recreate"])
def test_next_run_enrolls_whichever_netbox_id_ssot_kept(env, ssot_action):
    env.bind()
    vm = recreate(env)
    old = deepcopy(env.nb.records[3219])
    assert env.engine().reconcile(True) == 0
    assert not env.store.rows()
    # Simulate the main SSoT either updating the same object or replacing it.
    pk = 3219 if ssot_action == "reuse" else 4000
    env.nb.records = {pk: {**old, "id": pk, "vcpus": 8}}
    assert env.engine().reconcile(True) == 0
    (row,) = env.store.rows()
    assert row["vm_instance_uuid"] == vm.instance_uuid
    assert row["vm_bios_uuid"] == vm.bios_uuid
    assert row["netbox_vm_id"] == pk
    assert env.engine().reconcile(True) == 0
    assert not env.nb.patches
    if ssot_action == "reuse":
        assert env.nb.records[3219]["primary_ip4"] == old["primary_ip4"]
        assert env.nb.records[3219]["interface_count"] == old["interface_count"]


@pytest.mark.parametrize("missing_placement", [False, True])
def test_recreated_vm_in_different_cluster_can_reach_ssot(env, missing_placement):
    env.bind()
    recreate(env, cluster_ref="domain-c102", host_ref="host-202")
    if missing_placement:
        env.nb.clusters.clear()
        env.nb.devices.clear()
    assert env.engine().reconcile(True) == 0
    assert not env.store.rows()
    assert env.nb.records[3219]["cluster"] == {"id": 10}
    assert not env.nb.patches
    # Main SSoT settles the current Cluster/Device/Site before enrollment.
    if not missing_placement:
        env.nb.records[3219].update(cluster={"id": 20}, device={"id": 102}, site={"id": 2})
        assert env.engine().reconcile(True) == 0
        assert env.store.rows()[0]["netbox_vm_id"] == 3219


def test_stale_netbox_id_is_deferred_even_if_saved_mapping_name_differs(env):
    env.store.bind(replace(env.vm, name="last-known-older-name"), 3219, 10)
    recreate(env, cluster_ref="domain-c102", host_ref="host-202")
    assert env.engine().reconcile(True) == 0
    assert not env.store.rows()
    assert not env.nb.patches


@pytest.mark.parametrize("presence", ["same_name", "renamed", "excluded_template"])
def test_previous_uuid_still_present_is_never_replaced(env, presence):
    env.bind()
    recreate(env)
    if presence == "excluded_template":
        env.vw.excluded_keys.add(env.vm.key)
    else:
        old = replace(env.vm, name="old-renamed") if presence == "renamed" else env.vm
        env.vw.vms.append(old)
    assert env.engine().reconcile(True) == 2
    assert env.store.rows()[0]["vm_instance_uuid"] == env.vm.instance_uuid
    assert env.nb.patches == ([(3219, {"name": "old-renamed"})] if presence == "renamed" else [])


@pytest.mark.parametrize("conflict", ["duplicate", "ownership", "foreign_collision"])
def test_replacement_does_not_bypass_netbox_conflicts(env, conflict):
    env.bind()
    recreate(env)
    if conflict == "duplicate":
        env.nb.records[4000] = record(4000)
    elif conflict == "ownership":
        env.nb.records[3219]["tags"] = []
    else:
        env.nb.records[4000] = record(4000, source="foreign")
    before = env.cfg.database.read_bytes()
    assert env.engine().reconcile(True) == 2
    assert env.cfg.database.read_bytes() == before
    assert not env.nb.patches


def test_old_uuid_reappearing_before_cleanup_blocks_ssot_and_retains_mapping(env, monkeypatch):
    env.bind()
    vm = recreate(env)
    engine = env.engine()

    def inventory():
        env.vw.vms = [vm, replace(env.vm, name="old-reappeared")]
        return env.vw.vms

    monkeypatch.setattr(env.vw, "inventory", inventory)
    assert engine.reconcile(True) == 2
    assert env.store.rows()[0]["vm_instance_uuid"] == env.vm.instance_uuid
    assert not env.nb.patches


def test_netbox_failure_when_deferring_replacement_retains_mapping(env, monkeypatch):
    env.bind()
    recreate(env)

    def fail(name):
        raise SafetyError("NetBox unavailable")

    monkeypatch.setattr(env.nb, "named", fail)
    assert env.engine().reconcile(True) == 2
    assert env.store.rows()[0]["vm_instance_uuid"] == env.vm.instance_uuid


def test_recreated_vm_does_not_prevent_repair_of_other_known_vm(env):
    env.bind()
    new = recreate(env)
    second = replace(
        env.vm,
        instance_uuid="66666666-6666-4666-8666-666666666666",
        bios_uuid="77777777-7777-4777-8777-777777777777",
        moref="vm-other",
        name="other-vm",
    )
    env.store.bind(second, 4000, 10)
    env.nb.records[4000] = record(4000, second.name)
    env.vw.vms = [new, replace(second, host_ref="host-203")]
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches == [(4000, {"device": 103})]
    assert [r["netbox_vm_id"] for r in env.store.rows()] == [4000]


def test_recreation_with_same_name_in_other_vcenter_preserves_other_mapping(env):
    env.bind()
    vc2 = replace(
        env.cfg.vcenters[0],
        name="vc2",
        expected_uuid="88888888-8888-4888-8888-888888888888",
        source_tag="Source: vc2",
    )
    env.cfg = replace(env.cfg, vcenters=[*env.cfg.vcenters, vc2])
    env.vw.vcenter_uuids[vc2.name] = vc2.expected_uuid
    env.store.register_vcenters({vc2.name: vc2.expected_uuid})
    other = replace(
        env.vm,
        source=vc2.name,
        vcenter_uuid=vc2.expected_uuid,
        instance_uuid="66666666-6666-4666-8666-666666666666",
        bios_uuid="77777777-7777-4777-8777-777777777777",
        cluster_ref="domain-c102",
        host_ref="host-202",
    )
    env.store.bind(other, 4000, 20)
    env.nb.records[4000] = record(4000, cid=20, did=102, sid=2, source=vc2.name)
    new = recreate(env)
    env.vw.vms = [new, other]
    assert env.engine().reconcile(True) == 0
    assert [r["netbox_vm_id"] for r in env.store.rows()] == [4000]
    assert env.engine().reconcile(True) == 0
    assert {r["netbox_vm_id"] for r in env.store.rows()} == {3219, 4000}
    assert not env.nb.patches


def test_cli_recreation_cycle_allows_ssot_then_registers_same_id(env, monkeypatch):
    from contextlib import nullcontext

    from netbox_vm_reconcile import cli

    env.bind()
    vm = recreate(env)
    monkeypatch.setattr(cli, "load", lambda *a, **k: env.cfg)
    monkeypatch.setattr(cli, "VMware", lambda configs: nullcontext(env.vw))
    monkeypatch.setattr(cli, "NetBox", lambda config: env.nb)
    monkeypatch.setattr(env.nb, "close", lambda: None, raising=False)
    assert cli.main(["reconcile", "--apply"]) == 0
    assert not env.store.rows()
    assert env.nb.records[3219]["name"] == vm.name
    env.nb.records[3219]["vcpus"] = 8  # Successful main SSoT update of the preserved record.
    assert cli.main(["reconcile", "--apply"]) == 0
    assert env.store.rows()[0]["vm_instance_uuid"] == vm.instance_uuid
    assert env.store.rows()[0]["netbox_vm_id"] == 3219
    assert not env.nb.patches
