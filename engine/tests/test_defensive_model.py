import json

import pytest
from jsonschema import Draft202012Validator

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


def _captured_review_schema(monkeypatch, unit, sources):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps({"findings": []})

    monkeypatch.setattr(defensive_model, "_call", fake_call)
    assert defensive_model.review_unit(
        unit,
        sources,
        {"summary": "bounded", "invariants": [], "limitations": []},
        focus="access",
        **_settings(),
    ) == {"findings": []}
    assert len(calls) == 1
    return calls[0]["schema"], calls[0]["user_prompt"]


def _citation_errors(schema, finding):
    return list(Draft202012Validator(schema).iter_errors({"findings": [finding]}))


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


def test_outgoing_review_schema_binds_full_findings_to_changed_side_ranges(monkeypatch):
    unit = _unit()
    unit["metadata"] = {
        **unit["metadata"],
        "base_lines": 5,
        "head_lines": 5,
        "base_changed_lines": [[2, 2], [4, 4]],
        "head_changed_lines": [[2, 2], [4, 4]],
    }
    unit["base"] = "a\nb\nc\nd\ne\n"
    unit["head"] = "a\nB\nc\nD\ne\n"
    helper = {"path": "src/helper.rs", "base": "fn helper() {}\n", "head": "fn helper() {}\n"}
    schema, prompt = _captured_review_schema(monkeypatch, unit, [_source(), helper])
    finding = _finding()
    variants = schema["properties"]["findings"]["items"]["anyOf"]

    assert len(variants) == 2
    for variant in variants:
        assert set(variant["required"]) == set(finding)
        assert variant["properties"]["path"]["enum"] == ["src/lib.rs"]
        assert variant["properties"]["side"]["enum"] in (["base"], ["head"])
        assert "anyOf" in variant["properties"]["line"]
    assert not list(Draft202012Validator(schema).iter_errors({"findings": []}))
    assert not _citation_errors(schema, {**finding, "side": "head", "line": 2})
    assert not _citation_errors(schema, {**finding, "side": "base", "line": 4})
    assert _citation_errors(schema, {**finding, "path": "src/helper.rs", "side": "head", "line": 2})
    assert _citation_errors(schema, {**finding, "side": "head", "line": 3})
    assert _citation_errors(schema, {**finding, "side": "base", "line": 5})
    assert _citation_errors(schema, {key: value for key, value in finding.items() if key != "remediation"})
    assert "supporting files" in prompt.lower()
    assert "evidence" in prompt.lower()
    assert "one-based" in prompt.lower()
    assert "changed lines" in prompt.lower()


def test_outgoing_review_schema_omits_base_citations_without_base_changes(monkeypatch):
    unit = _unit()
    unit["metadata"] = {
        **unit["metadata"],
        "head_lines": 3,
        "base_changed_lines": [],
        "head_changed_lines": [[2, 2]],
    }
    unit["head"] = "new();\nadded();\n\n"
    schema, _ = _captured_review_schema(monkeypatch, unit, [_source()])
    finding = _finding()

    assert not _citation_errors(schema, {**finding, "side": "head", "line": 2})
    assert _citation_errors(schema, {**finding, "side": "base", "line": 1})


def test_outgoing_review_schema_correlates_renamed_side_paths(monkeypatch):
    unit = _unit()
    unit["metadata"] = {
        **unit["metadata"],
        "path": "src/new.rs",
        "base_path": "src/old.rs",
        "head_path": "src/new.rs",
        "status": "renamed",
    }
    source = {"path": "src/new.rs", "base": unit["base"], "head": unit["head"]}
    schema, _ = _captured_review_schema(monkeypatch, unit, [source])
    finding = _finding()

    assert not _citation_errors(schema, {**finding, "path": "src/old.rs", "side": "base"})
    assert not _citation_errors(schema, {**finding, "path": "src/new.rs", "side": "head"})
    assert _citation_errors(schema, {**finding, "path": "src/new.rs", "side": "base"})
    assert _citation_errors(schema, {**finding, "path": "src/old.rs", "side": "head"})


@pytest.mark.parametrize(
    "citation",
    [
        {"path": "src/helper.rs", "side": "head", "line": 1},
        {"path": "src/lib.rs", "side": "head", "line": 2},
        {"path": "src/lib.rs", "side": "base", "line": 2},
    ],
)
def test_local_review_parser_rejects_bad_citations_when_provider_ignores_schema(monkeypatch, citation):
    finding = {**_finding(), **citation}
    monkeypatch.setattr(defensive_model, "_call", lambda **_: json.dumps({"findings": [finding]}))

    with pytest.raises(SelfHostedInvalidOutputError):
        defensive_model.review_unit(
            _unit(),
            [_source(), {"path": "src/helper.rs", "base": "helper\n", "head": "helper\n"}],
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
