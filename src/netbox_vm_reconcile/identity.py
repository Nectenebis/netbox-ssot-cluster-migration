"""Identity and shared safety primitives; no remote mutations."""

from dataclasses import dataclass
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
