import json
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
        if "SELECT status, last_resumed_at" in sql:
            return Result({"status": self.scan["status"], "last_resumed_at": self.scan["last_resumed_at"]})
        assert "last_resumed_at IS NOT DISTINCT FROM" in sql
        marker = params[-1]
        if self.scan["status"] != "running" or self.scan["last_resumed_at"] != marker:
            return Result(None)
        if "base_commit_sha =" in sql:
            self.scan.update(base_commit_sha=params[0], commit_sha=params[1])
            return Result({"id": self.scan["id"]})
        if "status = 'completed'" in sql:
            self.scan.update(status="completed", extras=params[0].obj)
            return Result({"id": self.scan["id"]})
        elif "status = 'failed'" in sql:
            self.scan.update(status="failed", reasoning=params[0].obj)
        elif "diff_review" in params[0].obj:
            self.scan.update(extras=params[0].obj)
            return Result({"id": self.scan["id"]})
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
    diff["batches"] = [dict(diff)]
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
    assert "batches" not in scan["extras"]["diff_review"]
    assert scan["extras"]["diff_review"]["batches_completed"] == 1


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
    assert "reasoning" not in scan
    assert scan["extras"]["diff_review"]["batches_completed"] == 0


def test_no_changes_never_calls_model(review, monkeypatch):
    scan, diff, db = review
    diff.update(no_changes=True, files=[], patch="", batches=[])
    monkeypatch.setattr(commit_review, "review_diff", lambda *_args, **_kwargs: pytest.fail("unexpected inference"))
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "completed"
    assert scan["extras"]["diff_review"]["findings"] == []


def test_all_skipped_changes_complete_without_model(review, monkeypatch):
    scan, diff, db = review
    diff.update(files=[], patch="", batches=[], unreviewed=[{"path": "image.bin", "reason": "binary content"}])
    monkeypatch.setattr(commit_review, "review_diff", lambda *_args, **_kwargs: pytest.fail("unexpected inference"))
    assert commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "completed"
    assert scan["extras"]["diff_review"]["unreviewed"] == diff["unreviewed"]
    assert scan["extras"]["diff_review"]["batches_total"] == 0


def test_reviewable_files_without_batches_fail_instead_of_claiming_completion(review, monkeypatch):
    scan, diff, db = review
    diff["batches"] = []
    monkeypatch.setattr(commit_review, "review_diff", lambda *_args, **_kwargs: pytest.fail("unexpected inference"))
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "failed"
    assert "batches" in scan["reasoning"]["error"]


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


def test_batches_checkpoint_and_resume_without_repeating_completed_work(review, monkeypatch):
    scan, diff, db = review
    first = diff["batches"][0]
    second = {**first, "patch": "different bounded patch", "files": [{"path": "second.py"}]}
    diff["batches"] = [first, second]
    diff["files"] = [*first["files"], *second["files"]]
    calls = []

    def infer(batch, **_):
        calls.append(batch["patch"])
        if len(calls) == 2:
            raise SelfHostedConnectionError()
        return {"findings": [{"summary": batch["files"][0]["path"]}]}

    monkeypatch.setattr(commit_review, "review_diff", infer)
    assert commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "failed"
    checkpoint = scan["extras"]["diff_review"]
    assert checkpoint["batches_completed"] == 1
    assert checkpoint["batches_total"] == 2
    assert checkpoint["findings"] == [{"summary": "example.py"}]
    assert "bounded patch" not in repr(checkpoint)
    assert "fixture-key" not in repr(checkpoint)

    scan.update(status="running", last_resumed_at="attempt-two")
    monkeypatch.setattr(
        commit_review,
        "review_diff",
        lambda batch, **_: calls.append(batch["patch"]) or {"findings": [{"summary": "second.py"}]},
    )
    assert commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "completed"
    assert calls == ["bounded patch", "different bounded patch", "different bounded patch"]
    assert scan["extras"]["diff_review"]["findings"] == [
        {"summary": "example.py"},
        {"summary": "second.py"},
    ]


def test_changed_batch_partition_restarts_review(review, monkeypatch):
    scan, diff, db = review
    scan["extras"] = {
        "diff_review": commit_review._checkpoint(
            diff,
            commit_review._batch_hashes(diff["batches"]),
            {"base_url": "http://localhost:9000/v1", "model": "fixture-model"},
            [{"summary": "old"}],
            1,
        )
    }
    diff["batches"][0]["patch"] = "changed source"
    calls = []
    monkeypatch.setattr(commit_review, "review_diff", lambda *_args, **_kwargs: calls.append(1) or {"findings": []})
    commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert calls == [1]
    assert scan["extras"]["diff_review"]["findings"] == []


def test_cancellation_and_config_change_stop_before_next_batch(review, monkeypatch):
    scan, diff, db = review
    second = {**diff["batches"][0], "patch": "second patch", "files": [{"path": "second.py"}]}
    diff["batches"].append(second)
    diff["files"].extend(second["files"])
    calls = []

    def stop_after_first(batch, **_):
        calls.append(batch["patch"])
        scan["status"] = "stopped"
        return {"findings": []}

    monkeypatch.setattr(commit_review, "review_diff", stop_after_first)
    assert not commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "stopped"
    assert calls == ["bounded patch"]

    scan.update(status="running", last_resumed_at="attempt-two", extras={})
    settings = {"base_url": "http://localhost:9000/v1", "model": "fixture-model", "api_key": "fixture-key"}
    monkeypatch.setattr(commit_review, "local_settings", lambda: dict(settings))

    def change_after_first(batch, **_):
        calls.append(batch["patch"])
        settings["model"] = "different-model"
        return {"findings": []}

    monkeypatch.setattr(commit_review, "review_diff", change_after_first)
    assert commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert scan["status"] == "failed"
    assert calls == ["bounded patch", "bounded patch"]
    assert scan["extras"]["diff_review"]["batches_completed"] == 1


def test_outer_failure_cannot_fail_a_new_attempt(review):
    scan, _diff, db = review
    old_attempt = deepcopy(scan)
    scan["last_resumed_at"] = "attempt-two"
    commit_review.fail_commit_review(db, old_attempt)
    assert scan["status"] == "running"


@pytest.mark.parametrize("budget", ["", "1024", "0", "4097", "abc", "1.5"])
def test_local_thinking_budget_configuration(tmp_path, monkeypatch, budget):
    (tmp_path / "self-hosted.json").write_text(json.dumps({"baseUrl": "http://localhost:9000/v1", "model": "m"}))
    monkeypatch.setattr(commit_review, "settings_root", lambda: tmp_path)
    monkeypatch.setattr(commit_review, "provider_environment", lambda: {"SELF_HOSTED_API_KEY": "test-key"})
    monkeypatch.setattr(commit_review, "assert_account_assignment", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("SELF_HOSTED_THINKING_TOKEN_BUDGET", budget)
    if budget not in {"", "1024"}:
        with pytest.raises(ValueError, match="THINKING_TOKEN_BUDGET"):
            commit_review.local_settings()
    else:
        settings = commit_review.local_settings()
        assert settings.get("thinking_token_budget") == (1024 if budget else None)


def test_changed_thinking_budget_restarts_saved_review(review, monkeypatch):
    scan, diff, db = review
    settings = {"base_url": "http://localhost:9000/v1", "model": "fixture-model", "api_key": "fixture-key"}
    scan["extras"] = {
        "diff_review": commit_review._checkpoint(
            diff, commit_review._batch_hashes(diff["batches"]), settings, [{"summary": "old"}], 1
        )
    }
    settings["thinking_token_budget"] = 1024
    monkeypatch.setattr(commit_review, "local_settings", lambda: dict(settings))
    calls = []
    monkeypatch.setattr(commit_review, "review_diff", lambda *_args, **kwargs: calls.append(kwargs) or {"findings": []})
    assert commit_review.process_commit_review(db, SimpleNamespace(), 4)
    assert len(calls) == 1
    assert calls[0]["thinking_token_budget"] == 1024
    assert scan["extras"]["diff_review"]["findings"] == []
    assert scan["extras"]["diff_review"]["model_snapshot"]["thinking_token_budget"] == 1024
    assert "fixture-key" not in repr(scan["extras"])
