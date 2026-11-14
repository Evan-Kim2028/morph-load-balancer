import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agentq import run_worker


class RunWorkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text("do it\n", encoding="utf-8")
        self.run_dir = self.tmp / "run"
        self.env = {"AGENTQ_HOME": str(self.home)}

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_worker.main(list(args), env=self.env)
        return code, out.getvalue()

    def base(self, tier="pi-deepseek-flash", label="exec-one"):
        return [tier, label, str(self.workspace), str(self.prompt), str(self.run_dir), "0", "--no-preflight"]

    def test_unknown_tier_is_refused_with_a_status_line(self):
        code, out = self.call(*self.base(tier="pi-nope"))
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "STATUS: failed reason=unknown-tier")

    def test_a_label_outside_the_launch_rule_is_refused(self):
        code, out = self.call(*self.base(label="has space"))
        self.assertEqual(code, 1)
        self.assertIn("bad-label", out)

    def test_a_missing_workspace_is_refused(self):
        argv = self.base()
        argv[2] = str(self.tmp / "absent")
        code, out = self.call(*argv)
        self.assertEqual(code, 1)
        self.assertIn("no-workspace", out)

    def test_a_launch_starts_the_pi_launcher_and_writes_the_pid(self):
        with mock.patch.object(run_worker.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            code, out = self.call(*self.base())
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("STATUS: running out="))
        cmd = popen.call_args.args[0]
        self.assertIn("agentq.launch_pi", cmd)
        self.assertIn("--tier", cmd)
        self.assertIn(run_worker.DEFAULT_MODELS["pi-deepseek-flash"], cmd)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual((self.run_dir / "exec-one.pid").read_text().strip(), "4242")

    def test_broker_json_models_override_the_defaults(self):
        (self.home / "broker.json").write_text(json.dumps({"models": {"pi-kimi": "morph/custom-kimi"}}), encoding="utf-8")
        self.assertEqual(run_worker.models_for(self.home)["pi-kimi"], "morph/custom-kimi")
        self.assertEqual(run_worker.models_for(self.home)["pi-deepseek-flash"],
                         run_worker.DEFAULT_MODELS["pi-deepseek-flash"])


if __name__ == "__main__":
    unittest.main()
