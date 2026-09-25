import json

import pytest

from open_kritt_engine import defensive_model
from open_kritt_engine.self_hosted import SelfHostedInputLimitError, SelfHostedInvalidOutputError


def _metadata():
    return {
        "path": "src/lib.rs",
        "base_path": "src/lib.rs",
        "head_path": "src/lib.rs",
        "status": "modified",
        "base_lines": 2,
        "head_lines": 2,
        "base_changed_lines": [[1, 1]],
        "head_changed_lines": [[1, 1]],
    }


def _unit():
    return {"metadata": _metadata(), "base": "old();\n\n", "head": "new();\n\n"}


def _source():
    return {"path": "src/lib.rs", "base": "old();\n\n", "head": "new();\n\n"}


def _settings():
    return {"base_url": "https://model.example/v1", "model": "local-model", "api_key": "test-secret"}


def _finding():
    return {
        "summary": "Changed value crosses a trust boundary without validation",
        "explanation": "The changed call passes the unvalidated value into the privileged operation.",
        "remediation": "Validate the value at the boundary before the privileged operation.",
        "path": "src/lib.rs",
        "side": "head",
        "line": 1,
        "confidence": "high",
    }


def test_build_context_uses_fixed_schema_and_excludes_credentials(monkeypatch):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps({"summary": "bounded", "invariants": ["ownership"], "limitations": []})

    monkeypatch.setattr(defensive_model, "_call", fake_call)
    result = defensive_model.build_context([_source()], [_metadata()], **_settings())

    assert result == {"summary": "bounded", "invariants": ["ownership"], "limitations": []}
    assert "test-secret" not in calls[0]["user_prompt"]
    assert calls[0]["schema"] is defensive_model._CONTEXT_SCHEMA


def test_fixed_prompt_is_language_neutral_and_read_only():
    assert "production source changes" in defensive_model._DEFENSIVE_SYSTEM_PROMPT
    assert "Rust" not in defensive_model._DEFENSIVE_SYSTEM_PROMPT
    assert "tools" in defensive_model._DEFENSIVE_SYSTEM_PROMPT
    assert "exploit payloads" in defensive_model._DEFENSIVE_SYSTEM_PROMPT


def test_review_unit_validates_changed_line_citations_and_focus(monkeypatch):
    finding = _finding()
    monkeypatch.setattr(defensive_model, "_call", lambda **_: json.dumps({"findings": [finding]}))

    result = defensive_model.review_unit(
        _unit(),
        [_source(), {"path": "src/types.rs", "base": "struct Old;\n", "head": "struct New;\n"}],
        {"summary": "bounded", "invariants": [], "limitations": []},
        focus="access",
        **_settings(),
    )

    assert result == {"findings": [finding]}

    bad = {**finding, "line": 2}
    monkeypatch.setattr(defensive_model, "_call", lambda **_: json.dumps({"findings": [bad]}))
    with pytest.raises(SelfHostedInvalidOutputError):
        defensive_model.review_unit(
            _unit(),
            [_source()],
            {"summary": "bounded", "invariants": [], "limitations": []},
            focus="state",
            **_settings(),
        )


def test_validate_candidates_requires_complete_unique_decisions(monkeypatch):
    candidate = _finding()
    monkeypatch.setattr(
        defensive_model,
        "_call",
        lambda **_: json.dumps({"decisions": [{"index": 0, "status": "supported", "explanation": "evidence"}]}),
    )
    result = defensive_model.validate_candidates(
        _unit(),
        [_source()],
        {"summary": "bounded", "invariants": [], "limitations": []},
        [candidate],
        **_settings(),
    )
    assert result["decisions"][0]["index"] == 0

    monkeypatch.setattr(
        defensive_model,
        "_call",
        lambda **_: json.dumps(
            {
                "decisions": [
                    {"index": 0, "status": "supported", "explanation": "one"},
                    {"index": 0, "status": "dismissed", "explanation": "two"},
                ]
            }
        ),
    )
    with pytest.raises(SelfHostedInvalidOutputError):
        defensive_model.validate_candidates(
            _unit(),
            [_source()],
            {"summary": "bounded", "invariants": [], "limitations": []},
            [candidate],
            **_settings(),
        )


def test_defensive_input_limit_is_checked_before_model_call(monkeypatch):
    called = False

    def fail_call(**_):
        nonlocal called
        called = True
        raise AssertionError("model call should not happen")

    monkeypatch.setattr(defensive_model, "_call", fail_call)
    sources = [{"path": f"src/{index}.rs", "base": "x" * 190_000, "head": "y" * 190_000} for index in range(3)]
    with pytest.raises(SelfHostedInputLimitError):
        defensive_model.build_context(sources, [], **_settings())
    assert not called


def test_structured_output_rejects_tool_calls_and_secret_echo(monkeypatch):
    monkeypatch.setattr(
        defensive_model,
        "_call",
        lambda **_: json.dumps({"summary": "ok", "invariants": [], "limitations": [], "tool_calls": []}),
    )
    with pytest.raises(SelfHostedInvalidOutputError):
        defensive_model.build_context([], [], **_settings())

    monkeypatch.setattr(
        defensive_model,
        "_call",
        lambda **_: json.dumps({"summary": "test-secret", "invariants": [], "limitations": []}),
    )
    with pytest.raises(SelfHostedInvalidOutputError):
        defensive_model.build_context([], [], **_settings())
