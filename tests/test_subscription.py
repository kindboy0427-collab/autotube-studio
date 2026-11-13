"""Claude Pro (Claude Code) and ChatGPT Plus (Codex) as script writers - CLIs faked."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from autotube import db, llm, subscription
from tests.base import TempHome


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class SubscriptionTest(TempHome):
    def test_claude_code_call_shape(self):
        seen = {}

        def runner(args, input=None, cwd=None, env=None, **kw):
            seen.update(args=args, input=input, env=env)
            return Proc(0, json.dumps({"type": "result", "is_error": False, "result": '{"ok": 1}'}))
        with patch("autotube.subscription._bin", return_value="/usr/bin/claude"), \
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-should-not-leak"}):
            out = subscription.run_claude_code("대본 써줘", "작가", runner=runner)
        self.assertEqual(out, '{"ok": 1}')
        self.assertIn("-p", seen["args"])
        self.assertEqual(seen["args"][seen["args"].index("--output-format") + 1], "json")
        self.assertEqual(seen["input"], "대본 써줘")
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])  # bills the Pro login, not an API key
        self.assertEqual(db.one("SELECT service, cost_usd FROM usage")["cost_usd"], 0.0)

    def test_codex_call_shape_and_output_file(self):
        seen = {}

        def runner(args, input=None, cwd=None, env=None, **kw):
            seen.update(args=args, input=input, env=env)
            Path(args[args.index("-o") + 1]).write_text('{"plan": true}', encoding="utf-8")
            return Proc(0, "progress...")
        with patch("autotube.subscription._bin", return_value="codex"), patch.dict("os.environ", {"OPENAI_API_KEY": "sk-x"}):
            out = subscription.run_codex("기획", "PD", runner=runner)
        self.assertEqual(out, '{"plan": true}')
        a = seen["args"]
        self.assertEqual(a[:2], ["codex", "exec"])
        self.assertIn("--skip-git-repo-check", a)
        self.assertEqual(a[a.index("--sandbox") + 1], "read-only")
        self.assertEqual(a[-1], "-")
        self.assertIn("[역할]\nPD", seen["input"])
        self.assertNotIn("OPENAI_API_KEY", seen["env"])

    def test_limit_pauses_provider_and_falls_back(self):
        calls = []

        def fake_claude(prompt, system, pid):
            calls.append("claude")
            raise subscription.SubscriptionLimit("limit")

        def fake_codex(prompt, system, pid):
            calls.append("codex")
            return '{"title": "ok"}'
        with patch("autotube.subscription._bin", side_effect=lambda n: n), \
                patch("autotube.subscription.run_claude_code", side_effect=fake_claude), \
                patch("autotube.subscription.run_codex", side_effect=fake_codex):
            self.assertEqual(llm.order()[:2], ["claude_code", "codex"])
            self.assertEqual(llm.generate_json("p"), {"title": "ok"})
            self.assertEqual(calls, ["claude", "codex"])

    def test_limit_text_sets_cooldown(self):
        def runner(args, **kw):
            return Proc(1, "", "Claude usage limit reached. Your limit will reset at 5pm")
        with patch("autotube.subscription._bin", return_value="claude"):
            with self.assertRaises(subscription.SubscriptionLimit):
                subscription.run_claude_code("x", runner=runner)
            self.assertFalse(subscription.available("claude_code"))   # paused for an hour
            self.assertTrue(subscription.available("codex"))
            self.assertNotIn("claude_code", llm.order())

    def test_broken_json_moves_to_next_provider(self):
        with patch("autotube.subscription._bin", side_effect=lambda n: n), \
                patch("autotube.subscription.run_claude_code", return_value="죄송하지만 JSON이 아닙니다"), \
                patch("autotube.subscription.run_codex", return_value='{"a": 2}'):
            self.assertEqual(llm.generate_json("p"), {"a": 2})
