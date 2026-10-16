import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import requests
from pyVmomi import vim

from netbox_vm_reconcile.identity import SafetyError
from netbox_vm_reconcile.netbox import NetBox, Session, safe_call
from netbox_vm_reconcile.vmware import VMware, properties

from .conftest import VC, record


def test_real_pynetbox_pagination_and_patch(env, monkeypatch):
    calls = []

    def send(session, request, **kwargs):
        calls.append((request.method, request.url, request.body))
        result = requests.Response()
        result.status_code = 200
        result.request = request
        result.url = request.url
        if request.method == "PATCH":
            data = record()
            data.update(json.loads(request.body))
        elif "offset=1" in request.url:
            data = {"count": 2, "next": None, "previous": None, "results": [record(4000, "other")]}
        elif "virtual-machines/" in request.url:
            data = {
                "count": 2,
                "next": env.cfg.url + "/api/virtualization/virtual-machines/?offset=1",
                "previous": None,
                "results": [record()],
            }
        else:
            data = {"count": 0, "next": None, "previous": None, "results": []}
        result._content = json.dumps(data).encode()
        result.headers["Content-Type"] = "application/json"
        assert kwargs["timeout"] == 30
        return result

    monkeypatch.setattr(Session, "send", send)
    nb = NetBox(env.cfg)
    rows, _, _ = nb.snapshot()
    assert {r["id"] for r in rows} == {3219, 4000}
    nb.patch_vm(3219, {"cluster": 20, "device": 102, "site": 2})
    writes = [call for call in calls if call[0] != "GET"]
    assert len(writes) == 1
    assert writes[0][0] == "PATCH" and writes[0][1].endswith("/3219/")
    assert json.loads(writes[0][2]) == {"cluster": 20, "device": 102, "site": 2}
    nb.close()


def test_transport_rejects_external_pagination(env):
    with pytest.raises(SafetyError, match="escaped"):
        Session(env.cfg).get("https://attacker.example/api/")


def test_transport_rejects_redirect(env, monkeypatch):
    response = requests.Response()
    response.status_code = 302
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: response)
    with pytest.raises(SafetyError, match="redirect"):
        Session(env.cfg).get(env.cfg.url + "/api/")


def test_api_error_does_not_expose_credentials():
    def fail():
        raise requests.exceptions.SSLError("secret-password")

    with pytest.raises(SafetyError) as error:
        safe_call(fail)
    assert "secret-password" not in str(error.value)


def test_adapter_forbids_resource_mutation(env):
    nb = NetBox(env.cfg)
    for changes in ({"vcpus": 8}, {"custom_fields": {}}, {"interfaces": []}, {}):
        with pytest.raises(SafetyError):
            nb.patch_vm(3219, changes)
    nb.close()


def test_propertycollector_pagination_and_cleanup():
    content = MagicMock()
    # Construct actual pyVmomi managed objects to exercise FilterSpec/TraversalSpec types.
    content.rootFolder = vim.Folder("group-root")
    view = vim.view.ContainerView("view-1")
    content.viewManager.CreateContainerView.return_value = view
    content.propertyCollector.RetrievePropertiesEx.return_value = NS(objects=[], token="next")
    content.propertyCollector.ContinueRetrievePropertiesEx.return_value = NS(objects=[], token=None)
    from unittest.mock import patch

    with patch.object(vim.view.ContainerView, "Destroy") as destroy:
        assert properties(content, vim.VirtualMachine, ["name"]) == []
    destroy.assert_called_once()
    content.propertyCollector.ContinueRetrievePropertiesEx.assert_called_once_with(token="next")
    options = content.propertyCollector.RetrievePropertiesEx.call_args.kwargs["options"]
    assert options.maxObjects == 500


def test_vcenter_identity_pinned(env, monkeypatch):
    si = MagicMock()
    si.RetrieveContent.return_value.about = NS(instanceUuid=VC, apiType="VirtualCenter")
    connect = MagicMock(return_value=si)
    disconnect = MagicMock()
    monkeypatch.setattr("netbox_vm_reconcile.vmware.SmartConnect", connect)
    monkeypatch.setattr("netbox_vm_reconcile.vmware.Disconnect", disconnect)
    with VMware(env.cfg.vcenters):
        pass
    assert connect.call_args.kwargs["sslContext"].verify_mode == 2
    disconnect.assert_called_once_with(si)
    si.RetrieveContent.return_value.about.instanceUuid = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(SafetyError, match="identity mismatch"):
        with VMware(env.cfg.vcenters):
            pass


def test_vmware_connection_failure_is_sanitized(env, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("secret-password")

    monkeypatch.setattr("netbox_vm_reconcile.vmware.SmartConnect", fail)
    with pytest.raises(SafetyError) as error:
        with VMware(env.cfg.vcenters):
            pass
    assert "secret-password" not in str(error.value)
