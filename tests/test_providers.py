"""Direct providers: offline only. No request leaves the machine and no key is real."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from adpbench.problem import load_problem, repo_root
from scripts import go_agent as agent
from scripts.go_pilot import (
    billable_tokens,
    call_model,
    generation_settings,
    read_plan,
    redact_credentials,
    slot_dir,
    validate_plan,
)
from scripts.go_schedule import dispatch_inputs, slot_outputs
from scripts.go_stream import call_streaming, read_stream
from scripts.providers import (
    GO,
    KEY_ENVS,
    PROVIDERS,
    auth_headers,
    check_billing,
    provider_of,
    run_label,
    source_name,
)
from scripts.publish_agent import PRIVATE_FIELDS, validate_generation
from scripts.publish_results import register

ROOT = repo_root()
PROBLEM = "005_argmax"
FAMILIES = ("fable51", "fable52", "direct")
# Everything two plans must share to be the same experiment.
PROTOCOL_FIELDS = (
    "protocol", "problems", "max_turns", "max_checks", "slot_timeout_s", "check_timeout_s",
    "max_context_bytes", "transport_retries", "request_timeout_s", "request_wall_timeout_s",
    "max_prompt_bytes", "stream", "output_budget", "release_on_quota",
)


def plan_of(family: str, attempt: int = 1) -> dict:
    return read_plan(ROOT / f"pilot/go-agent-v2-{family}-r{attempt}.json")


def sse(*events) -> bytes:
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


def tool_reply(name: str, args: dict, identifier: str) -> dict:
    """One Anthropic messages turn: an (empty, signed) thinking block, then a tool call."""
    return {
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-" + identifier},
            {"type": "tool_use", "id": identifier, "name": name, "input": args},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50,
                  "output_tokens": 40},
    }


class RegistryTest(unittest.TestCase):
    def test_a_model_without_a_provider_is_opencode_go(self):
        self.assertIs(provider_of({"id": "kimi-k3"}), GO)
        self.assertEqual((GO.host, GO.path, GO.key_env), ("opencode.ai", "/zen/go/v1/", "OPENCODE_GO_API_KEY"))
        self.assertEqual(slot_dir(Path("out"), {"id": "m"}, "p", 3), Path("out/opencode-go-m/p/rep3"))
        self.assertEqual(run_label({"id": "m"}, "agent-assisted-v2"), "opencode-go/m [agent-assisted-v2]")
        self.assertEqual(generation_settings({"id": "m", "api": "messages", "thinking": {"type": "enabled"}}),
                         {"thinking": {"type": "enabled"}})

    def test_hosts_and_keys_are_fixed_per_provider(self):
        self.assertEqual({p.id: (p.host, p.key_env) for p in PROVIDERS.values()}, {
            "opencode-go": ("opencode.ai", "OPENCODE_GO_API_KEY"),
            "anthropic": ("api.anthropic.com", "ANTHROPIC_API_KEY"),
            "openai": ("api.openai.com", "OPENAI_API_KEY"),
            "xai": ("api.x.ai", "XAI_API_KEY"),
            "moonshot": ("api.moonshot.ai", "MOONSHOT_API_KEY"),
        })
        self.assertEqual(len(set(KEY_ENVS)), len(PROVIDERS))
        with self.assertRaises(ValueError):
            provider_of({"id": "m", "provider": "https://evil.example"})
        with self.assertRaises(ValueError):
            provider_of({"id": "m", "provider": {"host": "evil.example"}})

    def test_each_provider_gets_only_its_own_auth_headers(self):
        anthropic = auth_headers(PROVIDERS["anthropic"], "messages", "k", "s")
        self.assertEqual(anthropic, {"x-api-key": "k", "anthropic-version": "2023-06-01"})
        self.assertEqual(auth_headers(PROVIDERS["openai"], "responses", "k", "s"), {"Authorization": "Bearer k"})
        self.assertEqual(auth_headers(PROVIDERS["moonshot"], "chat/completions", "k", "s"), {"Authorization": "Bearer k"})
        go = auth_headers(GO, "messages", "k", "s")
        self.assertEqual(go, {"Authorization": "Bearer k", "x-api-key": "k", "anthropic-version": "2023-06-01",
                              "x-opencode-session": "s"})

    def test_results_are_filed_and_labelled_by_provider(self):
        model = plan_of("fable51")["models"][0]
        self.assertEqual(slot_dir(Path("out"), model, PROBLEM, 2), Path("out/anthropic-claude-fable-5-1/005_argmax/rep2"))
        self.assertEqual(run_label(model, "agent-assisted-v2"), "anthropic/claude-fable-5-1 [agent-assisted-v2]")
        self.assertEqual(slot_outputs(model), {"dir": "anthropic-claude-fable-5-1", "key_env": "ANTHROPIC_API_KEY",
                                               "billing": "metered"})
        self.assertEqual(source_name(plan_of("fable51")), "Anthropic")
        self.assertEqual(source_name(plan_of("direct")), "Direct APIs")
        go_round = read_plan(ROOT / "pilot/go-agent-v2-r2.json")
        self.assertEqual(source_name(go_round), "OpenCode Go")
        catalog = register({"schema_version": 1, "evaluations": []}, plan_of("direct"))
        self.assertEqual(catalog["evaluations"][0]["label"], "Direct APIs · go-agent-v2-direct-r1 · agent-assisted-v2")


class BillingTest(unittest.TestCase):
    def test_a_dispatch_must_acknowledge_exactly_how_the_plan_is_paid_for(self):
        go_round = read_plan(ROOT / "pilot/go-agent-v2-r2.json")
        check_billing(go_round, subscription_only=True, metered=False)
        check_billing(plan_of("fable52"), subscription_only=False, metered=True)
        for plan, flags in ((go_round, (False, False)), (go_round, (False, True)), (go_round, (True, True)),
                            (plan_of("direct"), (True, False)), (plan_of("direct"), (False, False)),
                            (plan_of("direct"), (True, True))):
            with self.subTest(plan=plan["name"], flags=flags), self.assertRaises(ValueError):
                check_billing(plan, subscription_only=flags[0], metered=flags[1])
        mixed = {"models": [{"id": "a"}, {"id": "b", "provider": "openai"}]}
        check_billing(mixed, subscription_only=True, metered=True)

    def test_the_scheduler_dispatches_with_the_plans_own_acknowledgment(self):
        self.assertIn("subscription_only=true", dispatch_inputs(read_plan(ROOT / "pilot/go-agent-v2-r2.json")))
        self.assertIn("metered=false", dispatch_inputs(read_plan(ROOT / "pilot/go-agent-v2-r2.json")))
        inputs = dispatch_inputs(plan_of("direct"))
        self.assertIn("subscription_only=false", inputs)
        self.assertIn("metered=true", inputs)

    def test_the_cli_refuses_the_wrong_acknowledgment_before_any_request(self):
        plan = str(ROOT / "pilot/go-agent-v2-fable51-r1.json")
        with patch("sys.argv", ["go_agent", "validate", "--plan", plan]):
            agent.main()  # offline validation needs no acknowledgment
        with patch("sys.argv", ["go_agent", "validate", "--plan", plan, "--metered"]):
            agent.main()
        for flags in (["--subscription-only"], ["--subscription-only", "--metered"]):
            argv = ["go_agent", "generate", "--plan", plan, "--model", "claude-fable-5-1", "--problem", PROBLEM, *flags]
            with self.subTest(flags=flags), patch("sys.argv", argv), patch.object(agent, "generate") as generate:
                with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
                    agent.main()
                generate.assert_not_called()


class PlanTest(unittest.TestCase):
    def test_every_round_plan_and_canary_is_valid_and_disabled(self):
        for family in FAMILIES:
            for attempt in (1, 2, 3):
                plan = plan_of(family, attempt)
                agent.validate(plan)
                self.assertEqual((plan["attempt"], plan["try"], plan["generation_enabled"]), (attempt, 1, False))
                self.assertEqual(plan["name"], f"go-agent-v2-{family}-r{attempt}")
            canary = json.loads((ROOT / f"pilot/go-canary-v2-{family}.json").read_text())
            self.assertEqual(agent.validate_canary(canary)["name"], f"go-agent-v2-{family}-r1")
            self.assertFalse(canary["generation_enabled"])

    def test_all_three_families_are_the_same_experiment_on_all_eight_problems(self):
        reference = plan_of("fable51")
        self.assertEqual(len(reference["problems"]), 8)
        self.assertEqual((reference["max_turns"], reference["max_checks"]), (20, 3))
        for family in FAMILIES:
            rounds = [plan_of(family, attempt) for attempt in (1, 2, 3)]
            for plan in rounds:
                for field in PROTOCOL_FIELDS:
                    self.assertEqual(plan[field], reference[field], (plan["name"], field))
                # A later round changes nothing but its name and repetition number.
                self.assertEqual({**plan, "name": "", "attempt": 0}, {**rounds[0], "name": "", "attempt": 0})
        fable51, fable52 = plan_of("fable51")["models"][0], plan_of("fable52")["models"][0]
        self.assertEqual({**fable51, "id": ""}, {**fable52, "id": ""})
        self.assertEqual((fable51["id"], fable52["id"]), ("claude-fable-5-1", "claude-fable-5-2"))

    def test_comparison_models_keep_round_one_settings(self):
        go_round = {model["id"]: model for model in read_plan(ROOT / "pilot/go-agent-v2-r1.json")["models"]}
        go_limits = read_plan(ROOT / "pilot/go-agent-v2-r1.json")["max_output_tokens"]
        direct = plan_of("direct")
        self.assertEqual([model["provider"] for model in direct["models"]], ["openai", "xai", "moonshot"])
        for model in direct["models"]:
            settings = {key: value for key, value in model.items() if key not in ("provider", "not_before")}
            earlier = {key: value for key, value in go_round[model["id"]].items() if key != "not_before"}
            self.assertEqual(settings, earlier)
            self.assertEqual(direct["max_output_tokens"][model["id"]], go_limits[model["id"]])

    def test_provider_settings_are_validated(self):
        plan = plan_of("fable52")
        fable = plan["models"][0]

        def with_model(**changes):
            model = {key: value for key, value in {**fable, **changes}.items() if value is not None}
            return {**plan, "models": [model]}

        validate_plan(with_model(output_config=None, cache_control=None))
        for changes in ({"provider": "unknown"}, {"api": "chat/completions"},
                        {"output_config": {"effort": "extreme"}}, {"output_config": {"effort": "high", "format": {}}},
                        {"cache_control": {"type": "ephemeral", "ttl": "1h"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_plan(with_model(**changes))
        # Anthropic's own request settings are never sent through another provider.
        with self.assertRaises(ValueError):
            validate_plan(with_model(provider=None))
        with self.assertRaises(ValueError):
            validate_plan(with_model(provider="moonshot", api="chat/completions"))
        # Moonshot serves chat/completions only.
        direct = plan_of("direct")
        kimi = {**direct["models"][2], "api": "responses", "token_limit_key": "max_output_tokens",
                "reasoning": {"effort": "high"}}
        kimi.pop("thinking")
        with self.assertRaisesRegex(ValueError, "does not serve"):
            validate_plan({**direct, "models": [*direct["models"][:2], kimi]})


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adpbench-provider-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_a_request_goes_only_to_its_providers_origin(self):
        model = plan_of("fable52")["models"][0]
        with patch("scripts.go_pilot.call_streaming", return_value={}) as stream:
            call_model(model, {"stream": True}, "secret", "session", 120, evidence=self.root)
        headers = stream.call_args.args[2]
        self.assertEqual((stream.call_args.kwargs["host"], stream.call_args.kwargs["base_path"]),
                         ("api.anthropic.com", "/v1/"))
        self.assertEqual(headers["x-api-key"], "secret")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("x-opencode-session", headers)

        conn = MagicMock()
        conn.getresponse.return_value.status = 401
        conn.getresponse.return_value.read.return_value = b"{}"
        with patch("scripts.go_stream.http.client.HTTPSConnection", return_value=conn) as create:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                call_model(model, {"stream": True}, "secret", "session", 120, evidence=self.root)
            caught.exception.close()
        create.assert_called_once_with("api.anthropic.com", timeout=120)
        self.assertEqual(conn.request.call_args.args[1], "/v1/messages")

    def test_the_default_origin_is_still_opencode_go(self):
        conn = MagicMock()
        conn.getresponse.return_value.status = 400
        conn.getresponse.return_value.read.return_value = b"{}"
        with patch("scripts.go_stream.http.client.HTTPSConnection", return_value=conn) as create:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                call_streaming("messages", {"stream": True}, {}, self.root, "", 120, 1800)
            caught.exception.close()
        create.assert_called_once_with("opencode.ai", timeout=120)
        self.assertEqual(conn.request.call_args.args[1], "/zen/go/v1/messages")

    def test_a_refusal_stream_keeps_its_category(self):
        response = MagicMock()
        response.read1.side_effect = [sse(
            {"type": "message_start", "message": {"id": "msg_1", "model": "claude-fable-5-2",
                                                  "usage": {"input_tokens": 7, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "refusal",
                                                "stop_details": {"type": "refusal", "category": "frontier_llm",
                                                                 "explanation": "declined"}},
             "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        )]
        data = read_stream(response, "messages", self.root, "", float("inf"), allow_tools=True)
        self.assertEqual(data["stop_reason"], "refusal")
        self.assertEqual(data["stop_details"]["category"], "frontier_llm")
        self.assertEqual(data["usage"], {"input_tokens": 7, "output_tokens": 1})

    def test_billable_tokens_split_cached_input_for_each_api(self):
        self.assertEqual(billable_tokens(tool_reply("check", {}, "c")["usage"], "messages"),
                         {"input": 100, "cache_read": 900, "cache_write": 50, "output": 40})
        self.assertEqual(
            billable_tokens({"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 800},
                             "output_tokens": 30}, "responses"),
            {"input": 200, "cache_read": 800, "cache_write": 0, "output": 30})
        self.assertEqual(
            billable_tokens({"prompt_tokens": 500, "cached_tokens": 400, "completion_tokens": 20}, "chat/completions"),
            {"input": 100, "cache_read": 400, "cache_write": 0, "output": 20})
        # No cache detail means nothing is assumed cached; a bad count is never trusted.
        self.assertEqual(billable_tokens({"prompt_tokens": 500, "completion_tokens": 20}, "chat/completions"),
                         {"input": 500, "cache_read": 0, "cache_write": 0, "output": 20})
        self.assertEqual(
            billable_tokens({"input_tokens": 10, "input_tokens_details": {"cached_tokens": 99},
                             "output_tokens": True}, "responses"),
            {"input": 0, "cache_read": 10, "cache_write": 0, "output": 0})

    def test_every_provider_key_shape_and_name_is_redacted(self):
        text = redact_credentials("sk-ant-api03-abcdefghijkl xai-abcdefghijklmnop sk-proj-abcdefghijkl", "")
        self.assertEqual(text, "[REDACTED] [REDACTED] [REDACTED]")
        for name in KEY_ENVS:
            self.assertTrue(PRIVATE_FIELDS.search(json.dumps({name: "x"})), name)


class DirectAgentTest(unittest.TestCase):
    """The agent loop against a mocked Anthropic model."""

    def setUp(self):
        self.plan = plan_of("fable52")
        self.plan["generation_enabled"] = True
        self.model = self.plan["models"][0]
        self.problem = load_problem(ROOT / "problems/level1" / PROBLEM)
        self.temp = tempfile.TemporaryDirectory(prefix="adpbench-direct-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        environment = {name: "" for name in KEY_ENVS} | {"ANTHROPIC_API_KEY": "test-only-not-a-real-key"}
        for patcher in (patch.dict(os.environ, environment),
                        patch.object(agent, "now_utc", return_value=datetime(2026, 11, 10, 12, tzinfo=timezone.utc)),
                        patch.object(agent.time, "sleep"),
                        patch.object(agent.Controller, "check", return_value={"correct": True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def replies(self):
        source = self.problem.baseline_rtl.read_text()
        return [tool_reply("write_file", {"path": "dut.v", "content": source}, "toolu_1"),
                tool_reply("check", {}, "toolu_2"), tool_reply("submit", {}, "toolu_3")]

    def run_slot(self, replies, name="run", model=None):
        with patch.object(agent, "call_model", side_effect=replies) as call:
            record = agent.generate(self.plan, model or self.model, PROBLEM, self.root / name, "unused")
        return record, call

    def test_roundtrip_records_provider_cost_classes_and_returns_thinking_unchanged(self):
        record, call = self.run_slot(self.replies())
        self.assertEqual(record["outcome"], "submitted", record)
        self.assertTrue((self.root / "run/anthropic-claude-fable-5-2" / PROBLEM / "rep1/dut.v").is_file())
        self.assertEqual(record["generation_settings"], {
            "provider": "anthropic", "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
            "cache_control": {"type": "ephemeral"}})
        self.assertEqual(record["billable_tokens"], {"input": 300, "cache_read": 2700, "cache_write": 150, "output": 120})
        self.assertEqual(call.call_args_list[0].args[2], "test-only-not-a-real-key")
        self.assertNotIn("test-only-not-a-real-key", json.dumps(record))

        first, second = call.call_args_list[0].args[1], call.call_args_list[1].args[1]
        self.assertEqual((first["model"], first["max_tokens"]), ("claude-fable-5-2", 128000))
        self.assertEqual((first["thinking"], first["output_config"], first["cache_control"], first["tool_choice"]),
                         ({"type": "adaptive"}, {"effort": "high"}, {"type": "ephemeral"}, {"type": "auto"}))
        self.assertNotIn("provider", first)
        self.assertNotIn("fallbacks", first)
        # History only grows, and the signed thinking block goes back exactly as it came.
        self.assertEqual(second["messages"][: len(first["messages"])], first["messages"])
        self.assertEqual(second["messages"][1]["content"][0],
                         {"type": "thinking", "thinking": "", "signature": "sig-toolu_1"})
        self.assertEqual(second["messages"][2]["content"][0]["tool_use_id"], "toolu_1")
        validate_generation(record, self.model, PROBLEM, self.plan)

    def test_a_refusal_is_a_model_outcome_that_is_never_rerun(self):
        refusal = {"content": [], "stop_reason": "refusal",
                   "stop_details": {"type": "refusal", "category": "frontier_llm", "explanation": "private text"},
                   "usage": {"input_tokens": 100, "output_tokens": 1}}
        record, call = self.run_slot([refusal])
        self.assertEqual((record["outcome"], record["refusal_category"], call.call_count), ("refused", "frontier_llm", 1))
        self.assertEqual(record["execution_health"], "completed")
        self.assertNotIn("private text", json.dumps(record))
        self.assertEqual(record["generation_state"], "failed")
        validate_generation(record, self.model, PROBLEM, self.plan)
        from scripts.publish_agent import RERUNNABLE
        self.assertNotIn("refused", RERUNNABLE)

        odd = {**refusal, "stop_details": {"category": "Not A Category!"}}
        record, _ = self.run_slot([odd], name="odd")
        self.assertEqual((record["outcome"], record["refusal_category"]), ("refused", ""))

    def test_an_overloaded_provider_is_retried_only_where_it_means_that(self):
        def overloaded():
            return urllib.error.HTTPError("https://api.anthropic.com/v1/messages", 529, "overloaded", {}, io.BytesIO(b"{}"))

        record, call = self.run_slot([overloaded(), *self.replies()])
        self.assertEqual((record["outcome"], call.call_count), ("submitted", 4))
        self.assertEqual(record["transport_retries"][0]["reason"], "http_529")

        go_plan = read_plan(ROOT / "pilot/go-agent-v2-r1.json")
        go_plan["generation_enabled"] = True
        go_model = next(model for model in go_plan["models"] if model["api"] == "messages")
        with patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-only-not-a-real-key"}), \
                patch.object(agent, "now_utc", return_value=datetime(2026, 9, 27, 12, tzinfo=timezone.utc)), \
                patch.object(agent, "call_model", side_effect=[overloaded()]) as call:
            record = agent.generate(go_plan, go_model, "001_dot_product", self.root / "go", "unused")
        self.assertEqual((record["outcome"], call.call_count), ("provider_error", 1))
        self.assertNotIn("billable_tokens", record)

    def test_a_missing_key_never_falls_back_to_another_providers(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "OPENCODE_GO_API_KEY": "another-providers-key"}):
            record, call = self.run_slot(self.replies(), name="no-key")
        self.assertEqual((record["outcome"], call.call_count), ("harness_error", 0))


if __name__ == "__main__":
    unittest.main()
