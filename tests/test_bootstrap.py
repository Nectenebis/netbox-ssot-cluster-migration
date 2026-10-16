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


@pytest.fixture
def same_names(env):
    vc1 = replace(env.cfg.vcenters[0], cluster_ids={"domain-c101": 33})
    vc2 = replace(
        vc1,
        name="vc2",
        expected_uuid="44444444-4444-4444-8444-444444444444",
        source_tag="Source: vc2",
        cluster_ids={"domain-c101": 27},
        host_ids={"host-201": 102},
    )
    env.cfg = replace(env.cfg, vcenters=[vc1, vc2])
    vm1 = replace(env.vm, name="GOST_cert")
    vm2 = replace(
        vm1,
        source="vc2",
        vcenter_uuid=vc2.expected_uuid,
        instance_uuid="55555555-5555-4555-8555-555555555555",
        bios_uuid="66666666-6666-4666-8666-666666666666",
    )
    env.vw.vms = [vm1, vm2]
    env.nb.records = {
        2962: record(2962, "GOST_cert", cid=33),
        2760: record(2760, "GOST_cert", cid=27, did=102, sid=2, source="vc2"),
    }
    env.nb.clusters = {33: {"id": 33, "scope_type": None}, 27: {"id": 27, "scope_type": None}}
    env.nb.devices[101]["cluster"] = {"id": 33}
    env.nb.devices[102]["cluster"] = {"id": 27}
    return env


@pytest.mark.parametrize("custom_tags", [False, True])
def test_same_names_in_two_vcenters_register_independently(same_names, custom_tags):
    env = same_names
    if custom_tags:
        tags = {"mvcenter": "Production VMware", "vc2": "Test VMware"}
        env.cfg = replace(
            env.cfg, vcenters=[replace(vc, source_tag=tags[vc.name]) for vc in env.cfg.vcenters]
        )
        for r in env.nb.records.values():
            r["tags"] = [{"name": "vmware"}, {"name": tags[r["custom_fields"]["source"]]}]
    before = env.cfg.database.read_bytes()
    assert env.engine().bootstrap() == 0
    assert env.cfg.database.read_bytes() == before
    assert not env.store.rows()
    assert env.engine().bootstrap(True) == 0
    rows = {r["source_name"]: r for r in env.store.rows()}
    assert rows["mvcenter"]["netbox_vm_id"] == 2962
    assert rows["vc2"]["netbox_vm_id"] == 2760
    assert env.engine().bootstrap(True) == 0
    assert env.engine().reconcile(True) == 0
    assert not env.nb.patches


def test_other_source_is_not_a_false_duplicate_in_conflicts(same_names, caplog):
    env = same_names
    # Register the second vCenter, but leave both VM mappings absent.
    env.store.register_vcenters(env.vw.identities())
    assert env.engine().reconcile() == 2
    assert "Unregistered VMware UUID" in caplog.text
    assert "DUPLICATE DETECTED" not in caplog.text


def test_same_source_vmware_name_ambiguity_remains_blocked(env):
    env.vw.vms.append(
        replace(
            env.vm,
            instance_uuid="55555555-5555-4555-8555-555555555555",
            bios_uuid="66666666-6666-4666-8666-666666666666",
            moref="vm-2",
            cluster_ref="domain-c102",
            host_ref="host-202",
        )
    )
    assert env.engine().bootstrap(True) == 2
    assert not env.store.rows()


def test_duplicate_without_source_tag_is_not_hidden(env):
    env.nb.records[3567] = record(3567, cid=20)
    env.nb.records[3567]["tags"] = []
    assert env.engine().bootstrap(True) == 2
    assert not env.store.rows()


def test_same_source_duplicates_still_block_when_other_source_matches(same_names):
    env = same_names
    env.nb.records[999] = record(999, "GOST_cert", cid=99)
    assert env.engine().bootstrap(True) == 2
    rows = env.store.rows()
    assert len(rows) == 1
    assert rows[0]["source_name"] == "vc2"
    assert rows[0]["netbox_vm_id"] == 2760


def test_different_sources_cannot_share_ssot_name_and_cluster(same_names):
    env = same_names
    vc1, vc2 = env.cfg.vcenters
    env.cfg = replace(env.cfg, vcenters=[vc1, replace(vc2, cluster_ids={"domain-c101": 33})])
    env.nb.devices[102]["cluster"] = {"id": 33}
    env.nb.records[2760]["cluster"] = {"id": 33}
    env.nb.records[2760]["tenant"] = {"id": 2}
    assert env.engine().bootstrap(True) == 2
    assert not env.store.rows()


@pytest.mark.parametrize("change", ["same_source_duplicate", "foreign_collision", "lost_tag"])
def test_bootstrap_revalidates_source_scoped_candidates_before_bind(env, monkeypatch, change):
    original = env.nb.named

    def changed(name):
        candidates = original(name)
        if change == "same_source_duplicate":
            candidates.append(record(999, cid=20))
        elif change == "foreign_collision":
            candidates.append(record(999, source="vc2"))
        else:
            candidates[0]["tags"] = []
        return candidates

    monkeypatch.setattr(env.nb, "named", changed)
    assert env.engine().bootstrap(True) == 2
    assert not env.store.rows()


def test_source_tag_cannot_override_wrong_custom_field(env):
    env.nb.records[3219]["custom_fields"]["source"] = "vc2"
    assert env.engine().bootstrap(True) == 2
    assert not env.store.rows()
