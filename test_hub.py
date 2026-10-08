import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import hub


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state_patch = patch.object(hub, "STATE", self.root / "state")
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.temp.cleanup()

    def test_paid_or_unknown_prices_are_rejected(self):
        metadata = {"tool_call": True, "cost": {"input": 0, "output": 0, "cache_read": 0}}
        self.assertTrue(hub.zero_cost(metadata))
        for cost in ({"input": 0}, {"input": 0, "output": 1}, {"input": "0", "output": 0},
                     {"input": False, "output": 0}, {"input": 0, "output": 0, "request": 1}):
            self.assertFalse(hub.zero_cost({"tool_call": True, "cost": cost}))

    def test_catalog_is_allowlisted_not_arbitrary_free_names(self):
        metadata = {"tool_call": True, "cost": {"input": 0, "output": 0}}
        catalog = {"opencode": {"models": {"untrusted-free": metadata, "space-bunny-free": metadata,
                                            "big-pickle": {"tool_call": True, "cost": {"input": 1, "output": 0}}}}}
        self.assertEqual(hub.free_models(catalog), ["space-bunny-free"])

    def test_unknown_online_prices_do_not_use_cached_prices(self):
        with patch.object(hub, "executable", return_value="fake"), \
             patch.object(hub, "refresh_free_models", side_effect=OSError("offline")), \
             patch.object(hub, "local_models", return_value=[hub.LOCAL_AGENT_MODEL]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub.candidates("auto", "fast"), [
                ("antigravity", hub.AGY_MODELS["fast"]), ("local", hub.LOCAL_AGENT_MODEL)])

    def test_main_and_helper_models_are_free_and_same(self):
        env = hub.clean_environment("free", "space-bunny-free", True)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["model"], config["small_model"])
        self.assertEqual(config["enabled_providers"], ["opencode"])
        self.assertEqual(config["share"], "disabled")
        self.assertEqual(config["agent"]["build"]["model"], config["model"])
        for name in ("title", "summary", "compaction"):
            self.assertEqual(config["agent"][name]["model"], config["model"])

    def test_local_endpoint_and_inherited_cloud_keys(self):
        with patch.dict(os.environ, {"OLLAMA_HOST": "https://paid.example", "OLLAMA_API_KEY": "secret"}):
            env = hub.clean_environment("local", hub.LOCAL_AGENT_MODEL, True)
        self.assertNotIn("OLLAMA_API_KEY", env)
        self.assertEqual(env["OPENCODE_DISABLE_MODELS_FETCH"], "true")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["enabled_providers"], ["ollama"])
        self.assertTrue(config["agent"]["title"]["disable"])
        self.assertTrue(config["agent"]["summary"]["disable"])
        self.assertEqual(config["provider"]["ollama"]["models"][hub.LOCAL_AGENT_MODEL]["options"]["reasoningEffort"], "none")
        self.assertEqual(config["provider"]["ollama"]["options"]["baseURL"], "http://127.0.0.1:11434/v1")

    def test_antigravity_does_not_use_inherited_paid_api_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret", "GOOGLE_API_KEY": "secret"}):
            env = hub.clean_environment("antigravity")
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("GOOGLE_API_KEY", env)

    def test_linux_ssh_uses_native_same_user_keyring(self):
        with patch.dict(os.environ, {"SSH_CONNECTION": "remote", "SSH_CLIENT": "remote"}), \
             patch.object(hub.sys, "platform", "linux"), patch.object(hub.os, "getuid", return_value=1000), \
             patch.object(Path, "exists", return_value=True):
            env = hub.clean_environment("antigravity")
        self.assertNotIn("SSH_CONNECTION", env)
        self.assertNotIn("SSH_CLIENT", env)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", env)

    def test_read_only_denies_writes_and_shell(self):
        config = json.loads(hub.clean_environment("local", hub.LOCAL_AGENT_MODEL)["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(config["permission"]["bash"], "deny")
        with patch.object(hub, "executable", return_value="agy"):
            cmd = hub.command("antigravity", "model", "task")
        self.assertIn("plan", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_prompt_is_argument_not_shell_code(self):
        prompt = '$(touch SHOULD_NOT_EXIST); `id` "quoted"\nnext line'
        with patch.object(hub, "executable", return_value="opencode"):
            cmd = hub.command("free", "space-bunny-free", prompt)
        self.assertEqual(cmd[-1], prompt)
        self.assertEqual(cmd[-2], "--")

    def test_zero_exit_structured_provider_error_is_failure(self):
        text, failed = hub.event_text(json.dumps({"type": "error", "error": {"message": "429 rate limit"}}))
        self.assertEqual(hub.classify_failure(0, text, failed), "quota")

    def test_canceled_status_stops_even_with_generic_exit_code(self):
        text, failed = hub.event_text('{"status":"CANCELED","response":""}')
        self.assertEqual(hub.classify_failure(1, text, failed), "canceled")

    def test_lock_preserves_existing_project(self):
        with hub.project_lock(self.project):
            with self.assertRaises(RuntimeError):
                with hub.project_lock(self.project):
                    pass
        self.assertEqual(list((hub.STATE / "locks").glob("*")), [])

    def test_fallback_carries_partial_work_and_original_request(self):
        (self.project / "user.txt").write_text("preserve me")
        prompts = []

        def process(args, project, env, log):
            prompts.append(args[-1])
            if len(prompts) == 1:
                (project / "partial.txt").write_text("work in progress")
                return 1, "429 quota exhausted after writing partial.txt", True
            self.assertTrue((project / "partial.txt").exists())
            return 0, "done", False

        with patch.object(hub, "route_options", return_value=[("free", "space-bunny-free"), ("local", hub.LOCAL_AGENT_MODEL)]), \
             patch.object(hub, "executable", return_value="fake"), patch.object(hub, "run_process", side_effect=process), \
             contextlib.redirect_stdout(io.StringIO()):
            result = hub.run_task(self.project, "Build a parser", apply=True)
        self.assertEqual(result, 0)
        self.assertIn("Build a parser", prompts[1])
        self.assertIn("partial.txt", prompts[1])
        self.assertEqual((self.project / "user.txt").read_text(), "preserve me")

    def test_cancel_does_not_start_fallback(self):
        with patch.object(hub, "route_options", return_value=[("free", "space-bunny-free"), ("local", hub.LOCAL_AGENT_MODEL)]), \
             patch.object(hub, "executable", return_value="fake"), \
             patch.object(hub, "run_process", return_value=(130, "", False)) as process, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub.run_task(self.project, "task"), 130)
        self.assertEqual(process.call_count, 1)

    def test_real_subprocess_preserves_literal_prompt(self):
        script = "import sys,json; print(json.dumps({'type':'text','part':{'text':sys.argv[1]}}))"
        prompt = "$(touch SHOULD_NOT_EXIST) `id`"
        with contextlib.redirect_stdout(io.StringIO()):
            code, output, error = hub.run_process([sys.executable, "-c", script, prompt], self.project,
                                                os.environ.copy(), self.root / "log.txt", 5)
        self.assertEqual(code, 0)
        self.assertEqual(output, prompt)
        self.assertFalse(error)
        self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())

    def test_explicit_project_and_pwd_match_actual_directory(self):
        with patch.object(hub, "executable", return_value="opencode"):
            cmd = hub.command("local", hub.LOCAL_AGENT_MODEL, "task", project=self.project)
        self.assertEqual(cmd[cmd.index("--dir") + 1], str(self.project))
        script = "import os; print(os.getcwd()); print(os.environ['PWD'])"
        with contextlib.redirect_stdout(io.StringIO()):
            code, output, _ = hub.run_process([sys.executable, "-c", script], self.project,
                dict(os.environ, PWD="/wrong/project"), self.root / "cwd.log", 5)
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), [str(self.project.resolve()), str(self.project.resolve())])

    def test_timeout_kills_process_and_does_not_hang(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code, _, _ = hub.run_process([sys.executable, "-c", "import time;time.sleep(30)"], self.project,
                                        os.environ.copy(), self.root / "log.txt", 1)
        self.assertEqual(code, 124)


if __name__ == "__main__":
    unittest.main()
