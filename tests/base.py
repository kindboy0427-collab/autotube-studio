import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotube import config, db


class TempHome(unittest.TestCase):
    """Every test gets its own data/output folders and empty database."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autotube_test_")
        root = Path(self.tmp)
        self._patches = [patch.object(config, "DATA", root / "data"), patch.object(config, "OUTPUT", root / "output"),
                         patch.object(config, "ASSETS", root / "assets"),
                         patch.dict(os.environ, {k: "" for k in config.SECRET_KEYS}),
                         # never call real Claude Code / Codex CLIs from tests
                         patch("autotube.subscription._bin", return_value=None)]
        for p in self._patches:
            p.start()
        config.ensure_dirs()
        db.init()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
