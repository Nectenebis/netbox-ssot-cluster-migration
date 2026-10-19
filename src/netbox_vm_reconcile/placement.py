"""Indexed placement discovery scoped to one SSoT source. Never guess on ambiguity."""

from collections import defaultdict

from .config import VCenter
from .identity import SafetyError, object_id

FOREIGN_TYPES = {"ovirt", "opennebula", "proxmox", "openstack", "hetznercloud"}


def owned(record: dict, source: VCenter) -> bool:
    tags = {tag["name"] for tag in record.get("tags", [])}
    return (
        record.get("custom_fields", {}).get("source") == source.name
        and "vmware" in tags
        and (source.source_tag or f"Source: {source.name}") in tags
        and not tags.intersection(FOREIGN_TYPES)
    )


def placement_state(record: dict, kind: str, source: VCenter) -> dict:
    """Only identity, selection, and assignment fields affect safe placement.

    API counters change after our own PATCHes. Timestamps, unrelated custom fields,
    display labels, and unrelated tags must not turn these into false conflicts.
    """
    fields = record.get("custom_fields") or {}
    result = {
        "id": record.get("id"),
        "name": record.get("name"),
        "source": fields.get("source"),
        "source_id": fields.get("source_id"),
        "owned": owned(record, source),
    }
    if kind == "Cluster":
        result.update(scope_type=record.get("scope_type"), scope_id=record.get("scope_id"))
    elif kind == "Device":
        result.update(cluster=object_id(record.get("cluster")), site=object_id(record.get("site")))
    else:
        raise SafetyError("Unknown placement object type")
    return result


def placement_diff(before: dict, after: dict, kind: str, source: VCenter) -> dict:
    old, new = placement_state(before, kind, source), placement_state(after, kind, source)
    return {
        field: {"old": old[field], "new": new[field]} for field in old if old[field] != new[field]
    }


class Placement:
    def __init__(self, clusters: dict[int, dict], devices: dict[int, dict]):
        self.records = {"Cluster": clusters, "Device": devices}
        self.refs: dict[tuple, list[dict]] = defaultdict(list)
        self.names: dict[tuple, list[dict]] = defaultdict(list)
        for kind, records in self.records.items():
            for record in records.values():
                fields = record.get("custom_fields") or {}
                source, source_id = fields.get("source"), fields.get("source_id")
                if source:
                    if source_id:
                        self.refs[kind, source, str(source_id)].append(record)
                    if record.get("name"):
                        self.names[kind, source, record["name"].casefold()].append(record)

    def resolve(self, kind: str, source: VCenter, moref: str, name: str) -> int:
        overrides = source.cluster_ids if kind == "Cluster" else source.host_ids
        if moref in overrides:
            pk = overrides[moref]
            if pk not in self.records[kind]:
                raise SafetyError(f"Configured {kind} ID={pk} is missing")
            return pk
        candidates = self.refs[kind, source.name, moref]
        matched_by_ref = bool(candidates)
        if not candidates and name:
            candidates = self.names[kind, source.name, name.casefold()]
        if len(candidates) != 1:
            ids = [r["id"] for r in candidates]
            raise SafetyError(
                f"Missing/ambiguous {kind} source={source.name} MoRef={moref} "
                f"name={name!r} candidates={ids}; optional ID override available"
            )
        record = candidates[0]
        if not owned(record, source):
            raise SafetyError(
                f"{kind} ownership mismatch for source={source.name} ID={record['id']}"
            )
        existing_ref = record.get("custom_fields", {}).get("source_id")
        if not matched_by_ref and existing_ref:
            raise SafetyError(f"{kind} name matches but source_id contradicts VMware MoRef={moref}")
        return record["id"]
