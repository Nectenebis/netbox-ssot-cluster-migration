import logging


def configure(verbose: bool = False) -> None:
    # Never enable transport DEBUG logging (it can expose authentication headers).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", force=True)
    logging.getLogger("netbox_vm_reconcile").setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in ("requests", "urllib3", "pynetbox", "pyVim", "pyVmomi"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
