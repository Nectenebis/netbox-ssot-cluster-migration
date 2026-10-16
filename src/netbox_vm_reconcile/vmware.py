"""Read-only vSphere PropertyCollector inventory with bounded, paginated requests."""

import ssl
from contextlib import ExitStack

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .config import VCenter
from .identity import VM, SafetyError, uuid, validate_inventory

PC = vmodl.query.PropertyCollector
VM_PATHS = ["name", "config.instanceUuid", "config.uuid", "config.template", "runtime.host"]


def properties(content, kind, paths, objects=None) -> list:
    view = None
    if objects is None:
        view = content.viewManager.CreateContainerView(content.rootFolder, [kind], True)
        spec = PC.ObjectSpec(
            obj=view,
            skip=True,
            selectSet=[
                PC.TraversalSpec(name="view", type=vim.view.ContainerView, path="view", skip=False)
            ],
        )
        objectspecs = [spec]
    else:
        objectspecs = [PC.ObjectSpec(obj=obj, skip=False) for obj in objects]
    token = None
    try:
        result = content.propertyCollector.RetrievePropertiesEx(
            specSet=[
                PC.FilterSpec(
                    objectSet=objectspecs,
                    propSet=[PC.PropertySpec(type=kind, all=False, pathSet=paths)],
                )
            ],
            options=PC.RetrieveOptions(maxObjects=500),
        )
        rows: list = []
        while result:
            token = result.token
            rows.extend(result.objects or [])
            if not token:
                break
            result = content.propertyCollector.ContinueRetrievePropertiesEx(token=token)
        if any(row.missingSet for row in rows):
            raise SafetyError("Incomplete VMware properties (permissions or inventory changed)")
        return rows
    finally:
        if token:
            content.propertyCollector.CancelRetrievePropertiesEx(token=token)
        if view:
            view.Destroy()


def values(row) -> dict:
    return {prop.name: prop.val for prop in row.propSet}


class VMware:
    def __init__(self, configs: list[VCenter]):
        self.configs = configs
        self.stack = ExitStack()
        self.sessions: dict = {}
        self.objects: dict = {}

    def __enter__(self):
        try:
            for config in self.configs:
                context = ssl.create_default_context(cafile=config.ca_file)
                si = SmartConnect(
                    host=config.hostname,
                    port=config.port,
                    user=config.username,
                    pwd=config.password,
                    sslContext=context,
                    httpConnectionTimeout=config.timeout,
                )
                self.stack.callback(Disconnect, si)
                content = si.RetrieveContent()
                observed = uuid(content.about.instanceUuid)
                if content.about.apiType != "VirtualCenter" or observed != config.expected_uuid:
                    raise SafetyError(
                        f"vCenter identity mismatch source={config.name} observed UUID={observed}"
                    )
                self.sessions[config.name] = (config, content)
            return self
        except SafetyError:
            self.stack.close()
            raise
        except Exception as exc:
            self.stack.close()
            raise SafetyError("vCenter connection/TLS/authentication failed") from exc

    def __exit__(self, *args):
        self.stack.close()

    def _vm(self, config, row, hosts) -> VM | None:
        p = values(row)
        if "config.template" not in p:
            raise SafetyError("VM configuration inaccessible")
        if p["config.template"] and config.ignore_templates:
            return None
        host = p.get("runtime.host")
        if not host or host._moId not in hosts:
            raise SafetyError("VM host unavailable or changed during inventory")
        parent = hosts[host._moId].get("parent")
        if not isinstance(parent, vim.ClusterComputeResource):
            raise SafetyError("Standalone ESXi inventory requires a separate source; excluded here")
        return VM(
            config.name,
            config.expected_uuid,
            uuid(p.get("config.instanceUuid")),
            uuid(p.get("config.uuid")),
            row.obj._moId,
            p["name"],
            parent._moId,
            host._moId,
        )

    def inventory(self) -> list[VM]:
        try:
            result = []
            for name, (config, content) in self.sessions.items():
                hosts = {
                    r.obj._moId: values(r)
                    for r in properties(content, vim.HostSystem, ["name", "parent"])
                }
                for row in properties(content, vim.VirtualMachine, VM_PATHS):
                    vm = self._vm(config, row, hosts)
                    if vm:
                        result.append(vm)
                        self.objects[(name, vm.moref)] = row.obj
            validate_inventory(result)
            return result
        except SafetyError:
            raise
        except Exception as exc:
            raise SafetyError("VMware inventory retrieval failed") from exc

    def refresh(self, vm: VM) -> VM:
        """Re-read identity and current placement just before a PATCH."""
        try:
            config, content = self.sessions[vm.source]
            rows = properties(
                content, vim.VirtualMachine, VM_PATHS, [self.objects[(vm.source, vm.moref)]]
            )
            if len(rows) != 1:
                raise SafetyError("VM disappeared during reconciliation")
            host = values(rows[0]).get("runtime.host")
            if not host:
                raise SafetyError("VM lost host during reconciliation")
            hosts = {
                r.obj._moId: values(r)
                for r in properties(content, vim.HostSystem, ["name", "parent"], [host])
            }
            current = self._vm(config, rows[0], hosts)
            if current is None:
                raise SafetyError("VM became an excluded template")
            return current
        except SafetyError:
            raise
        except Exception as exc:
            raise SafetyError("VMware pre-PATCH verification failed") from exc
