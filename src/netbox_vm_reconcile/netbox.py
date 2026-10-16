"""pynetbox transport. The only write operation exposed is a restricted VM PATCH."""

import pynetbox
import requests
from pynetbox.core.query import Request

from .config import Config
from .identity import SafetyError

ALLOWED = {"name", "cluster", "device", "site"}


class Session(requests.Session):
    def __init__(self, config: Config):
        super().__init__()
        self.timeout = config.timeout
        self.verify = config.ca_file or True
        self.origin = config.url.rstrip("/") + "/api/"

    def request(self, method, url, *args, **kwargs):
        if not url.startswith(self.origin):
            raise SafetyError("NetBox request/pagination URL escaped configured API origin")
        kwargs["timeout"] = self.timeout
        kwargs["allow_redirects"] = False
        response = super().request(method, url, *args, **kwargs)
        if 300 <= response.status_code < 400:
            raise SafetyError("Unexpected NetBox redirect; configure canonical HTTPS URL")
        return response


def safe_call(operation):
    try:
        return operation()
    except SafetyError:
        raise
    except Exception as exc:
        # API bodies may contain user-controlled data; don't log tokens, URLs, or bodies.
        response = getattr(exc, "req", None)
        status = getattr(response, "status_code", None)
        raise SafetyError(f"NetBox API operation failed (HTTP {status or 'unavailable'})") from exc


class NetBox:
    def __init__(self, config: Config):
        self.api = pynetbox.api(config.url, token=config.token, threading=False)
        self.api.http_session = Session(config)
        self.endpoint = self.api.virtualization.virtual_machines

    def close(self):
        self.api.http_session.close()

    def snapshot(self) -> tuple[list[dict], dict[int, dict], dict[int, dict]]:
        return safe_call(
            lambda: (
                [dict(r) for r in self.endpoint.all()],
                {r.id: dict(r) for r in self.api.virtualization.clusters.all()},
                {r.id: dict(r) for r in self.api.dcim.devices.all()},
            )
        )

    def get_vm(self, pk: int) -> dict:
        record = safe_call(lambda: self.endpoint.get(pk))
        if record is None:
            raise SafetyError(f"Previously mapped NetBox VM ID {pk} is missing")
        return dict(record)

    def named(self, name: str) -> list[dict]:
        return safe_call(lambda: [dict(r) for r in self.endpoint.filter(name__ie=name)])

    def placement(self, cluster_id: int, device_id: int) -> tuple[dict, dict]:
        def read():
            cluster = self.api.virtualization.clusters.get(cluster_id)
            device = self.api.dcim.devices.get(device_id)
            if not cluster or not device:
                raise SafetyError("Target Cluster or Device disappeared")
            return dict(cluster), dict(device)

        return safe_call(read)

    def patch_vm(self, pk: int, changes: dict) -> None:
        if not changes or set(changes) - ALLOWED:
            raise SafetyError("Forbidden or empty NetBox PATCH")
        safe_call(
            lambda: Request(
                base=self.endpoint.url,
                key=pk,
                token=self.api.token,
                http_session=self.api.http_session,
            ).patch(changes)
        )
