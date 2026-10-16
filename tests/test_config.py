from pathlib import Path

import pytest
import yaml

from netbox_vm_reconcile.config import load
from netbox_vm_reconcile.identity import SafetyError


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    example = Path(__file__).parents[1] / "config.example.yaml"
    data = yaml.safe_load(example.read_text())
    for name in ("NETBOX_TOKEN", "VCENTER_USER", "VCENTER_PASSWORD"):
        monkeypatch.setenv(name, "not-a-real-credential")
    data["state"]["database"] = str(tmp_path / "state.db")
    path = tmp_path / "config.yaml"

    def save():
        path.write_text(yaml.safe_dump(data))
        return str(path)

    return data, save


def test_valid_config(config_file):
    _, save = config_file
    cfg = load(save())
    assert cfg.timeout == 30
    assert cfg.vcenters[0].cluster_ids["domain-c102"] == 20
    assert "not-a-real-credential" not in repr(cfg)


@pytest.mark.parametrize("mutation", ["token", "tls", "bool", "unknown", "uuid", "url", "ids"])
def test_invalid_config(config_file, mutation):
    data, save = config_file
    if mutation == "token":
        data["netbox"]["token"] = "literal-secret"
    elif mutation == "tls":
        data["netbox"]["verify_ssl"] = False
    elif mutation == "bool":
        data["reconcile"]["allow_rename"] = "false"
    elif mutation == "unknown":
        data["reconcile"]["fail_on_conflicts"] = False
    elif mutation == "uuid":
        data["vmware"][0]["expected_uuid"] = "invalid"
    elif mutation == "url":
        data["netbox"]["url"] = "http://netbox.example.local"
    else:
        data["vmware"][0]["host_ids"]["host-201"] = True
    with pytest.raises(SafetyError):
        load(save())


def test_missing_secret_names_only(config_file, monkeypatch):
    _, save = config_file
    monkeypatch.delenv("NETBOX_TOKEN")
    with pytest.raises(SafetyError, match="NETBOX_TOKEN is unset"):
        load(save())


def test_local_state_config_does_not_resolve_secrets(config_file, monkeypatch):
    from netbox_vm_reconcile import cli

    _, save = config_file
    for name in ("NETBOX_TOKEN", "VCENTER_USER", "VCENTER_PASSWORD"):
        monkeypatch.delenv(name)
    path = save()
    cfg = load(path, credentials=False)
    assert cfg.token == "" and cfg.vcenters[0].password == ""
    assert cli.main(["mappings", "init", "--config", path, "--apply"]) == 0
    assert cli.main(["mappings", "list", "--config", path]) == 0
