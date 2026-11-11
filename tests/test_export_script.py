"""scripts/export_feedback.py: exports every sim/live version that has a lucid_id, idempotently."""
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
import feedback_fixture as fx  # noqa: E402

spec = importlib.util.spec_from_file_location("export_feedback_script", os.path.join(ROOT, "scripts", "export_feedback.py"))
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class ExportScriptTests(unittest.TestCase):
    def test_export_all_writes_then_is_unchanged(self):
        rcon, jcon = fx.build("ready")
        self.addCleanup(rcon.close)
        self.addCleanup(jcon.close)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {"NT_AUDIT_FILE": os.path.join(d, "a.jsonl")}), mock.patch(
                "urllib.request.urlopen", side_effect=AssertionError("network call")):
            kw = dict(out_dir=os.path.join(d, "fb"), alias_file=os.path.join(d, "al.json"), as_of=fx.AS_OF,
                      operator_commit=None)
            first = script.export_all(rcon, jcon, **kw)
            second = script.export_all(rcon, jcon, **kw)
        self.assertEqual([r["status"] for r in first], ["written"])
        self.assertEqual([r["status"] for r in second], ["unchanged"])

    def test_candidates_are_not_exported(self):
        rcon, jcon = fx.build("ready")
        self.addCleanup(rcon.close)
        self.addCleanup(jcon.close)
        rcon.execute("UPDATE specs SET status='candidate'")
        rcon.commit()
        self.assertEqual(script.export_all(rcon, jcon), [])


if __name__ == "__main__":
    unittest.main()
