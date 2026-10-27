"""Repository hygiene checks for the public tree. Standard library only.

Run from the repository root:  python -B -m unittest discover -s tests -v
(In the development repository the same tree is the folder public/.)
"""

import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
TEXT_SUFFIXES = {".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".ps1", ".svg", ".py"}
EM_DASH = b"\xe2\x80\x94"


def files(suffixes=None):
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if set(path.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        if suffixes is None or path.suffix.lower() in suffixes or path.name in suffixes:
            yield path


def rel(path):
    return path.relative_to(ROOT).as_posix()


class HygieneTests(unittest.TestCase):
    def test_src_files_match_sha256sums(self):
        listed = {}
        for line in (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                listed[parts[1].strip().lstrip("*")] = parts[0].lower()
        self.assertTrue(listed, "SHA256SUMS lists no files")
        src = {p.name: p for p in (ROOT / "src").iterdir() if p.is_file()}
        self.assertEqual(sorted(src), sorted(listed), "src/ and SHA256SUMS list different files")
        for name, path in src.items():
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(actual, listed[name], f"src/{name} does not match SHA256SUMS")

    def test_powershell_scripts_are_pure_ascii(self):
        # Windows PowerShell 5.1 reads a file without a byte order mark in the local code page.
        scripts = list(files({".ps1"}))
        self.assertTrue(scripts, "no .ps1 files found")
        for path in scripts:
            data = path.read_bytes()
            self.assertTrue(data.isascii(), f"{rel(path)} contains a non-ASCII byte")

    def test_no_em_dash_in_text_files(self):
        # src/ is excluded: those files are frozen and covered by SHA256SUMS, and some contain the character.
        offenders = [
            rel(p) for p in files(TEXT_SUFFIXES)
            if not rel(p).startswith("src/") and EM_DASH in p.read_bytes()
        ]
        self.assertEqual(offenders, [])

    def test_requirements_pin_every_package_with_double_equals(self):
        pin = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9][A-Za-z0-9.!+_-]*$")
        lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        entries = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        self.assertTrue(entries, "requirements.txt has no packages")
        unpinned = [ln for ln in entries if not pin.match(ln)]
        self.assertEqual(unpinned, [])

    def test_readme_names_the_first_changelog_version(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        match = re.search(r"^##\s+v?(\d+\.\d+\.\d+)", changelog, re.MULTILINE)
        self.assertIsNotNone(match, "CHANGELOG.md has no '## X.Y.Z' heading")
        version = match.group(1)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, r"(?<![\d.])" + re.escape(version) + r"(?![\d.]*\d)", f"README.md does not name {version}")


if __name__ == "__main__":
    unittest.main()
