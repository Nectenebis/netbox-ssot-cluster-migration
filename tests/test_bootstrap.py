from dataclasses import replace

import pytest

from netbox_vm_reconcile.identity import SafetyError

from .conftest import record


def test_bootstrap_dry_run_and_repeatability(env):
    before = env.cfg.database.read_bytes()
    assert env.engine().bootstrap() == 0
    assert env.cfg.database.read_bytes() == before
    assert not env.store.rows()
    assert env.engine().bootstrap(True) == 0
    assert env.engine().bootstrap(True) == 0
    assert len(env.store.rows()) == 1
    assert not env.nb.patches


def test_bootstrap_not_by_name_alone(env):
    env.nb.records[3219]["cluster"] = {"id": 20}
    assert env.engine().bootstrap(True) == 2
    assert not env.store.rows()


def test_same_names_across_clusters_require_review(env):
    env.nb.records[3567] = record(3567, cid=20)
    assert env.engine().bootstrap(True) == 2
    assert not env.store.rows()


def test_duplicate_uuid_blocks_entire_inventory(env):
    env.vw.vms.append(replace(env.vm, name="clone", moref="vm-2"))
    with pytest.raises(SafetyError, match="Duplicate VMware UUID"):
        env.engine()
    assert not env.store.rows()


def test_duplicate_bios_uuid_blocks_cross_vcenter(env):
    env.vw.vms.append(replace(env.vm, vcenter_uuid="other", instance_uuid="other", source="vc2"))
    with pytest.raises(SafetyError, match="Duplicate VMware UUID"):
        env.engine()


def test_changed_identity_cannot_bootstrap_to_bound_id(env):
    env.bind()
    env.migrate(instance_uuid="44444444-4444-4444-8444-444444444444")
    assert env.engine().bootstrap(True) == 2
    assert len(env.store.rows()) == 1


def test_manual_bind_old_cluster_then_move(env):
    env.migrate(cluster_ref="domain-c102", host_ref="host-202")
    env.engine().bind("mvcenter", env.vm.instance_uuid, 3219, True)
    assert env.engine().reconcile(True) == 0
    assert env.nb.patches[0][0] == 3219


def test_manual_bind_cannot_override_ownership_or_rebind(env):
    env.bind()
    with pytest.raises(SafetyError, match="already bound"):
        env.engine().bind("mvcenter", env.vm.instance_uuid, 3219, True)
    env.nb.records[3219]["custom_fields"]["source"] = "ovirt"
    with pytest.raises(SafetyError, match="ownership"):
        env.engine().bind("mvcenter", env.vm.instance_uuid, 3219, True)


def test_multiple_vcenters(env):
    vc2 = replace(
        env.cfg.vcenters[0],
        name="vc2",
        expected_uuid="44444444-4444-4444-8444-444444444444",
        source_tag="Source: vc2",
    )
    env.cfg = replace(env.cfg, vcenters=[*env.cfg.vcenters, vc2])
    vm2 = replace(
        env.vm,
        source="vc2",
        vcenter_uuid=vc2.expected_uuid,
        instance_uuid="55555555-5555-4555-8555-555555555555",
        bios_uuid="66666666-6666-4666-8666-666666666666",
        name="vc2-vm",
    )
    env.vw.vms.append(vm2)
    env.nb.records[4000] = record(4000, "vc2-vm", source="vc2")
    assert env.engine().bootstrap(True) == 0
    assert len(env.store.rows()) == 2
