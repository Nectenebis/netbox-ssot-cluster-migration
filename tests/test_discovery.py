from copy import deepcopy
from dataclasses import replace

import pytest

from netbox_vm_reconcile.identity import SafetyError
from netbox_vm_reconcile.placement import Placement


def managed(name, moref, source="mvcenter"):
    return {
        "name": name,
        "custom_fields": {"source": source, "source_id": moref},
        "tags": [{"name": "vmware"}, {"name": f"Source: {source}"}],
    }


@pytest.fixture
def auto(env):
    env.cfg = replace(
        env.cfg,
        vcenters=[replace(env.cfg.vcenters[0], expected_uuid=None, cluster_ids={}, host_ids={})],
    )
    env.vm = replace(env.vm, cluster_name="DevTest", host_name="mesx002")
    env.vw.vms = [env.vm]
    env.nb.clusters[10].update(managed("DevTest", "domain-c101"))
    env.nb.clusters[20].update(managed("prod62", "domain-c102"))
    env.nb.devices[101].update(managed("mesx002", "host-201"))
    env.nb.devices[102].update(managed("mesx001", "host-202"))
    env.nb.devices[103].update(managed("mesx003", "host-203"))
    return env


def test_automatic_bootstrap_and_cluster_migration(auto):
    assert auto.engine().bootstrap(True) == 0
    auto.migrate(
        cluster_ref="domain-c102", host_ref="host-202", cluster_name="prod62", host_name="mesx001"
    )
    assert auto.engine().reconcile(True) == 0
    assert auto.nb.patches == [(3219, {"cluster": 20, "device": 102, "site": 2})]
    assert auto.engine().reconcile(True) == 0
    assert len(auto.nb.patches) == 1


def test_source_id_takes_priority_over_display_name(auto):
    auto.bind()
    auto.nb.clusters[10]["name"] = "SSoT-transformed-cluster-name"
    auto.nb.devices[101]["name"] = "SSoT-transformed-host-name"
    assert auto.engine().reconcile(True) == 0


def test_fallback_unique_name_and_source(auto):
    auto.bind()
    del auto.nb.clusters[10]["custom_fields"]["source_id"]
    del auto.nb.devices[101]["custom_fields"]["source_id"]
    assert auto.engine().reconcile(True) == 0


@pytest.mark.parametrize("target", ["cluster", "host"])
def test_ambiguous_source_id_blocks_patch(auto, target):
    auto.bind()
    auto.migrate(name="new")
    records, pk = (auto.nb.clusters, 10) if target == "cluster" else (auto.nb.devices, 101)
    records[999] = {**deepcopy(records[pk]), "id": 999}
    assert auto.engine().reconcile(True) == 2
    assert not auto.nb.patches


def test_ambiguous_name_blocks_fallback(auto):
    auto.bind()
    del auto.nb.clusters[10]["custom_fields"]["source_id"]
    auto.nb.clusters[999] = {**deepcopy(auto.nb.clusters[10]), "id": 999}
    assert auto.engine().reconcile(True) == 2
    assert not auto.nb.patches


def test_name_cannot_override_conflicting_source_id(auto):
    auto.bind()
    auto.nb.clusters[10]["custom_fields"]["source_id"] = "domain-c999"
    assert auto.engine().reconcile(True) == 2
    assert not auto.nb.patches


@pytest.mark.parametrize("mutation", ["source", "type_tag", "source_tag", "foreign_tag"])
def test_auto_placement_requires_ownership(auto, mutation):
    auto.bind()
    r = auto.nb.devices[101]
    if mutation == "source":
        r["custom_fields"]["source"] = "ovirt"
    elif mutation == "type_tag":
        r["tags"] = [{"name": "Source: mvcenter"}]
    elif mutation == "source_tag":
        r["tags"] = [{"name": "vmware"}]
    else:
        r["tags"].append({"name": "ovirt"})
    assert auto.engine().reconcile(True) == 2
    assert not auto.nb.patches


def test_same_moref_and_name_in_another_source_are_not_candidates(auto):
    auto.bind()
    auto.nb.clusters[999] = {"id": 999, **managed("DevTest", "domain-c101", source="vc2")}
    assert auto.engine().reconcile(True) == 0


def test_manual_override_can_handle_unmanaged_placement(auto):
    auto.cfg = replace(
        auto.cfg,
        vcenters=[
            replace(
                auto.cfg.vcenters[0], cluster_ids={"domain-c101": 10}, host_ids={"host-201": 101}
            )
        ],
    )
    auto.nb.clusters[10].pop("custom_fields")
    auto.nb.devices[101].pop("custom_fields")
    auto.bind()
    assert auto.engine().reconcile(True) == 0


def test_changed_target_ownership_prevents_patch(auto, monkeypatch):
    auto.bind()
    auto.migrate(name="new")
    placement = auto.nb.placement

    def changed(cid, did):
        cluster, device = placement(cid, did)
        device["custom_fields"]["source"] = "ovirt"
        return cluster, device

    monkeypatch.setattr(auto.nb, "placement", changed)
    assert auto.engine().reconcile(True) == 2
    assert not auto.nb.patches


def test_missing_auto_target_has_actionable_error(auto):
    resolver = Placement({}, {})
    with pytest.raises(SafetyError, match="Missing/ambiguous Cluster"):
        resolver.resolve("Cluster", auto.cfg.vcenters[0], "domain-c101", "DevTest")


def test_cli_bootstrap_and_apply_without_manual_ids_or_uuid(auto, monkeypatch):
    from contextlib import nullcontext

    from netbox_vm_reconcile import cli

    auto.store.db.execute("DELETE FROM vcenters")
    auto.store.db.commit()
    monkeypatch.setattr(cli, "load", lambda *a, **k: auto.cfg)
    monkeypatch.setattr(cli, "VMware", lambda configs: nullcontext(auto.vw))
    monkeypatch.setattr(cli, "NetBox", lambda config: auto.nb)
    monkeypatch.setattr(auto.nb, "close", lambda: None, raising=False)
    assert cli.main(["bootstrap", "--dry-run"]) == 0
    assert auto.store.vcenters() == {}
    assert cli.main(["reconcile", "--apply"]) == 2
    assert cli.main(["bootstrap", "--apply"]) == 0
    assert auto.store.vcenters() == {auto.vm.source: auto.vm.vcenter_uuid}
    auto.migrate(
        cluster_ref="domain-c102", host_ref="host-202", cluster_name="prod62", host_name="mesx001"
    )
    assert cli.main(["reconcile", "--dry-run"]) == 0
    assert not auto.nb.patches
    assert cli.main(["reconcile", "--apply"]) == 0
    assert auto.nb.patches == [(3219, {"cluster": 20, "device": 102, "site": 2})]
