"""Test package initialization for local_dev_mcp test suite.

Installs global automatic cleanup for unittest.TestCase:
- Closes any leaked durable state SQLite connections via ``store.close_all_state_dbs()``
- Cleans up any leftover temporary directory attributes (e.g. ``self.temporary``,
  ``self.tmp``, ``self._tmp``, ``self._temp_dirs``)
"""

from __future__ import annotations

import tempfile
import unittest
from typing import Any


def _auto_test_cleanup(test_case: unittest.TestCase) -> None:
    for attr in ("server", "_server"):
        srv = getattr(test_case, attr, None)
        if srv is not None and hasattr(srv, "close") and callable(srv.close):
            try:
                srv.close()
            except Exception:
                pass

    # Always close all open SQLite databases first so no file descriptors hold locks
    try:
        from local_dev_mcp import store

        store.close_all_state_dbs()
    except Exception:
        pass

    # Clean up common temporary directory attributes if left open
    for attr in ("temporary", "tmp", "_tmp"):
        val = getattr(test_case, attr, None)
        if isinstance(val, tempfile.TemporaryDirectory):
            try:
                val.cleanup()
            except Exception:
                pass

    for td in list(getattr(test_case, "_temp_dirs", [])):
        if isinstance(td, tempfile.TemporaryDirectory):
            try:
                td.cleanup()
            except Exception:
                pass


_orig_testcase_init = unittest.TestCase.__init__


def _patched_testcase_init(self: Any, *args: Any, **kwargs: Any) -> None:
    _orig_testcase_init(self, *args, **kwargs)
    if not hasattr(self, "_mcp_auto_cleanup_installed"):
        self.addCleanup(_auto_test_cleanup, self)
        self._mcp_auto_cleanup_installed = True


unittest.TestCase.__init__ = _patched_testcase_init  # type: ignore[method-assign]
