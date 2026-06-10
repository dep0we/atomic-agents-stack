"""Shared test helper: simulate a machine without the [gcp] extra installed.

Blocking ``from google.cloud import secretmanager`` is subtle: once the SDK has
been imported anywhere in the test session, ``google.cloud.secretmanager`` is
bound as an attribute on the parent ``google.cloud`` package, and the from-import
resolves via that attribute WITHOUT consulting ``sys.meta_path`` or even
``sys.modules`` (a ``None`` entry alone does not help). To realistically simulate
SDK-absence we must, for the duration of the block: (1) remove the parent-package
attribute, (2) drop the ``sys.modules`` entry, and (3) install a meta-path finder
that raises ImportError for the submodule. All three are restored on exit.

Used by the SDK-absent tests in test_secret_backend_gcp.py,
test_secret_backend_filesystem.py, and test_doctor_gcp_secret_backend.py. No live
GCP is touched (build constraint noLiveGcp).
"""

from __future__ import annotations

import contextlib
import importlib.abc
import sys

_SDK = "google.cloud.secretmanager"


class _BlockSecretManager(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == _SDK or name.startswith(_SDK + "."):
            raise ImportError("simulated: google-cloud-secret-manager not installed")
        return None


@contextlib.contextmanager
def block_gcp_sdk():
    """Context manager that makes ``from google.cloud import secretmanager`` raise
    ImportError, mimicking a machine without the [gcp] extra installed."""
    saved_mod = sys.modules.pop(_SDK, None)

    parent = sys.modules.get("google.cloud")
    had_attr = parent is not None and hasattr(parent, "secretmanager")
    saved_attr = getattr(parent, "secretmanager", None) if parent is not None else None
    if had_attr:
        delattr(parent, "secretmanager")

    blocker = _BlockSecretManager()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(blocker)
        if saved_mod is not None:
            sys.modules[_SDK] = saved_mod
        if had_attr:
            setattr(parent, "secretmanager", saved_attr)
