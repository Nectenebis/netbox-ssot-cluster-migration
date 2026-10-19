"""Identity and shared safety primitives; no remote mutations."""

from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID


class SafetyError(Exception):
    """An operation cannot safely proceed (message must never contain credentials)."""


def uuid(value: Any) -> str:
    try:
        result = UUID(value)
        if result.int == 0:
            raise ValueError
        return str(result)
    except (ValueError, TypeError, AttributeError) as exc:
        raise SafetyError("Missing or invalid VMware UUID") from exc


def object_id(value: Any) -> int | None:
    return value.get("id") if isinstance(value, dict) else value


@dataclass(frozen=True)
class VM:
    source: str
    vcenter_uuid: str
    instance_uuid: str
    bios_uuid: str
    moref: str
    name: str
    cluster_ref: str
    host_ref: str
    cluster_name: str = ""
    host_name: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.vcenter_uuid, self.instance_uuid


def validate_inventory(vms: list[VM]) -> None:
    keys: set[tuple[str, str]] = set()
    bios: set[str] = set()
    for vm in vms:
        if vm.key in keys or vm.bios_uuid in bios:
            raise SafetyError(f"Duplicate VMware UUID: source={vm.source} VM={vm.name}")
        keys.add(vm.key)
        bios.add(vm.bios_uuid)


def inventory_diff(before: list[VM], after: list[VM]) -> list[dict]:
    """Describe snapshot differences without guessing identities from names or MoRefs."""
    old_by_key = {vm.key: vm for vm in before}
    new_by_key = {vm.key: vm for vm in after}
    changes = []
    for key in sorted(old_by_key.keys() | new_by_key.keys()):
        old, new = old_by_key.get(key), new_by_key.get(key)
        if old is None and new is not None:
            changes.append({"kind": "added", "vm": asdict(new)})
        elif new is None and old is not None:
            changes.append({"kind": "removed", "vm": asdict(old)})
        elif old is not None and new is not None and old != new:
            old_fields, new_fields = asdict(old), asdict(new)
            changes.append(
                {
                    "kind": "modified",
                    "source": new.source,
                    "name": new.name,
                    "vcenter_uuid": new.vcenter_uuid,
                    "instance_uuid": new.instance_uuid,
                    "fields": {
                        field: {"old": old_fields[field], "new": new_fields[field]}
                        for field in old_fields
                        if old_fields[field] != new_fields[field]
                    },
                }
            )
    return changes
