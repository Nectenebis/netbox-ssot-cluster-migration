"""Plan and execute conservative updates, without owning any VM network resources."""

import logging
from collections import Counter, defaultdict

from .config import Config
from .identity import VM, SafetyError, object_id, validate_inventory
from .mappings import Store

LOG = logging.getLogger(__name__)
FOREIGN_TYPES = {"ovirt", "opennebula", "proxmox", "openstack", "hetznercloud"}


class Engine:
    def __init__(self, config: Config, store: Store, vmware, netbox):
        self.config, self.store, self.vmware, self.netbox = config, store, vmware, netbox
        self.sources = {vc.name: vc for vc in config.vcenters}
        self.vms = vmware.inventory()  # All vCenters must succeed before any write.
        validate_inventory(self.vms)
        self.records, self.clusters, self.devices = netbox.snapshot()
        self.by_id = {r["id"]: r for r in self.records}
        self.by_name: dict[str, list[dict]] = defaultdict(list)
        for r in self.records:
            self.by_name[r["name"].casefold()].append(r)
        self.vm_names = Counter(v.name.casefold() for v in self.vms)
        self.mappings = {(m["vcenter_uuid"], m["vm_instance_uuid"]): m for m in store.rows()}
        self.vm_by_key = {v.key: v for v in self.vms}
        configured = {v.expected_uuid: v.name for v in config.vcenters}
        for key, row in self.mappings.items():
            if configured.get(key[0]) != row["source_name"]:
                raise SafetyError("Mapped vCenter UUID/source missing from configuration")

    def owned(self, record: dict, vm: VM) -> bool:
        tags = {tag["name"] for tag in record.get("tags", [])}
        return (
            record.get("custom_fields", {}).get("source") == vm.source
            and "vmware" in tags
            and self.sources[vm.source].source_tag in tags
            and not tags.intersection(FOREIGN_TYPES)
        )

    def targets(self, vm: VM) -> tuple[int, int, int]:
        vc = self.sources[vm.source]
        cid, did = vc.cluster_ids.get(vm.cluster_ref), vc.host_ids.get(vm.host_ref)
        if not cid or not did or cid not in self.clusters or did not in self.devices:
            raise SafetyError(f"Missing configured Cluster/ESXi Device for {vm.name}")
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
        candidates = self.by_name[vm.name.casefold()]
        if self.vm_names[vm.name.casefold()] != 1 or len(candidates) != 1:
            ids = [(r["id"], object_id(r.get("cluster"))) for r in candidates]
            raise SafetyError(f"Ambiguous/missing bootstrap candidate {vm.name}: {ids}")
        record = candidates[0]
        if not self.owned(record, vm) or object_id(record.get("cluster")) != cid:
            raise SafetyError(f"Bootstrap ownership/cluster mismatch for {vm.name}; bind manually")
        return record, cid

    def bootstrap(self, apply: bool = False) -> int:
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
                    candidates = self.netbox.named(vm.name)
                    if len(candidates) != 1 or candidates[0]["id"] != record["id"]:
                        raise SafetyError("Bootstrap candidate set changed")
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
            self.store.bind(vm, pk, cid)

    def reconcile(self, apply: bool = False, check: bool = False) -> int:
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
                    candidates = self.by_name[vm.name.casefold()]
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
                    LOG.info(
                        "%s VM=%s UUID=%s NetBox ID=%s PATCH=%s",
                        "APPLY" if apply else "DRY-RUN",
                        vm.name,
                        vm.instance_uuid,
                        record["id"],
                        changes,
                    )
                    if check:
                        raise SafetyError("Placement drift requires reconciliation")
                    if apply:
                        if self.vmware.refresh(vm) != vm:
                            raise SafetyError("VMware VM changed during processing; rerun required")
                        current = self.netbox.get_vm(record["id"])
                        if current != record:
                            raise SafetyError("NetBox VM changed concurrently; rerun required")
                        cluster, device = self.netbox.placement(cid, did)
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
            if {v.key: v for v in final_inventory} != self.vm_by_key:
                raise SafetyError("VMware inventory changed during run; SSoT must remain stopped")
        except SafetyError as exc:
            errors += 1
            LOG.error("%s", exc)
        LOG.info("Checked %d VMware VMs; errors=%d", len(self.vms), errors)
        return 2 if errors else 0
