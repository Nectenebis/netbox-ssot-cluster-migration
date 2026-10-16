from copy import deepcopy
from dataclasses import replace

import pytest

from netbox_vm_reconcile.config import Config, VCenter
from netbox_vm_reconcile.identity import VM
from netbox_vm_reconcile.mappings import Store
from netbox_vm_reconcile.reconcile import Engine

VC = "11111111-1111-4111-8111-111111111111"
INSTANCE = "22222222-2222-4222-8222-222222222222"
BIOS = "33333333-3333-4333-8333-333333333333"


def record(pk=3219, name="mnetbox", cid=10, did=101, sid=1, source="mvcenter"):
    return {
        "id": pk,
        "name": name,
        "cluster": {"id": cid},
        "device": {"id": did},
        "site": {"id": sid},
        "tenant": None,
        "tags": [{"name": "vmware"}, {"name": f"Source: {source}"}],
        "custom_fields": {"source": source},
        "last_updated": "2026-01-01T00:00:00Z",
        "vcpus": 4,
        "memory": 8192,
        "primary_ip4": {"id": 55},
        "interface_count": 1,
    }


class FakeVMware:
    def __init__(self, vms):
        self.vms = vms
        self.refresh_override = None

    def inventory(self):
        return self.vms

    def refresh(self, vm):
        return self.refresh_override or vm


class FakeNetBox:
    def __init__(self, records):
        self.records = {r["id"]: r for r in records}
        self.clusters = {10: {"id": 10, "scope_type": None}, 20: {"id": 20, "scope_type": None}}
        self.devices = {
            101: {"id": 101, "cluster": {"id": 10}, "site": {"id": 1}},
            102: {"id": 102, "cluster": {"id": 20}, "site": {"id": 2}},
            103: {"id": 103, "cluster": {"id": 10}, "site": {"id": 1}},
        }
        self.patches = []
        self.fail_ids = set()

    def snapshot(self):
        return deepcopy((list(self.records.values()), self.clusters, self.devices))

    def get_vm(self, pk):
        from netbox_vm_reconcile.identity import SafetyError

        if pk not in self.records:
            raise SafetyError("Mapped ID missing")
        return deepcopy(self.records[pk])

    def named(self, name):
        return [
            deepcopy(r) for r in self.records.values() if r["name"].casefold() == name.casefold()
        ]

    def placement(self, cid, did):
        return deepcopy((self.clusters[cid], self.devices[did]))

    def patch_vm(self, pk, changes):
        from netbox_vm_reconcile.identity import SafetyError

        if pk in self.fail_ids:
            raise SafetyError("NetBox HTTP 400")
        assert set(changes) <= {"name", "cluster", "device", "site"}
        self.patches.append((pk, deepcopy(changes)))
        for key, value in changes.items():
            self.records[pk][key] = value if key == "name" else {"id": value}


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "state.db"
    Store.initialize(db)
    store = Store(db, True)
    vm = VM("mvcenter", VC, INSTANCE, BIOS, "vm-1", "mnetbox", "domain-c101", "host-201")
    vc = VCenter(
        "mvcenter",
        "vc.example.test",
        "user",
        "password",
        VC,
        {"domain-c101": 10, "domain-c102": 20},
        {"host-201": 101, "host-202": 102, "host-203": 103},
        source_tag="Source: mvcenter",
    )
    cfg = Config("https://netbox.example.test", "secret", db, [vc])
    nb = FakeNetBox([record()])
    vw = FakeVMware([vm])

    class Env:
        def engine(self):
            return Engine(self.cfg, self.store, self.vw, self.nb)

        def bind(self):
            self.store.bind(self.vm, 3219, 10)

        def migrate(self, **changes):
            self.vw.vms = [replace(self.vm, **changes)]

    e = Env()
    e.cfg, e.store, e.vm, e.nb, e.vw = cfg, store, vm, nb, vw
    yield e
    store.close()
