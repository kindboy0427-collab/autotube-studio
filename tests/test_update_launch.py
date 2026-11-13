"""v1.4: self-update from GitHub and the desktop launcher."""
import io
import zipfile
from pathlib import Path
from unittest.mock import patch

from autotube import config, launch, updater
from tests.base import TempHome


def fake_repo_zip(version, extra=b"", broken=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        top = "autotube-studio-main/"
        z.writestr(top + "VERSION", version)
        z.writestr(top + "requirements.txt", "fastapi\n")
        z.writestr(top + "autotube/__init__.py", "")
        z.writestr(top + "autotube/web.py", "raise ImportError('boom')\n" if broken else "X=1\n")
        z.writestr(top + "autotube/launch.py", "Y=2\n")
        z.writestr(top + "autotube/new_module.py", "Z=3\n" + extra.decode())
    return buf.getvalue()


class Updater(TempHome):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp) / "app"
        (self.root / "autotube").mkdir(parents=True)
        (self.root / "VERSION").write_text("1.4.0")
        (self.root / "requirements.txt").write_text("fastapi\n")
        (self.root / "autotube" / "__init__.py").write_text("")
        (self.root / "autotube" / "web.py").write_text("OLD=1\n")
        (self.root / "autotube" / "launch.py").write_text("OLD=1\n")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "keep.txt").write_text("venv")
        self.p = patch.object(config, "ROOT", self.root)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def _get(self, remote_version, zip_bytes):
        def get(url, timeout=30, accept=None):
            return remote_version.encode() if "VERSION" in url else zip_bytes
        return get

    def test_version_compare(self):
        self.assertGreater(updater._vt("1.10.0"), updater._vt("1.9.9"))
        self.assertEqual(updater._vt("v1.4"), updater._vt("1.4.0"))

    def test_apply_replaces_program_keeps_data_and_venv(self):
        (config.DATA / "secrets.json").write_text('{"GEMINI_API_KEY":"k"}')
        with patch.object(updater, "_get", self._get("1.5.0", fake_repo_zip("1.5.0"))):
            res = updater.apply(lambda m: None)
        self.assertTrue(res["updated"])
        self.assertEqual(updater.local_version(), "1.5.0")
        self.assertTrue((self.root / "autotube" / "new_module.py").exists())
        self.assertEqual((self.root / ".venv" / "keep.txt").read_text(), "venv")
        self.assertIn("GEMINI_API_KEY", (config.DATA / "secrets.json").read_text())
        self.assertTrue((config.DATA / "backup_prev" / "autotube" / "web.py").exists())

    def test_no_update_when_same_version(self):
        with patch.object(updater, "_get", self._get("1.4.0", b"")):
            self.assertFalse(updater.apply()["updated"])

    def test_broken_release_rolls_back(self):
        with patch.object(updater, "_get", self._get("1.6.0", fake_repo_zip("1.6.0", broken=True))):
            with self.assertRaises(RuntimeError):
                updater.apply(lambda m: None)
        self.assertEqual(updater.local_version(), "1.4.0")
        self.assertEqual((self.root / "autotube" / "web.py").read_text(), "OLD=1\n")

    def test_private_repo_uses_token_and_api(self):
        config.set_secrets({"GITHUB_TOKEN": "t"})
        urls = []
        with patch("urllib.request.urlopen", side_effect=lambda req, timeout=0: urls.append(req) or io.BytesIO(b"9.9.9")):
            self.assertEqual(updater.remote_version(), "9.9.9")
        self.assertIn("api.github.com/repos/kindboy0427-collab/autotube-studio/contents/VERSION", urls[0].full_url)
        self.assertEqual(urls[0].headers["Authorization"], "Bearer t")


class Launcher(TempHome):
    def test_opens_app_window_with_own_profile(self):
        calls = []
        with patch.object(launch, "_browser", return_value=r"C:\Edge\msedge.exe"), \
                patch("subprocess.Popen", side_effect=lambda args, **k: calls.append(args)):
            launch.open_window()
        self.assertTrue(any(a.startswith("--app=http://127.0.0.1:8770") for a in calls[0]))
        self.assertTrue(any("window_profile" in a for a in calls[0]))

    def test_main_reuses_running_engine(self):
        with patch.object(launch, "running", return_value=True), patch.object(launch, "start_engine") as se, \
                patch.object(launch, "open_window") as ow, patch.object(launch, "try_update") as tu:
            launch.main([])
        se.assert_not_called()
        tu.assert_not_called()
        ow.assert_called_once()

    def test_background_starts_engine_without_window(self):
        with patch.object(launch, "running", return_value=False), patch.object(launch, "start_engine", return_value=True), \
                patch.object(launch, "open_window") as ow, patch.object(launch, "try_update") as tu:
            launch.main(["--background"])
        tu.assert_called_once()
        ow.assert_not_called()
