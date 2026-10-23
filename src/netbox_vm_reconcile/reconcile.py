"""Plan and execute conservative updates, without owning any VM network resources."""

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from .config import Config
from .identity import VM, SafetyError, inventory_diff, object_id, uuid, validate_inventory
from .mappings import Store
from .placement import Placement, owned, placement_diff

LOG = logging.getLogger(__name__)
RECONCILE_ATTEMPTS = 3
PLACEMENT_FIELDS = {"host_ref", "host_name", "cluster_ref", "cluster_name"}


class PlacementChanged(SafetyError):
    """A fresh inventory and reconciliation plan may resolve a concurrent vMotion."""


@dataclass
class MappingPlan:
    bindings: list[tuple[VM, int, int]] = dataclass_field(default_factory=list)
    pending: set[tuple[str, str]] = dataclass_field(default_factory=set)
    blocked: set[tuple[str, str]] = dataclass_field(default_factory=set)
    removals: list[dict] = dataclass_field(default_factory=list)
    errors: int = 0


def log_inventory_diff(before: list[VM], after: list[VM], retry_placement: bool = False) -> bool:
    changes = inventory_diff(before, after)
    retry = (
        retry_placement
        and bool(changes)
        and all(
            change["kind"] == "modified" and set(change["fields"]) <= PLACEMENT_FIELDS
            for change in changes
        )
    )
    for change in changes:
        LOG.log(
            logging.INFO if retry else logging.ERROR,
            "VMware inventory difference: %s",
            json.dumps(change, ensure_ascii=False),
        )
    if retry:
        raise PlacementChanged("VMware placement changed during reconciliation")
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
        self.mapping_by_id = {m["netbox_vm_id"]: m for m in self.mappings.values()}
        self.vm_by_key = {v.key: v for v in self.vms}
        self.present = vmware.present_keys()
        if not self.vm_by_key.keys() <= self.present:
            raise SafetyError("Incomplete VMware presence inventory")
        self.history_bios: dict[str, list[dict]] = defaultdict(list)
        self.history_names: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for key, row in self.mappings.items():
            if self.registered.get(row["source_name"]) != key[0]:
                raise SafetyError("Mapped vCenter UUID/source missing from configuration")
            self.history_bios[row["vm_bios_uuid"]].append(row)
            self.history_names[row["source_name"], row["last_known_name"].casefold()].append(row)

    def owned(self, record: dict, vm: VM) -> bool:
        return owned(record, self.sources[vm.source])

    def source_candidates(self, vm: VM, candidates: list[dict]) -> list[dict]:
        # Retain same-source objects with invalid/missing tags so they still cause
        # ambiguity instead of silently hiding a possible duplicate.
        return [r for r in candidates if (r.get("custom_fields") or {}).get("source") == vm.source]

    def register_sources(self, apply: bool, names=None, persist: bool = True) -> None:
        selected = self.observed if names is None else {n: self.observed[n] for n in names}
        for name, observed in selected.items():
            if name not in self.registered:
                LOG.info(
                    "%s register vCenter source=%s UUID=%s",
                    "APPLY PLAN" if apply else "DRY-RUN",
                    name,
                    observed,
                )
        if apply and persist:
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

    def bootstrap_candidate(self, vm: VM, all_candidates=None) -> tuple[dict, int] | None:
        if all_candidates is None:
            all_candidates = self.by_name[vm.name.casefold()]
        candidates = self.source_candidates(vm, all_candidates)
        if self.vm_names[vm.source, vm.name.casefold()] != 1 or len(candidates) > 1:
            ids = [(r["id"], object_id(r.get("cluster"))) for r in candidates]
            raise SafetyError(
                f"{'DUPLICATE DETECTED: ' if len(candidates) > 1 else ''}"
                f"Ambiguous bootstrap candidate source={vm.source} VM={vm.name}: {ids}"
            )
        if not candidates:
            if all_candidates:
                cid = self.placement.resolve(
                    "Cluster", self.sources[vm.source], vm.cluster_ref, vm.cluster_name
                )
                if any(
                    not (r.get("custom_fields") or {}).get("source")
                    or object_id(r.get("cluster")) == cid
                    for r in all_candidates
                ):
                    raise SafetyError(f"Possible foreign/unowned NetBox candidate for {vm.name}")
            return None
        cid, _, _ = self.targets(vm)
        record = candidates[0]
        if not self.owned(record, vm) or object_id(record.get("cluster")) != cid:
            raise SafetyError(f"Bootstrap ownership/cluster mismatch for {vm.name}; bind manually")
        self.duplicates(vm, record, cid, all_candidates)
        return record, cid

    def defer_replacement(self, vm: VM, previous: list[dict], plan: MappingPlan, apply: bool):
        """Let SSoT settle a replacement before enrolling its current NetBox identity."""
        for row in previous:
            key = row["vcenter_uuid"], row["vm_instance_uuid"]
            if key in self.present or row["source_name"] != vm.source:
                raise SafetyError(
                    "Previous VMware identity is still present or belongs to another source; "
                    "manual review required"
                )
        if self.vm_names[vm.source, vm.name.casefold()] != 1:
            raise SafetyError("Ambiguous replacement name in VMware; manual review required")
        snapshots = [self.by_name[vm.name.casefold()]]
        if apply:
            snapshots.append(self.netbox.named(vm.name))
        for records in snapshots:
            candidates = self.source_candidates(vm, records)
            if len(candidates) > 1:
                raise SafetyError("DUPLICATE DETECTED: multiple NetBox replacement candidates")
            for record in candidates:
                if not self.owned(record, vm):
                    raise SafetyError("Replacement NetBox ownership mismatch")
                bound = self.mapping_by_id.get(record["id"])
                if bound and (bound["vcenter_uuid"], bound["vm_instance_uuid"]) in self.present:
                    raise SafetyError("Replacement NetBox ID belongs to a present VMware VM")
            foreign = [r for r in records if r not in candidates]
            if foreign:
                cid = self.placement.resolve(
                    "Cluster", self.sources[vm.source], vm.cluster_ref, vm.cluster_name
                )
                if any(
                    not (r.get("custom_fields") or {}).get("source")
                    or object_id(r.get("cluster")) == cid
                    for r in foreign
                ):
                    raise SafetyError("Replacement conflicts with a foreign/unowned NetBox VM")
        LOG.info(
            "RECREATED VM source=%s VM=%s UUID=%s previous UUIDs=%s; "
            "old identities absent from VMware; left for SSoT; NetBox untouched",
            vm.source,
            vm.name,
            vm.instance_uuid,
            sorted({r["vm_instance_uuid"] for r in previous}),
        )
        plan.pending.add(vm.key)

    def plan_mappings(self, apply: bool, retry_placement: bool = False) -> MappingPlan:
        plan = MappingPlan(
            removals=[r for key, r in self.mappings.items() if key not in self.present]
        )
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
                previous = (
                    self.history_bios[vm.bios_uuid]
                    + self.history_names[vm.source, vm.name.casefold()]
                )
                candidates = self.source_candidates(vm, self.by_name[vm.name.casefold()])
                if len(candidates) == 1:
                    previous_row = self.mapping_by_id.get(candidates[0]["id"])
                    if previous_row:
                        previous.append(previous_row)
                if previous:
                    self.defer_replacement(vm, previous, plan, apply)
                    continue
                candidate = self.bootstrap_candidate(vm)
                if candidate is None:
                    if (
                        apply
                        and self.bootstrap_candidate(vm, self.netbox.named(vm.name)) is not None
                    ):
                        raise SafetyError(
                            "NetBox candidate appeared during discovery; rerun required"
                        )
                    LOG.info(
                        "NEW VM source=%s VM=%s UUID=%s absent from NetBox; left for SSoT",
                        vm.source,
                        vm.name,
                        vm.instance_uuid,
                    )
                    plan.pending.add(vm.key)
                    continue
                record, cid = candidate
                if record["id"] in bound:
                    previous_row = self.mapping_by_id.get(record["id"])
                    if previous_row:
                        self.defer_replacement(vm, [previous_row], plan, apply)
                        continue
                    raise SafetyError("NetBox VM is already mapped to another VMware identity")
                LOG.info(
                    "%s BIND source=%s UUID=%s NetBox ID=%s VM=%s",
                    "APPLY PLAN" if apply else "DRY-RUN",
                    vm.source,
                    vm.instance_uuid,
                    record["id"],
                    vm.name,
                )
                if apply:
                    if log_inventory_diff([vm], [self.vmware.refresh(vm)], retry_placement):
                        raise SafetyError("VM changed during bootstrap; rerun required")
                    current = self.netbox.get_vm(record["id"])
                    if current != record:
                        raise SafetyError("NetBox candidate changed during bootstrap")
                    refreshed = self.bootstrap_candidate(vm, self.netbox.named(vm.name))
                    if refreshed is None or (refreshed[0]["id"], refreshed[1]) != (
                        record["id"],
                        cid,
                    ):
                        raise SafetyError("Bootstrap candidate set changed")
                plan.bindings.append((vm, record["id"], cid))
                bound.add(record["id"])
            except PlacementChanged:
                raise
            except SafetyError as exc:
                plan.errors += 1
                plan.blocked.add(vm.key)
                LOG.error("source=%s VM=%s: %s", vm.source, vm.name, exc)
        for row in plan.removals:
            LOG.info(
                "%s REMOVE MAPPING source=%s UUID=%s NetBox ID=%s; NetBox object untouched",
                "STATE PLAN" if apply else "DRY-RUN",
                row["source_name"],
                row["vm_instance_uuid"],
                row["netbox_vm_id"],
            )
        return plan

    def finish_mappings(
        self,
        plan: MappingPlan,
        apply: bool,
        errors: int,
        seen: list[tuple[VM, int]],
        accepted: set[tuple[str, str]],
        register: bool = False,
    ) -> int:
        stable = True
        final_present = self.present
        try:
            final_inventory = self.vmware.inventory()
            validate_inventory(final_inventory)
            final_present = self.vmware.present_keys()
            if not {v.key for v in final_inventory} <= final_present:
                raise SafetyError("Incomplete final VMware presence inventory")
            if log_inventory_diff(
                self.vms, final_inventory, retry_placement=apply and not register and not errors
            ):
                raise SafetyError("VMware inventory changed during run; SSoT must remain stopped")
        except PlacementChanged:
            raise
        except SafetyError as exc:
            stable = False
            errors += 1
            LOG.error("%s", exc)
        bindings = [b for b in plan.bindings if b[0].key in accepted] if stable else []
        removals = (
            [
                r
                for r in plan.removals
                if (r["vcenter_uuid"], r["vm_instance_uuid"]) not in final_present
            ]
            if stable and not errors
            else []
        )
        if plan.removals and (not stable or errors):
            LOG.warning("Mapping cleanup deferred because checks failed; existing rows retained")
        if apply:
            self.store.apply_batch(
                bindings,
                [(r["vcenter_uuid"], r["vm_instance_uuid"], r["netbox_vm_id"]) for r in removals],
                seen,
                self.observed if register and stable else None,
            )
            if register and stable:
                self.registered.update(self.observed)
            for vm, pk, _ in bindings:
                LOG.info("BOUND source=%s UUID=%s NetBox ID=%s", vm.source, vm.instance_uuid, pk)
            for row in removals:
                LOG.info(
                    "REMOVED MAPPING source=%s UUID=%s NetBox ID=%s; NetBox untouched",
                    row["source_name"],
                    row["vm_instance_uuid"],
                    row["netbox_vm_id"],
                )
        LOG.info(
            "Checked %d VMware VMs; pending SSoT=%d; errors=%d",
            len(self.vms),
            len(plan.pending),
            errors,
        )
        return 2 if errors else 0

    def bootstrap(self, apply: bool = False) -> int:
        self.register_sources(apply, persist=False)
        plan = self.plan_mappings(apply)
        return self.finish_mappings(
            plan, apply, plan.errors, [], {vm.key for vm, _, _ in plan.bindings}, register=True
        )

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
        engine = self
        for attempt in range(1, RECONCILE_ATTEMPTS + 1):
            try:
                if attempt > 1:
                    # Re-read both systems and rebuild all ownership/conflict/placement indices.
                    # Confirmed NetBox PATCHes from an earlier attempt remain and are idempotent.
                    engine = Engine(self.config, self.store, self.vmware, self.netbox)
                return engine._reconcile_once(apply, check)
            except PlacementChanged:
                if attempt == RECONCILE_ATTEMPTS:
                    LOG.error(
                        "VMware placement kept changing after %d attempts; "
                        "SQLite lifecycle changes not committed; SSoT must remain stopped",
                        RECONCILE_ATTEMPTS,
                    )
                    return 2
                LOG.warning(
                    "VMware placement changed; refreshing VMware and NetBox and retrying "
                    "reconciliation attempt=%d/%d",
                    attempt + 1,
                    RECONCILE_ATTEMPTS,
                )
        raise AssertionError("Unreachable reconciliation attempt")

    def _reconcile_once(self, apply: bool, check: bool) -> int:
        if set(self.observed) - set(self.registered):
            raise SafetyError("Unregistered vCenter UUID; run bootstrap --dry-run then --apply")
        plan = self.plan_mappings(apply, retry_placement=apply)
        errors = plan.errors
        staged_rows = {
            vm.key: {
                "vcenter_uuid": vm.vcenter_uuid,
                "vm_instance_uuid": vm.instance_uuid,
                "vm_bios_uuid": vm.bios_uuid,
                "netbox_vm_id": pk,
                "source_name": vm.source,
            }
            for vm, pk, _ in plan.bindings
        }
        seen: list[tuple[VM, int]] = []
        accepted = set()
        for vm in self.vms:
            if vm.key in plan.pending or vm.key in plan.blocked:
                continue
            try:
                row = self.mappings.get(vm.key) or staged_rows.get(vm.key)
                if row is None:
                    raise SafetyError("Mapping discovery did not classify the VMware VM")
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
                        if log_inventory_diff(
                            [vm], [self.vmware.refresh(vm)], retry_placement=True
                        ):
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
                accepted.add(vm.key)
                if apply:
                    seen.append((vm, cid))
            except PlacementChanged:
                raise
            except SafetyError as exc:
                errors += 1
                LOG.error("VM=%s UUID=%s: %s", vm.name, vm.instance_uuid, exc)
        return self.finish_mappings(plan, apply, errors, seen, accepted)
