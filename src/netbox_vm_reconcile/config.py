"""Strict YAML configuration with automatic discovery and optional identity pins."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .identity import SafetyError, uuid


@dataclass(frozen=True)
class VCenter:
    name: str
    hostname: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    expected_uuid: str | None = None
    cluster_ids: dict[str, int] = field(default_factory=dict)
    host_ids: dict[str, int] = field(default_factory=dict)
    port: int = 443
    ca_file: str | None = None
    timeout: int = 30
    source_tag: str = ""
    ignore_templates: bool = True
    verify_ssl: bool = True


@dataclass(frozen=True)
class Config:
    url: str
    token: str = field(repr=False)
    database: Path
    vcenters: list[VCenter]
    ca_file: str | None = None
    timeout: int = 30
    allow_rename: bool = True
    allow_cluster_change: bool = True
    allow_host_change: bool = True
    allow_site_change: bool = True


def _keys(data: Any, allowed: set[str], label: str) -> dict:
    if not isinstance(data, dict) or set(data) - allowed:
        raise SafetyError(f"Invalid or unknown keys in {label}")
    return data


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SafetyError("Required configuration string is empty")
    return value


def _secret(value: Any, credentials: bool = True) -> str:
    value = _text(value)
    if not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value):
        raise SafetyError("Credentials must use a ${VARIABLE} reference")
    if not credentials:
        return ""
    result = os.environ.get(value[2:-1])
    if not result:
        raise SafetyError(f"Required environment variable {value[2:-1]} is unset")
    return result


def _positive(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise SafetyError("Expected a positive integer in configuration")
    return value


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        raise SafetyError("Expected a YAML boolean")
    return value


def _ids(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise SafetyError("cluster_ids and host_ids must be explicit MoRef -> NetBox ID maps")
    return {_text(k): _positive(v) for k, v in value.items()}


def load(path: str, credentials: bool = True) -> Config:
    try:
        data = yaml.safe_load(Path(path).read_text())
        _keys(data, {"netbox", "state", "vmware", "reconcile"}, "config")
        nb = _keys(data["netbox"], {"url", "token", "timeout", "ca_file"}, "netbox")
        state = _keys(data["state"], {"database"}, "state")
        options = _keys(
            data.get("reconcile", {}),
            {"allow_rename", "allow_cluster_change", "allow_host_change", "allow_site_change"},
            "reconcile",
        )
        for value in options.values():
            _bool(value)
        url = _text(nb["url"]).rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SafetyError(
                "NetBox URL must be HTTPS without embedded credentials/query/fragment"
            )
        vcs = []
        for entry in data["vmware"]:
            v = _keys(entry, set(VCenter.__dataclass_fields__), "vmware entry")
            vcs.append(
                VCenter(
                    name=_text(v["name"]),
                    hostname=_text(v["hostname"]),
                    username=_secret(v["username"], credentials),
                    password=_secret(v["password"], credentials),
                    expected_uuid=(
                        uuid(v["expected_uuid"]) if v.get("expected_uuid") is not None else None
                    ),
                    cluster_ids=_ids(v.get("cluster_ids", {})),
                    host_ids=_ids(v.get("host_ids", {})),
                    port=_positive(v.get("port", 443)),
                    ca_file=v.get("ca_file"),
                    timeout=_positive(v.get("timeout", 30)),
                    source_tag=_text(v.get("source_tag", f"Source: {v['name']}")),
                    ignore_templates=_bool(v.get("ignore_templates", True)),
                    verify_ssl=_bool(v.get("verify_ssl", True)),
                )
            )
        if (
            not vcs
            or len({v.name for v in vcs}) != len(vcs)
            or len({v.expected_uuid for v in vcs if v.expected_uuid})
            != sum(v.expected_uuid is not None for v in vcs)
        ):
            raise SafetyError("vCenter source names and configured UUID pins must be unique")
        db = Path(_text(state["database"]))
        if not db.is_absolute():
            raise SafetyError("State database path must be absolute")
        return Config(
            url,
            _secret(nb["token"], credentials),
            db,
            vcs,
            nb.get("ca_file"),
            _positive(nb.get("timeout", 30)),
            **options,
        )
    except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError) as exc:
        raise SafetyError(
            "Invalid/unreadable configuration; check required fields and YAML"
        ) from exc
