import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_kritt_engine.self_hosted import (
    MAX_INPUT_BYTES,
    SelfHostedAuthenticationError,
    SelfHostedConfigurationError,
    SelfHostedConnectionError,
    SelfHostedInputLimitError,
    SelfHostedInvalidOutputError,
    SelfHostedModelError,
    check_connection,
    review_diff,
)


def _finding(*, path="src/app.py", side="head", line=2):
    return {
        "summary": "Input reaches a shell command",
        "explanation": "The changed value is passed to a shell without safe argument handling.",
        "remediation": "Use an argument vector and validate the value.",
        "path": path,
        "side": side,
        "line": line,
        "confidence": "high",
    }


def _provider_response(findings=None, *, model="review-model", finish_reason="stop"):
    content = json.dumps({"findings": [] if findings is None else findings}, separators=(",", ":"))
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
    }


@contextmanager
def _mock_endpoint(*, response=None, status=200, raw_body=None, headers=None):
    requests = []
    response_headers = headers or {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, value)
            if raw_body is None:
                encoded = json.dumps(response or _provider_response()).encode("utf-8")
            else:
                encoded = raw_body
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):  # noqa: A002
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _diff(**overrides):
    diff = {
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "patch": "diff --git a/src/app.py b/src/app.py\n@@ -1,2 +1,2 @@\n-old()\n+new(user_input)\n",
        "files": [
            {
                "path": "src/app.py",
                "base_path": "src/app.py",
                "head_path": "src/app.py",
                "status": "modified",
                "base_lines": 2,
                "head_lines": 2,
                "base_changed_lines": [[2, 2]],
                "head_changed_lines": [[2, 2]],
            }
        ],
        "unreviewed": [],
        "no_changes": False,
    }
    diff.update(overrides)
    return diff


def test_review_diff_sends_bounded_structured_request_and_validates_finding_location():
    expected = _finding()
    with _mock_endpoint(response=_provider_response([expected])) as (base_url, requests):
        result = review_diff(_diff(), base_url=base_url + "/v1/", model="review-model", api_key="local-secret")

    assert result == {"findings": [expected]}
    assert len(requests) == 1
    request = requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["authorization"] == "Bearer local-secret"
    payload = json.loads(request["body"])
    assert payload["model"] == "review-model"
    assert payload["max_tokens"] == 8192
    assert "thinking_token_budget" not in payload
    assert payload["stream"] is False
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"]["properties"]["findings"]["maxItems"] == 50
    prompt = "\n".join(message["content"] for message in payload["messages"])
    assert "new(user_input)" in prompt
    assert "local-secret" not in request["body"].decode("utf-8")


def test_review_diff_accepts_base_side_deleted_location_and_renamed_paths():
    diff = _diff(
        patch="diff --git a/old.py b/new.py\n@@ -2 +0,0 @@\n-unsafe()\n",
        files=[
            {
                "path": "new.py",
                "base_path": "old.py",
                "head_path": "new.py",
                "status": "renamed",
                "base_lines": 2,
                "head_lines": 1,
                "base_changed_lines": [[2, 2]],
                "head_changed_lines": [[1, 1]],
            }
        ],
    )
    expected = _finding(path="old.py", side="base", line=2)
    with _mock_endpoint(response=_provider_response([expected])) as (base_url, _requests):
        result = review_diff(diff, base_url=base_url, model="review-model", api_key="key")

    assert result == {"findings": [expected]}


@pytest.mark.parametrize(
    ("finding", "description"),
    [
        (_finding(path="other.py"), "unreviewed path"),
        (_finding(line=1), "unchanged line"),
        (_finding(line=3), "line past end of file"),
        ({**_finding(), "unexpected": True}, "extra field"),
    ],
)
def test_review_diff_rejects_findings_outside_the_reviewed_contract(finding, description):
    del description
    with _mock_endpoint(response=_provider_response([finding])) as (base_url, _requests):
        with pytest.raises(SelfHostedInvalidOutputError):
            review_diff(_diff(), base_url=base_url, model="review-model", api_key="key")


def test_review_diff_returns_empty_without_calling_endpoint_for_identical_trees():
    diff = _diff(patch="", files=[], unreviewed=[], no_changes=True)
    result = review_diff(diff, base_url="https://unused.example/v1", model="m", api_key="k")
    assert result == {"findings": []}


def test_review_diff_rejects_oversized_input_before_network_call():
    diff = _diff(patch="x" * (MAX_INPUT_BYTES + 1))
    with pytest.raises(SelfHostedInputLimitError):
        review_diff(diff, base_url="https://unused.example/v1", model="m", api_key="k")


@pytest.mark.parametrize(
    "base_url",
    ["http://example.com", "https://user:pass@example.com/v1", "https://example.com/v1?", "https://example.com/v1#"],
)
def test_endpoint_validation_rejects_remote_plain_http_and_url_credentials_or_suffixes(base_url):
    with pytest.raises(SelfHostedConfigurationError):
        check_connection(base_url=base_url, model="m", api_key="k")


def test_check_connection_uses_the_exact_model_and_a_fixed_prompt_without_repository_data():
    with _mock_endpoint(response=_provider_response(model="exact-model")) as (base_url, requests):
        result = check_connection(base_url=base_url, model="exact-model", api_key="key")

    assert result == {"success": True, "model": "exact-model"}
    request = requests[0]
    assert request["path"] == "/v1/chat/completions"
    payload = json.loads(request["body"])
    assert payload["model"] == "exact-model"
    assert payload["max_tokens"] == 8192
    assert "thinking_token_budget" not in payload
    assert len(payload["messages"]) == 2
    assert "fixed connection check" in payload["messages"][1]["content"]
    assert "repository" in payload["messages"][1]["content"]
    assert "findings" in payload["response_format"]["json_schema"]["schema"]["properties"]


@pytest.mark.parametrize("budget", [1, 1024, 4096])
def test_explicit_thinking_budget_reaches_review_and_connection_requests(budget):
    with _mock_endpoint(response=_provider_response()) as (base_url, requests):
        assert review_diff(
            _diff(), base_url=base_url, model="review-model", api_key="key", thinking_token_budget=budget
        ) == {"findings": []}
        assert check_connection(
            base_url=base_url, model="review-model", api_key="key", thinking_token_budget=budget
        ) == {"success": True, "model": "review-model"}

    assert len(requests) == 2
    for request in requests:
        payload = json.loads(request["body"])
        assert payload["max_tokens"] == 8192
        assert payload["thinking_token_budget"] == budget


@pytest.mark.parametrize("budget", [True, False, 0, -1, 4097, 1.0, "1024"])
def test_invalid_thinking_budget_fails_before_network(budget):
    with _mock_endpoint(response=_provider_response()) as (base_url, requests):
        with pytest.raises(SelfHostedConfigurationError, match="Thinking token budget"):
            review_diff(_diff(), base_url=base_url, model="review-model", api_key="key", thinking_token_budget=budget)
        with pytest.raises(SelfHostedConfigurationError, match="Thinking token budget"):
            check_connection(base_url=base_url, model="review-model", api_key="key", thinking_token_budget=budget)
    assert requests == []


@pytest.mark.parametrize(
    ("status", "exception"),
    [
        (401, SelfHostedAuthenticationError),
        (403, SelfHostedAuthenticationError),
        (404, SelfHostedModelError),
        (422, SelfHostedModelError),
        (500, SelfHostedConnectionError),
    ],
)
def test_provider_http_errors_are_classified_without_exposing_body_or_key(status, exception):
    secret = "do-not-leak-this-api-key"
    with _mock_endpoint(status=status, raw_body=secret.encode("utf-8")) as (base_url, _requests):
        with pytest.raises(exception) as caught:
            check_connection(base_url=base_url, model="m", api_key=secret)
    assert secret not in str(caught.value)
    assert secret not in caught.value.message
    assert caught.value.code in {"authentication", "model", "connection"}


def test_redirect_is_not_followed():
    with _mock_endpoint(status=302, headers={"Location": "http://127.0.0.1:1/redirected"}) as (base_url, requests):
        with pytest.raises(SelfHostedConnectionError):
            check_connection(base_url=base_url, model="m", api_key="key")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "raw_body",
    [
        b"not json",
        b'{"choices":[]}',
        json.dumps({"choices": [{"message": {"content": '{"findings":[],"extra":true}'}}]}).encode(),
    ],
)
def test_invalid_provider_envelopes_and_model_content_use_safe_invalid_output_error(raw_body):
    with _mock_endpoint(raw_body=raw_body) as (base_url, _requests):
        with pytest.raises(SelfHostedInvalidOutputError):
            check_connection(base_url=base_url, model="m", api_key="key")


def test_truncated_response_has_specific_safe_error():
    with _mock_endpoint(response=_provider_response(finish_reason="length")) as (base_url, _):
        with pytest.raises(SelfHostedInvalidOutputError, match="output limit"):
            check_connection(base_url=base_url, model="review-model", api_key="key")


@pytest.mark.parametrize(
    ("finding", "message"),
    [(_finding(path="other.py"), "file outside"), (_finding(line=1), "line outside")],
)
def test_rejected_citation_identifies_the_contract_failure(finding, message):
    with _mock_endpoint(response=_provider_response([finding])) as (base_url, _):
        with pytest.raises(SelfHostedInvalidOutputError, match=message):
            review_diff(_diff(), base_url=base_url, model="review-model", api_key="key")


def test_response_size_limit_is_enforced():
    oversized = b" " * (1024 * 1024 + 1)
    with _mock_endpoint(raw_body=oversized) as (base_url, _requests):
        with pytest.raises(SelfHostedInvalidOutputError):
            check_connection(base_url=base_url, model="m", api_key="key")


def test_connection_check_rejects_output_that_echoes_the_api_key():
    secret = "must-not-come-back"
    response = _provider_response([_finding()], model="m")
    response["choices"][0]["message"]["content"] = json.dumps({"findings": [], "note": secret}, separators=(",", ":"))
    with _mock_endpoint(response=response) as (base_url, _requests):
        with pytest.raises(SelfHostedInvalidOutputError) as caught:
            check_connection(base_url=base_url, model="m", api_key=secret)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("field", ["summary", "explanation", "remediation"])
def test_review_rejects_credentials_in_decoded_finding_text(field):
    secret = "test-only-credential"
    response = _provider_response([{**_finding(), field: f"Response containing {secret}."}])
    message = response["choices"][0]["message"]
    message["content"] = message["content"].replace(secret, secret.replace("t", "\\u0074"))
    with _mock_endpoint(response=response) as (base_url, _requests):
        with pytest.raises(SelfHostedInvalidOutputError) as caught:
            review_diff(_diff(), base_url=base_url, model="review-model", api_key=secret)
    assert secret not in str(caught.value)


def test_connection_check_rejects_a_different_model_in_the_provider_response():
    with _mock_endpoint(response=_provider_response(model="other-model")) as (base_url, _requests):
        with pytest.raises(SelfHostedInvalidOutputError):
            check_connection(base_url=base_url, model="expected-model", api_key="key")


def test_connection_check_rejects_valid_json_with_length_finish_reason():
    with _mock_endpoint(response=_provider_response(model="m", finish_reason="length")) as (base_url, _requests):
        with pytest.raises(SelfHostedInvalidOutputError):
            check_connection(base_url=base_url, model="m", api_key="key")
