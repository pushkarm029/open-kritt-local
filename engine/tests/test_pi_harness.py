"""Unit tests for the Pi harness and Local AI provider wiring."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_kritt_engine import harnesses
from open_kritt_engine.generation import (
    HARNESS_THINKING_EFFORTS,
    MODEL_PROVIDER_HARNESSES,
    GenerationValidationError,
    validate_generation_job,
)
from open_kritt_engine.harnesses import HarnessError, PiHarness, harness_for, pi_agent_config
from open_kritt_engine.schema import EXTRACTOR_HELPER_FIELD

SCHEMA = {"type": "object", "properties": {"stub": {"type": "boolean"}}, "required": ["stub"]}


def assistant(text, *, stop="stop", usage=None, error=None):
    message = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "plan"}, {"type": "text", "text": text}],
        "stopReason": stop,
        "usage": usage or {"input": 100, "output": 20, "cacheRead": 50, "reasoning": 5, "totalTokens": 170},
    }
    if error:
        message["errorMessage"] = error
    return json.dumps({"type": "message_end", "message": message})


def events(*messages):
    header = json.dumps({"type": "session", "version": 3})
    user = json.dumps({"type": "message_end", "message": {"role": "user", "content": "task"}})
    return "\n".join([header, user, *messages, json.dumps({"type": "agent_settled", "aborted": False})])


def test_local_ai_uses_only_the_pi_harness_at_max_effort():
    assert MODEL_PROVIDER_HARNESSES["local"] == frozenset({"pi"})
    assert HARNESS_THINKING_EFFORTS["pi"] == frozenset({"max"})
    assert isinstance(harness_for("pi", timeout_seconds=5, model_provider="local"), PiHarness)
    job = {
        "kind": "workflow",
        "request": "draft a workflow",
        "model": "Qwen3.8-Flash-Next",
        "model_provider": "local",
        "harness": "codex",
        "thinking_effort": "max",
    }
    with pytest.raises(GenerationValidationError):
        validate_generation_job(job)
    job["harness"] = "pi"
    assert validate_generation_job(job)["harness"] == "pi"
    job["thinking_effort"] = "medium"
    with pytest.raises(GenerationValidationError):
        validate_generation_job(job)


def test_pi_config_targets_the_server_with_max_effort_and_sampling():
    models, settings = pi_agent_config("https://spark.example.ts.net/v1/", "qwen3:8b", api_key_configured=True, seed=7)
    provider = models["providers"]["local"]

    assert provider["baseUrl"] == "https://spark.example.ts.net/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "$LOCAL_AI_API_KEY"
    assert provider["authHeader"] is True
    assert provider["models"][0]["id"] == "qwen3:8b"
    assert provider["models"][0]["samplingParams"] == {
        "temperature": 0.6,
        "top_p": 0.95,
        "seed": 7,
        "reasoning_effort": "max",
        "chat_template_kwargs": {"reasoning_effort": "max"},
    }
    assert settings == {"defaultProvider": "local", "defaultModel": "qwen3:8b", "defaultProjectTrust": "never"}

    provider = pi_agent_config("http://host.docker.internal:11434/v1", "m", api_key_configured=False)[0]["providers"][
        "local"
    ]
    assert provider["apiKey"] == "local"
    assert provider["authHeader"] is False


def test_pi_config_uses_a_new_seed_for_each_attempt():
    seeds = {
        pi_agent_config("http://localhost:1234/v1", "m", api_key_configured=False)[0]["providers"]["local"]["models"][
            0
        ]["samplingParams"]["seed"]
        for _ in range(5)
    }
    assert len(seeds) > 1


@pytest.mark.parametrize(
    "base_url",
    [None, "", "localhost:11434/v1", "ftp://host/v1", 'http://host/v1"\nx', "http://host /v1"],
)
def test_pi_config_rejects_missing_or_unsafe_server_urls(base_url):
    with pytest.raises(HarnessError) as exc_info:
        pi_agent_config(base_url, "m", api_key_configured=False)
    assert exc_info.value.code == "configuration_error"


def test_pi_scan_runs_in_runner_with_job_home_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    calls = []

    def fake_docker(cmd, repo_dir, env, **_kwargs):
        return ["docker", "run", "open-kritt-engine:local", *cmd]

    def fake_run_process(cmd, prompt, cwd, timeout, env=None):
        calls.append((cmd, prompt, env))
        return SimpleNamespace(
            stdout=events(
                assistant(""),
                assistant(json.dumps({EXTRACTOR_HELPER_FIELD: True, "stub": True})),
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(harnesses, "_scan_docker_command", fake_docker)
    monkeypatch.setattr(harnesses, "_run_process", fake_run_process)
    result = PiHarness(timeout_seconds=5, model_provider="local").run(
        prompt="Scan this.",
        schema=SCHEMA,
        repo_dir=str(tmp_path / "repo"),
        model="Qwen3.8-Flash-Next",
        thinking_effort="max",
        env={
            "HOME": str(home),
            "LOCAL_AI_BASE_URL": "https://spark.example.ts.net/v1",
            "LOCAL_AI_API_KEY": "local-secret",
        },
    )

    cmd, prompt, env = calls[0]
    assert cmd[:3] == ["docker", "run", "open-kritt-engine:local"]
    for flag in ("--no-approve", "--no-extensions", "--no-mcp", "--no-context-files", "--no-session"):
        assert flag in cmd
    assert "--no-tools" not in cmd
    assert prompt == "Scan this."
    assert "PI_CODING_AGENT_DIR" not in env
    assert not any("local-secret" in part for part in cmd)
    agent_dir = Path(home) / ".pi" / "agent"
    models = json.loads((agent_dir / "models.json").read_text())
    assert "local-secret" not in (agent_dir / "models.json").read_text()
    assert models["providers"]["local"]["baseUrl"] == "https://spark.example.ts.net/v1"
    assert models["providers"]["local"]["models"][0]["samplingParams"]["reasoning_effort"] == "max"
    assert result.payload["stub"] is True
    assert result.usage["input_tokens"] == 300
    assert result.usage["cached_input_tokens"] == 100
    assert result.usage["output_tokens"] == 40
    assert result.usage["num_turns"] == 2
    assert result.usage["model_provider"] == "local"
    assert result.usage["thinking_effort"] == "max"


def test_pi_generation_is_tool_free_with_a_private_agent_dir(monkeypatch, tmp_path):
    seen = {}

    def fake_run_process(cmd, prompt, cwd, timeout, env=None):
        agent_dir = Path(env["PI_CODING_AGENT_DIR"])
        seen["cmd"] = cmd
        seen["env"] = dict(env)
        seen["settings"] = json.loads((agent_dir / "settings.json").read_text())
        return SimpleNamespace(stdout=events(assistant('{"stub": false}')), stderr="", returncode=0)

    monkeypatch.setattr(harnesses, "_run_process", fake_run_process)
    result = PiHarness(timeout_seconds=5, model_provider="local").run(
        prompt="Draft.",
        schema=SCHEMA,
        repo_dir=str(tmp_path),
        model="qwen3-coder",
        env={"HOME": str(tmp_path), "LOCAL_AI_BASE_URL": "http://host.docker.internal:11434/v1"},
        allow_tools=False,
    )

    assert seen["cmd"][0] == "pi"
    assert "--no-tools" in seen["cmd"] and "--no-skills" in seen["cmd"]
    assert seen["env"]["PI_OFFLINE"] == "1"
    assert seen["settings"]["defaultModel"] == "qwen3-coder"
    assert not Path(seen["env"]["PI_CODING_AGENT_DIR"]).exists()
    assert result.payload == {"stub": False}


def test_pi_reports_server_errors_and_unparseable_answers(monkeypatch, tmp_path):
    outputs = iter(
        [
            events(assistant("", stop="error", error="400: context_length_exceeded")),
            events(assistant("I could not finish.")),
            "",
        ]
    )
    monkeypatch.setattr(
        harnesses,
        "_run_process",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=next(outputs), stderr="boom", returncode=1),
    )
    harness = PiHarness(timeout_seconds=5, model_provider="local")
    kwargs = {
        "prompt": "Draft.",
        "schema": SCHEMA,
        "repo_dir": str(tmp_path),
        "model": "m",
        "env": {"HOME": str(tmp_path), "LOCAL_AI_BASE_URL": "http://localhost:1234/v1"},
        "allow_tools": False,
    }

    with pytest.raises(HarnessError) as server_error:
        harness.run(**kwargs)
    assert server_error.value.harness == "pi"
    assert server_error.value.output is not None
    with pytest.raises(HarnessError) as invalid:
        harness.run(**kwargs)
    assert invalid.value.code == "invalid_output"
    with pytest.raises(HarnessError) as empty:
        harness.run(**kwargs)
    assert empty.value.exit_code == 1


def test_pi_runner_gets_offline_settings(monkeypatch, tmp_path):
    data_dir = tmp_path / "engine-data"
    repo_dir = data_dir / "jobs" / "metadata-9" / "workspace"
    home_dir = data_dir / "jobs" / "metadata-9" / "home"
    repo_dir.mkdir(parents=True)
    home_dir.mkdir(parents=True)
    monkeypatch.setenv("ENGINE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ENGINE_DOCKER_DATA_DIR_HOST", str(tmp_path / "host-engine-data"))
    monkeypatch.setattr(harnesses.shutil, "which", lambda name: "docker" if name == "docker" else None)

    command = harnesses._scan_docker_command(["pi", "--print"], str(repo_dir), {"HOME": str(home_dir)})
    envs = [command[index + 1] for index, value in enumerate(command) if value == "--env"]

    assert "PI_OFFLINE=1" in envs
    assert "PI_TELEMETRY=0" in envs
