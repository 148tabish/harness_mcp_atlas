"""Unit tests for harness.finance (stdlib unittest; no network).

    python -m unittest tests.test_finance -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import finance  # noqa: E402

# the 0472db25 trial of 2026-10-28, the one reconciled against CloudWatch
RESULT = {
    "trial_name": "0472db25-4655-5c24-98bf-d805790a__W5ZHNDL",
    "started_at": "2026-10-28T13:25:55.746828Z",
    "finished_at": "2026-10-28T13:45:28.602655Z",
    "agent_info": {"name": "openhands-sdk", "model_info": {"name": "glm-5.3-bedrock", "provider": "anthropic"}},
    "agent_result": {"n_input_tokens": 5183353, "n_cache_tokens": 5079091, "n_output_tokens": 39444,
                     "cost_usd": 1.9681008719999997},
}
TRAJ = {"schema_version": "ATIF-v1.5", "steps": [],
        "final_metrics": {"total_prompt_tokens": 5183353, "total_completion_tokens": 39444,
                          "total_cached_tokens": 5079091, "total_cost_usd": 1.9681008719999997}}
JUDGE = {"ran": True, "model": "gpt-5.6-sol",
         "usage": {"input_tokens": 134263, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 90624,
                   "output_tokens": 2209, "cost_usd": 0.2549856,
                   "models": {"gpt-5.6-sol": {"input_tokens": 134263, "cache_creation_input_tokens": 0,
                                              "cache_read_input_tokens": 90624, "output_tokens": 2209,
                                              "cost_usd": 0.2549856}}}}
ENV = {"FINANCE_REPORT": "true", "FINANCE_REPORT_MODELS": "glm-5.3-bedrock",
       "ODOO_URL": "https://odoo.example", "FINANCE_PROJECT_ID": "PRJ-512",
       "FINANCE_SUBSCRIPTION_ID": "", "FINANCE_BUDGET_TYPE": "RFP", "FINANCE_RFP_SUB_TYPE": "Testing",
       "FINANCE_PRODUCTION_MODE": "", "FINANCE_PHASE_NUMBER": "1", "FINANCE_LEDGER": ""}


def make_trial(root: Path, name: str = "0472db25-4655-5c24-98bf-d805790a__W5ZHNDL",
               result=RESULT, traj=TRAJ, judge=JUDGE) -> Path:
    t = root / name
    (t / "agent").mkdir(parents=True)
    (t / "verifier").mkdir()
    (t / "config.json").write_text("{}")
    if result is not None:
        (t / "result.json").write_text(json.dumps(result))
    if traj is not None:
        (t / "agent" / "trajectory.json").write_text(json.dumps(traj))
    if judge is not None:
        (t / "verifier" / "judge.json").write_text(json.dumps(judge))
    return t


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = Path(self.tmp.name) / "0472db25-glm-5.3-bedrock-20261028-132552"
        self.job.mkdir()
        self.env = dict(ENV, FINANCE_LEDGER=str(Path(self.tmp.name) / "ledger.jsonl"))
        self.patch = mock.patch.dict(os.environ, self.env, clear=False)
        self.patch.start()
        finance._DOTENV = {}          # never read the repo's .env in tests

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    # ---- payload ----
    def test_payload_matches_the_reconciled_run(self):
        trial = make_trial(self.job)
        p = finance.build_payload(trial, "0472db25-4655-5c24-98bf-d805790a3e3e", "glm-5.3-bedrock")
        self.assertEqual(p["task_id"], "0472db25-4655-5c24-98bf-d805790a3e3e")
        self.assertEqual(p["trajectory_id"], "0472db25-4655-5c24-98bf-d805790a__W5ZHNDL")
        self.assertEqual(p["generated_at"], "2026-10-28T13:45:28+00:00")
        self.assertEqual(p["model_name"], "bedrock/glm-5.3")
        self.assertEqual(p["subscription_id"], "")
        self.assertEqual(p["trajectory_input_tokens"], 104262)        # prompt - cache reads
        self.assertEqual(p["trajectory_input_cache_tokens"], 5079091)
        self.assertEqual(p["trajectory_output_tokens"], 39444)
        self.assertEqual(p["trajectory_output_cache_tokens"], 0)
        self.assertEqual(p["trajectory_cache_write_tokens"], 0)
        self.assertAlmostEqual(p["trajectory_cost_usd"], 1.968100872, places=9)
        self.assertEqual(p["budget_type"], "RFP")
        self.assertEqual(p["rfp_sub_type"], "Testing")
        self.assertEqual(p["production_mode"], "")
        self.assertEqual(p["phase_number"], "1")
        self.assertIsInstance(p["phase_number"], str)
        self.assertNotIn("is_phase_based", p)
        self.assertEqual(p["judge_lines"], [{
            "model_name": "gpt-5.6-sol", "judge_input_tokens": 134263, "judge_output_tokens": 2209,
            "judge_input_cache_tokens": 90624, "judge_output_cache_tokens": 0,
            "judge_cache_write_tokens": 0, "judge_cost_usd": 0.2549856, "judge_turns": 0}])

    def test_no_judge_means_empty_judge_lines(self):
        trial = make_trial(self.job, judge={"ran": False, "reason": "no codex login"})
        self.assertEqual(finance.judge_lines(trial), [])

    def test_production_budget_fields(self):
        with mock.patch.dict(os.environ, {"FINANCE_BUDGET_TYPE": "Production",
                                          "FINANCE_PRODUCTION_MODE": "Multiphase"}):
            self.assertEqual(finance.budget_fields(),
                             {"budget_type": "Production", "rfp_sub_type": "", "production_mode": "Multiphase"})
        with mock.patch.dict(os.environ, {"FINANCE_BUDGET_TYPE": "Production", "FINANCE_PRODUCTION_MODE": ""}):
            with self.assertRaises(ValueError):
                finance.budget_fields()

    # ---- gate ----
    def test_gate_truth_table(self):
        self.assertEqual(finance.gate("glm-5.3-bedrock"), "")
        self.assertIn("not in FINANCE_REPORT_MODELS", finance.gate("opus-5"))
        with mock.patch.dict(os.environ, {"FINANCE_REPORT": "false"}):
            self.assertEqual(finance.gate("glm-5.3-bedrock"), "FINANCE_REPORT is off")
        with mock.patch.dict(os.environ, {"FINANCE_REPORT": "TRUE", "ODOO_URL": ""}):
            self.assertEqual(finance.gate("glm-5.3-bedrock"), "ODOO_URL is not set")
        with mock.patch.dict(os.environ, {"FINANCE_PROJECT_ID": ""}):
            self.assertEqual(finance.gate("glm-5.3-bedrock"), "FINANCE_PROJECT_ID is not set")

    def test_opus_never_posts_even_when_on(self):
        make_trial(self.job)
        with mock.patch.object(finance, "post") as fake:
            counts = finance.report_job(self.job, "task", "opus-5")
        fake.assert_not_called()
        self.assertEqual(counts, {"posted": 0, "skipped": 0, "failed": 0})

    def test_switch_off_posts_nothing(self):
        make_trial(self.job)
        with mock.patch.dict(os.environ, {"FINANCE_REPORT": "false"}), mock.patch.object(finance, "post") as fake:
            finance.report_job(self.job, "task", "glm-5.3-bedrock")
        fake.assert_not_called()

    # ---- posting ----
    def test_post_writes_receipt_and_ledger_then_skips(self):
        trial = make_trial(self.job)
        with mock.patch.object(finance, "post", return_value=(200, '{"message":"ok","data":{"id":1}}')) as fake:
            counts = finance.report_job(self.job, "task", "glm-5.3-bedrock")
            self.assertEqual(counts["posted"], 1)
            url, payload = fake.call_args[0]
            self.assertEqual(url, "https://odoo.example/api/v1/ethara_project/trajectory_usage/create")
            self.assertEqual(payload["model_name"], "bedrock/glm-5.3")
            receipt = json.loads((trial / finance.RECEIPT).read_text())
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["payload"]["trajectory_input_tokens"], 104262)
            ledger = [json.loads(l) for l in Path(self.env["FINANCE_LEDGER"]).read_text().splitlines()]
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger[0]["trajectory_id"], "0472db25-4655-5c24-98bf-d805790a__W5ZHNDL")
            # second pass: the receipt blocks a duplicate
            counts = finance.report_job(self.job, "task", "glm-5.3-bedrock")
            self.assertEqual(counts, {"posted": 0, "skipped": 1, "failed": 0})
            self.assertEqual(fake.call_count, 1)

    def test_failed_post_is_logged_not_raised_and_retried_later(self):
        trial = make_trial(self.job)
        with mock.patch.object(finance, "post", return_value=(0, "URLError: down")):
            counts = finance.report_job(self.job, "task", "glm-5.3-bedrock")
        self.assertEqual(counts["failed"], 1)
        self.assertFalse(json.loads((trial / finance.RECEIPT).read_text())["ok"])
        self.assertFalse(finance.already_reported(trial))       # a failed receipt does not block
        with mock.patch.object(finance, "post", return_value=(0, "down")):
            with self.assertRaises(finance.Failure):
                finance.report_job(self.job, "task", "glm-5.3-bedrock", strict=True)

    def test_unfinished_or_errored_trial_is_never_posted(self):
        make_trial(self.job, name="a__unfinished", result=None)
        make_trial(self.job, name="b__errored", result={"trial_name": "b", "agent_result": None,
                                                        "exception_info": {"exception_type": "AgentTimeoutError"}},
                   traj={"steps": []})
        with mock.patch.object(finance, "post") as fake:
            counts = finance.report_job(self.job, "task", "glm-5.3-bedrock")
        fake.assert_not_called()
        self.assertEqual(counts, {"posted": 0, "skipped": 2, "failed": 0})

    def test_dry_run_prints_payload_without_switch_or_url(self):
        make_trial(self.job)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"FINANCE_REPORT": "false", "ODOO_URL": ""}), \
             mock.patch.object(finance, "post") as fake, redirect_stdout(buf):
            counts = finance.report_job(self.job, "task", "glm-5.3-bedrock", dry_run=True)
        fake.assert_not_called()
        self.assertEqual(counts["posted"], 1)
        payload_text = buf.getvalue().split("\n[harness]")[0]      # the step's summary line follows
        self.assertEqual(json.loads(payload_text)["trajectory_input_tokens"], 104262)

    # ---- http retry policy ----
    def test_retry_only_on_5xx_429_and_network(self):
        def http_error(code):
            return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b"nope"))
        calls = []

        def opener_5xx_then_ok(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise http_error(503)
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False,
                                  status=200, read=lambda: b"ok")
        status, text = finance.post("https://x/y", {}, opener=opener_5xx_then_ok, sleep=lambda s: None)
        self.assertEqual((status, text, len(calls)), (200, "ok", 3))

        calls.clear()

        def opener_400(req, timeout):
            calls.append(1)
            raise http_error(400)
        status, _ = finance.post("https://x/y", {}, opener=opener_400, sleep=lambda s: None)
        self.assertEqual((status, len(calls)), (400, 1))            # no retry on other 4xx

        calls.clear()

        def opener_down(req, timeout):
            calls.append(1)
            raise OSError("connection refused")
        status, text = finance.post("https://x/y", {}, opener=opener_down, sleep=lambda s: None)
        self.assertEqual((status, len(calls)), (0, 3))
        self.assertIn("connection refused", text)


if __name__ == "__main__":
    unittest.main()
