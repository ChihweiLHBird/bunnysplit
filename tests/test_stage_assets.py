"""Regression tests for the explicit deployment artifact."""

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY_ASSETS = {
    "index.html",
    "styles.css",
    "main.py",
    "ui.py",
    "storage.py",
    "pyscript.toml",
    "sw.js",
    "manifest.webmanifest",
    "splitcore/__init__.py",
    "splitcore/model.py",
    "splitcore/calc.py",
    "icons/favicon.png",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "icons/icon-512-maskable.png",
    "icons/apple-touch-icon.png",
}


class StageAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        for relative in DEPLOY_ASSETS | {"scripts/stage-assets.sh"}:
            source = ROOT / relative
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def tearDown(self):
        self.temp.cleanup()

    def _stage(self, check=True):
        return subprocess.run(
            ["bash", "scripts/stage-assets.sh"],
            cwd=self.root,
            check=check,
            capture_output=True,
            text=True,
        )

    def _cache_version(self):
        text = (self.root / "dist/sw.js").read_text(encoding="utf-8")
        match = re.search(
            r"^const CACHE_VERSION = 'bunnysplit-([0-9a-f]{16})';$",
            text,
            re.MULTILINE,
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def test_only_manifested_files_are_staged(self):
        (self.root / ".venv").mkdir()
        (self.root / ".venv/secret.txt").write_text("secret", encoding="utf-8")
        (self.root / ".codex").mkdir()
        (self.root / ".codex/notes.txt").write_text("notes", encoding="utf-8")
        (self.root / "untracked.env").write_text("TOKEN=x", encoding="utf-8")

        self._stage()

        staged = {
            str(path.relative_to(self.root / "dist"))
            for path in (self.root / "dist").rglob("*")
            if path.is_file()
        }
        self.assertEqual(staged, DEPLOY_ASSETS)

    def test_cache_version_is_reproducible_and_content_derived(self):
        self._stage()
        first = self._cache_version()
        self._stage()
        self.assertEqual(self._cache_version(), first)

        with (self.root / "styles.css").open("a", encoding="utf-8") as stream:
            stream.write("\n/* changed */\n")
        self._stage()
        self.assertNotEqual(self._cache_version(), first)

    def test_pwa_manifest_starts_at_canonical_root(self):
        self._stage()

        manifest = json.loads(
            (self.root / "dist/manifest.webmanifest").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["start_url"], "./")
        self.assertEqual(manifest["scope"], "./")

    def test_missing_required_asset_fails_before_replacing_dist(self):
        self._stage()
        previous = (self.root / "dist/index.html").read_bytes()
        os.remove(self.root / "index.html")

        result = self._stage(check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing required deploy asset: index.html", result.stderr)
        self.assertEqual((self.root / "dist/index.html").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
