"""Plan and execute conservative updates, without owning any VM network resources."""

import json
import logging
from collections import Counter, defaultdict

from .config import Config
from .identity import VM, SafetyError, inventory_diff, object_id, uuid, validate_inventory
from .mappings import Store
from .placement import Placement, owned, placement_diff

LOG = logging.getLogger(__name__)


def log_inventory_diff(before: list[VM], after: list[VM]) -> bool:
    changes = inventory_diff(before, after)
    for change in changes:
        LOG.error("VMware inventory difference: %s", json.dumps(change, ensure_ascii=False))
    return bool(changes)


class Engine:
    def __init__(self, config: Config, store: Store, vmware, netbox):
        self.config, self.store, self.vmware, self.netbox = config, store, vmware, netbox
        self.sources = {vc.name: vc for vc in config.vcenters}
        self.vms = vmware.inventory()  # All vCenters must succeed before any write.
        validate_inventory(self.vms)
        self.observed = {name: uuid(value) for name, value in vmware.identities().items()}
        if set(self.observed) != set(self.sources) or len(set(self.observed.values())) != len(
            self.observed
        ):
            raise SafetyError("Incomplete or duplicate vCenter identities")
        self.registered = store.vcenters()
        for source, saved_uuid in self.registered.items():
            if self.observed.get(source) != saved_uuid:
                raise SafetyError(f"Registered vCenter UUID/source changed: source={source}")
        for source, vc in self.sources.items():
            if vc.expected_uuid and vc.expected_uuid != self.observed[source]:
                raise SafetyError(f"Configured vCenter UUID mismatch: source={source}")
        if any(self.observed.get(vm.source) != vm.vcenter_uuid for vm in self.vms):
            raise SafetyError("VM inventory does not match connected vCenter identities")
        self.records, self.clusters, self.devices = netbox.snapshot()
        self.placement = Placement(self.clusters, self.devices)
        self.by_id = {r["id"]: r for r in self.records}
        self.by_name: dict[str, list[dict]] = defaultdict(list)
        for r in self.records:
            self.by_name[r["name"].casefold()].append(r)
        self.vm_names = Counter((v.source, v.name.casefold()) for v in self.vms)
        self.mappings = {(m["vcenter_uuid"], m["vm_instance_uuid"]): m for m in store.rows()}
        self.vm_by_key = {v.key: v for v in self.vms}
        for key, row in self.mappings.items():
            if self.registered.get(row["source_name"]) != key[0]:
                raise SafetyError("Mapped vCenter UUID/source missing from configuration")

    def owned(self, record: dict, vm: VM) -> bool:
        return owned(record, self.sources[vm.source])

    def source_candidates(self, vm: VM, candidates: list[dict]) -> list[dict]:
        # Retain same-source objects with invalid/missing tags so they still cause
        # ambiguity instead of silently hiding a possible duplicate.
        return [r for r in candidates if (r.get("custom_fields") or {}).get("source") == vm.source]

    def register_sources(self, apply: bool, names=None) -> None:
        selected = self.observed if names is None else {n: self.observed[n] for n in names}
        for name, observed in selected.items():
            if name not in self.registered:
                LOG.info(
                    "%s register vCenter source=%s UUID=%s",
                    "APPLY" if apply else "DRY-RUN",
                    name,
                    observed,
                )
        if apply:
            self.store.register_vcenters(selected)
            self.registered.update(selected)

    def targets(self, vm: VM) -> tuple[int, int, int]:
        vc = self.sources[vm.source]
        cid = self.placement.resolve("Cluster", vc, vm.cluster_ref, vm.cluster_name)
        did = self.placement.resolve("Device", vc, vm.host_ref, vm.host_name)
        self.validate_placement(self.clusters[cid], self.devices[did], cid, did)
        return cid, did, object_id(self.devices[did]["site"])  # type: ignore[return-value]

    @staticmethod
    def validate_placement(cluster: dict, device: dict, cid: int, did: int) -> None:
        if (
            cluster.get("id") != cid
            or device.get("id") != did
            or object_id(device.get("cluster")) != cid
            or not object_id(device.get("site"))
        ):
            raise SafetyError("ESXi Device must belong to target Cluster and have a Site")
        scope = cluster.get("scope_type")
        if scope and scope != "dcim.site":
            raise SafetyError("Only unscoped and Site-scoped clusters are supported")
        if scope == "dcim.site" and cluster.get("scope_id") != object_id(device["site"]):
            raise SafetyError("Cluster Site scope conflicts with ESXi Device Site")

    def duplicates(self, vm: VM, record: dict, cid: int, candidates=None) -> None:
        if candidates is None:
            candidates = self.by_name[vm.name.casefold()]
            if record["name"].casefold() != vm.name.casefold():
                candidates = candidates + self.by_name[record["name"].casefold()]
        bad = [
            r
            for r in candidates
            if r["id"] != record["id"] and (self.owned(r, vm) or object_id(r.get("cluster")) == cid)
        ]
        if bad:
            ids = [(r["id"], object_id(r.get("cluster"))) for r in [record, *bad]]
            raise SafetyError(
                f"DUPLICATE DETECTED UUID={vm.instance_uuid} NetBox (ID, Cluster)={ids}; "
                "manual resolution required"
            )

    def identity(self, vm: VM, row: dict, record: dict) -> None:
        if (
            row["source_name"] != vm.source
            or row["vm_bios_uuid"] != vm.bios_uuid
            or record["id"] != row["netbox_vm_id"]
            or not self.owned(record, vm)
        ):
            raise SafetyError("Mapped identity/BIOS UUID/NetBox ownership changed; review required")

    def delta(self, vm: VM, record: dict, cid: int, did: int, sid: int) -> dict:
        desired = {"name": vm.name, "cluster": cid, "device": did, "site": sid}
        flags = {
            "name": self.config.allow_rename,
            "cluster": self.config.allow_cluster_change,
            "device": self.config.allow_host_change,
            "site": self.config.allow_site_change,
        }
        changes = {}
        for field, value in desired.items():
            current = record.get(field) if field == "name" else object_id(record.get(field))
            if current != value:
                if not flags[field]:
                    raise SafetyError(f"Required {field} change disabled for VM {vm.name}")
                changes[field] = value
        return changes

    def bootstrap_candidate(self, vm: VM) -> tuple[dict, int]:
        cid, _, _ = self.targets(vm)
        all_candidates = self.by_name[vm.name.casefold()]
        candidates = self.source_candidates(vm, all_candidates)
        if self.vm_names[vm.source, vm.name.casefold()] != 1 or len(candidates) != 1:
            ids = [(r["id"], object_id(r.get("cluster"))) for r in candidates]
            raise SafetyError(
                f"Ambiguous/missing bootstrap candidate source={vm.source} VM={vm.name}: {ids}"
            )
        record = candidates[0]
        if not self.owned(record, vm) or object_id(record.get("cluster")) != cid:
            raise SafetyError(f"Bootstrap ownership/cluster mismatch for {vm.name}; bind manually")
        self.duplicates(vm, record, cid, all_candidates)
        return record, cid

    def bootstrap(self, apply: bool = False) -> int:
        self.register_sources(apply)
        errors = 0
        bound = {r["netbox_vm_id"] for r in self.mappings.values()}
        for vm in self.vms:
            try:
                if vm.key in self.mappings:
                    row = self.mappings[vm.key]
                    existing = self.by_id.get(row["netbox_vm_id"])
                    if not existing:
                        raise SafetyError("Mapped NetBox VM is missing")
                    self.identity(vm, row, existing)
                    continue
                record, cid = self.bootstrap_candidate(vm)
                if record["id"] in bound:
                    raise SafetyError("NetBox VM is already mapped to another VMware identity")
                LOG.info(
                    "%s BIND UUID=%s NetBox ID=%s VM=%s",
                    "APPLY" if apply else "DRY-RUN",
                    vm.instance_uuid,
                    record["id"],
                    vm.name,
                )
                if apply:
                    if self.vmware.refresh(vm) != vm:
                        raise SafetyError("VM changed during bootstrap; rerun required")
                    current = self.netbox.get_vm(record["id"])
                    if current != record:
                        raise SafetyError("NetBox candidate changed during bootstrap")
                    all_candidates = self.netbox.named(vm.name)
                    candidates = self.source_candidates(vm, all_candidates)
                    if (
                        len(candidates) != 1
                        or candidates[0]["id"] != record["id"]
                        or not self.owned(candidates[0], vm)
                        or object_id(candidates[0].get("cluster")) != cid
                    ):
                        raise SafetyError("Bootstrap candidate set changed")
                    self.duplicates(vm, record, cid, all_candidates)
                    self.store.bind(vm, record["id"], cid)
                bound.add(record["id"])
            except SafetyError as exc:
                errors += 1
                LOG.error("%s", exc)
        return 2 if errors else 0

    def bind(self, source: str, instance_uuid: str, pk: int, apply: bool) -> None:
        matches = [v for v in self.vms if v.source == source and v.instance_uuid == instance_uuid]
        if len(matches) != 1:
            raise SafetyError("Manual bind needs one current VMware identity")
        vm = matches[0]
        cid, _, _ = self.targets(vm)
        record = self.netbox.get_vm(pk)
        if not self.owned(record, vm):
            raise SafetyError("Manual bind cannot override source ownership")
        self.duplicates(vm, record, cid)
        LOG.info(
            "%s manual BIND UUID=%s NetBox ID=%s",
            "APPLY" if apply else "DRY-RUN",
            vm.instance_uuid,
            pk,
        )
        if apply:
            if self.vmware.refresh(vm) != vm:
                raise SafetyError("VM changed during manual bind")
            if self.netbox.get_vm(pk) != record:
                raise SafetyError("NetBox candidate changed during manual bind")
            candidates = self.netbox.named(vm.name)
            if record["name"].casefold() != vm.name.casefold():
                candidates += self.netbox.named(record["name"])
            self.duplicates(vm, record, cid, candidates)
            self.register_sources(True, [source])
            self.store.bind(vm, pk, cid)

    def reconcile(self, apply: bool = False, check: bool = False) -> int:
        if set(self.observed) - set(self.registered):
            raise SafetyError("Unregistered vCenter UUID; run bootstrap --dry-run then --apply")
        errors = 0
        for key, absent_row in self.mappings.items():
            if key not in self.vm_by_key:
                LOG.warning(
                    "Mapped VM absent in VMware; retained NetBox ID=%s", absent_row["netbox_vm_id"]
                )
        for vm in self.vms:
            try:
                row = self.mappings.get(vm.key)
                if row is None:
                    candidates = self.source_candidates(vm, self.by_name[vm.name.casefold()])
                    if len(candidates) > 1:
                        ids = [(r["id"], object_id(r.get("cluster"))) for r in candidates]
                        raise SafetyError(
                            f"DUPLICATE DETECTED UUID={vm.instance_uuid} "
                            f"NetBox (ID, Cluster)={ids}; manual resolution required"
                        )
                    raise SafetyError(
                        f"Unregistered VMware UUID={vm.instance_uuid} VM={vm.name}; "
                        "bootstrap/manual review required"
                    )
                cid, did, sid = self.targets(vm)
                record = self.netbox.get_vm(row["netbox_vm_id"])
                self.identity(vm, row, record)
                self.duplicates(vm, record, cid)
                changes = self.delta(vm, record, cid, did, sid)
                if changes:
                    differences = {
                        field: {
                            "old": record.get(field)
                            if field == "name"
                            else object_id(record.get(field)),
                            "new": value,
                        }
                        for field, value in changes.items()
                    }
                    LOG.info(
                        "%s VM=%s UUID=%s NetBox ID=%s PATCH=%s CHANGES=%s",
                        "APPLY PLAN" if apply else "DRY-RUN",
                        vm.name,
                        vm.instance_uuid,
                        record["id"],
                        changes,
                        differences,
                    )
                    if check:
                        raise SafetyError("Placement drift requires reconciliation")
                    if apply:
                        if log_inventory_diff([vm], [self.vmware.refresh(vm)]):
                            raise SafetyError("VMware VM changed during processing; rerun required")
                        current = self.netbox.get_vm(record["id"])
                        if current != record:
                            raise SafetyError("NetBox VM changed concurrently; rerun required")
                        cluster, device = self.netbox.placement(cid, did)
                        source = self.sources[vm.source]
                        target_changes = {
                            "Cluster": placement_diff(
                                self.clusters[cid], cluster, "Cluster", source
                            ),
                            "Device": placement_diff(self.devices[did], device, "Device", source),
                        }
                        if any(target_changes.values()):
                            for kind, changed in target_changes.items():
                                if changed:
                                    LOG.error(
                                        "Target %s changed source=%s fields=%s",
                                        kind,
                                        vm.source,
                                        json.dumps(changed, ensure_ascii=False),
                                    )
                            raise SafetyError("Target Cluster/Device changed concurrently")
                        self.validate_placement(cluster, device, cid, did)
                        if object_id(device["site"]) != sid:
                            raise SafetyError("Device Site changed concurrently")
                        candidates = self.netbox.named(vm.name)
                        if record["name"].casefold() != vm.name.casefold():
                            candidates += self.netbox.named(record["name"])
                        self.duplicates(vm, record, cid, candidates)
                        self.netbox.patch_vm(record["id"], changes)
                        confirmed = self.netbox.get_vm(record["id"])
                        if self.delta(vm, confirmed, cid, did, sid):
                            raise SafetyError("PATCH result does not match desired placement")
                        self.identity(vm, row, confirmed)
                        LOG.info(
                            "APPLIED VM=%s UUID=%s NetBox ID=%s PATCH=%s",
                            vm.name,
                            vm.instance_uuid,
                            record["id"],
                            changes,
                        )
                        # Keep in-memory conflict indices current after a successful rename.
                        self.by_name[record["name"].casefold()] = [
                            r
                            for r in self.by_name[record["name"].casefold()]
                            if r["id"] != record["id"]
                        ]
                        self.by_name[vm.name.casefold()].append(confirmed)
                if apply:
                    self.store.seen(vm, cid)
            except SafetyError as exc:
                errors += 1
                LOG.error("VM=%s UUID=%s: %s", vm.name, vm.instance_uuid, exc)
        try:
            final_inventory = self.vmware.inventory()
            validate_inventory(final_inventory)
            if log_inventory_diff(self.vms, final_inventory):
                raise SafetyError("VMware inventory changed during run; SSoT must remain stopped")
        except SafetyError as exc:
            errors += 1
            LOG.error("%s", exc)
        LOG.info("Checked %d VMware VMs; errors=%d", len(self.vms), errors)
        return 2 if errors else 0
