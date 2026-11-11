"""Safety guard for the Lucid loop: the new modules must never gain a path to NinjaTrader.

The launcher is the only automated POST path. Modules added for the feedback/promotion/approval work are scanned
(static) and the feedback export is run with `urllib.request.urlopen` booby-trapped (runtime). This file lands first
and must stay green in every later slice: add each new module to NEW_MODULES.
"""
import ast
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

NEW_MODULES = ["feedback", "readiness", "promote", "approvals"]  # modules that do not exist yet are skipped
FORBIDDEN_IMPORTS = ("urllib.request", "http.client", "http.server", "socket", "requests", "launcher", "server")


def _violations(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [mod] + [f"{mod}.{a.name}" for a in node.names]
            if any(a.name.startswith("enable_") for a in node.names) and mod == "handoff":
                bad.append(f"line {node.lineno}: imports handoff.enable_*")
        else:
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "handoff" and node.attr.startswith("enable_"):
                    bad.append(f"line {node.lineno}: references handoff.{node.attr}")
                if node.value.id == "launcher" and node.attr == "post":
                    bad.append(f"line {node.lineno}: references launcher.post")
            continue
        for n in names:
            if any(n == f or n.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
                bad.append(f"line {node.lineno}: imports {n}")
    return bad


class NoNewPostPathTests(unittest.TestCase):
    def test_new_modules_have_no_network_imports(self):
        checked = 0
        for name in NEW_MODULES:
            path = os.path.join(ROOT, name + ".py")
            if not os.path.isfile(path):
                continue
            checked += 1
            self.assertEqual(_violations(path), [], f"{name}.py gained a path to NinjaTrader")
        self.assertGreater(checked, 0, "no new module found to scan")

    def test_scanner_catches_violations(self):
        import tempfile
        src = "import urllib.request\nfrom handoff import enable_spec\nimport handoff\nhandoff.enable_requests\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            self.assertEqual(len(_violations(p)), 3)

    def test_urlopen_tripwire_is_armed_for_feedback_export(self):
        """Runs in test_feedback.py's end-to-end export too; here it proves the patch target exists."""
        import urllib.request
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network call")):
            with self.assertRaises(AssertionError):
                urllib.request.urlopen("http://127.0.0.1:8765/status")


if __name__ == "__main__":
    unittest.main()
