from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from pyVmomi import vim

from netbox_vm_reconcile.identity import SafetyError
from netbox_vm_reconcile.vmware import VMware


def test_vmware_inventory_and_fresh_identity(env, monkeypatch):
    content = MagicMock()
    host = vim.HostSystem("host-201")
    cluster = vim.ClusterComputeResource("domain-c101")
    obj = vim.VirtualMachine("vm-1")
    props = {
        "name": "mnetbox",
        "config.instanceUuid": env.vm.instance_uuid,
        "config.uuid": env.vm.bios_uuid,
        "config.template": False,
        "runtime.host": host,
    }

    def row(obj, data):
        return NS(obj=obj, propSet=[NS(name=k, val=v) for k, v in data.items()], missingSet=[])

    def properties(content, kind, paths, objects=None):
        if kind == vim.HostSystem:
            return [row(host, {"name": "mesx002", "parent": cluster})]
        if kind == vim.ClusterComputeResource:
            return [row(cluster, {"name": "DevTest"})]
        return [row(obj, props)]

    monkeypatch.setattr("netbox_vm_reconcile.vmware.properties", properties)
    reader = VMware(env.cfg.vcenters)
    reader.sessions["mvcenter"] = (env.cfg.vcenters[0], content)
    expected = replace(env.vm, cluster_name="DevTest", host_name="mesx002")
    assert reader.inventory() == [expected]
    assert reader.refresh(expected) == expected
    props["name"] = "new"
    assert reader.refresh(env.vm).name == "new"
    props["config.instanceUuid"] = None
    with pytest.raises(SafetyError, match="UUID"):
        reader.inventory()


def test_vmware_template_excluded_and_standalone_rejected(env):
    reader = VMware(env.cfg.vcenters)
    row = NS(obj=vim.VirtualMachine("vm-1"), propSet=[NS(name="config.template", val=True)])
    assert reader._vm(env.cfg.vcenters[0], row, {}) is None
    row.propSet = [
        NS(name="config.template", val=False),
        NS(name="runtime.host", val=vim.HostSystem("host-201")),
    ]
    hosts = {"host-201": {"parent": vim.ComputeResource("domain-s1")}}
    with pytest.raises(SafetyError, match="Standalone"):
        reader._vm(env.cfg.vcenters[0], row, hosts)
