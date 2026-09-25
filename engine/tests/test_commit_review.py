from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from open_kritt_engine import commit_review
from open_kritt_engine.commit_diff import CommitDiffError
from open_kritt_engine.self_hosted import SelfHostedConnectionError


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, scan):
        self.scan = scan
        self.locked = True

    def execute(self, sql, params):
        if "pg_try_advisory_lock" in sql:
            return Result({"acquired": self.locked})
        if "pg_advisory_unlock" in sql:
            return Result(None)
        assert "last_resumed_at IS NOT DISTINCT FROM" in sql
        marker = params[-1]
        if self.scan["status"] != "running" or self.scan["last_resumed_at"] != marker:
            return Result(None)
        if "base_commit_sha =" in sql:
            self.scan.update(base_commit_sha=params[0], commit_sha=params[1])
            return Result({"id": self.scan["id"]})
        if "status = 'completed'" in sql:
            self.scan.update(status="completed", extras=params[0].obj)
        elif "status = 'failed'" in sql:
            self.scan.update(status="failed", reasoning=params[0].obj)
        return Result(None)

    def commit(self):
        pass

    def rollback(self):
        pass


class Database:
    def __init__(self, scan):
        self.connection = Connection(scan)

    @contextmanager
    def connect(self):
        yield self.connection

    def load_scan(self, conn, scan_id):
        return deepcopy(conn.scan)


@pytest.fixture
def review(monkeypatch):
    scan = {
        "id": 4,
        "status": "running",
        "comparison_mode": "commits",
        "model_provider": "self_hosted",
        "model": "fixture-model",
        "configuration": {"self_hosted_base_url": "http://localhost:9000/v1"},
        "last_resumed_at": "attempt-one",
        "base_commit_sha": "a" * 7,
        "commit_sha": "b" * 7,
    }
    diff = {
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "patch": "bounded patch",
        "files": [{"path": "example.py"}],
        "unreviewed": [],
        "no_changes": False,
    }
    monkeypatch.setattr(
        commit_review,
        "local_settings",
        lambda: {"base_url": "http://localhost:9000/v1", "model": "fixture-model", "api_key": "fixture-key"},
    )
    monkeypatch.setattr(commit_review, "prepare_diff", lambda *_: diff)
    return scan, diff, Database(scan)


def test_pins_before_inference_and_does_not_store_patch(review, monkeypatch):
    scan, _diff, db = review

    def infer(*_, **__):
        assert scan["base_commit_sha"] == "a" * 40
        assert scan["commit_sha"] == "b" * 40
        return {"findings": [{"summary": "Source finding"}]}

    monkeypatch.setattr(commit_review, "review_diff", infer)
    assert commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "completed"
    assert "patch" not in scan["extras"]["diff_review"]


@pytest.mark.parametrize("fails", [False, True])
def test_old_attempt_cannot_overwrite_new_retry(review, monkeypatch, fails):
    scan, _diff, db = review

    def infer(*_, **__):
        scan["last_resumed_at"] = "attempt-two"
        if fails:
            raise SelfHostedConnectionError()
        return {"findings": []}

    monkeypatch.setattr(commit_review, "review_diff", infer)
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "running"
    assert "reasoning" not in scan and "extras" not in scan


def test_no_changes_never_calls_model(review, monkeypatch):
    scan, diff, db = review
    diff.update(no_changes=True, files=[], patch="")
    monkeypatch.setattr(commit_review, "review_diff", lambda *_args, **_kwargs: pytest.fail("unexpected inference"))
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "completed"
    assert scan["extras"]["diff_review"]["findings"] == []


def test_invalid_revision_has_field_error_without_inference(review, monkeypatch):
    scan, _diff, db = review

    def invalid(*_):
        raise CommitDiffError("base commit object was not found", field="base_commit_sha")

    monkeypatch.setattr(commit_review, "prepare_diff", invalid)
    monkeypatch.setattr(commit_review, "review_diff", lambda *_args, **_kwargs: pytest.fail("unexpected inference"))
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "failed"
    assert scan["reasoning"]["errors"][0]["field"] == "base_commit_sha"


def test_changed_provider_does_not_receive_source(review, monkeypatch):
    scan, _diff, db = review
    scan["configuration"]["self_hosted_base_url"] = "https://previous.example/v1"
    monkeypatch.setattr(commit_review, "prepare_diff", lambda *_: pytest.fail("unexpected source read"))
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "failed"
    assert "configuration changed" in scan["reasoning"]["error"]


def test_unexpected_failure_is_terminal_and_does_not_expose_details(review, monkeypatch):
    scan, _diff, db = review

    def fail(*_args, **_kwargs):
        raise KeyError("private implementation detail")

    monkeypatch.setattr(commit_review, "review_diff", fail)
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "failed"
    assert "unexpectedly" in scan["reasoning"]["error"]
    assert "private" not in scan["reasoning"]["error"]


def test_outer_failure_cannot_fail_a_new_attempt(review):
    scan, _diff, db = review
    old_attempt = deepcopy(scan)
    scan["last_resumed_at"] = "attempt-two"
    commit_review.fail_commit_review(db, old_attempt)
    assert scan["status"] == "running"
