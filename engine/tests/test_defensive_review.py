"""Deterministic checks for the fixed defensive review queue executor."""

import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from open_kritt_engine import defensive_review
from open_kritt_engine.models import Job, State, Step, StepResultRow, Workflow
from open_kritt_engine.self_hosted import (
    SelfHostedAuthenticationError,
    SelfHostedConfigurationError,
    SelfHostedConnectionError,
    SelfHostedInvalidOutputError,
    SelfHostedModelError,
)


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, scan):
        self.scan = scan
        self.metadata = {}
        self.rows = {}
        self.next_metadata_id = 1
        self.next_row_id = 1
        self.locked = True
        self.lock_held = False
        self.unlock_count = 0
        self.commit_count = 0

    def execute(self, sql, params):
        if "pg_try_advisory_lock" in sql:
            acquired = self.locked and not self.lock_held
            self.lock_held = acquired
            return _Result({"acquired": acquired})
        if "pg_advisory_unlock" in sql:
            self.lock_held = False
            self.unlock_count += 1
            return _Result()
        if "SELECT status, last_resumed_at" in sql:
            return _Result({"status": self.scan["status"], "last_resumed_at": self.scan["last_resumed_at"]})
        if "UPDATE public.scans SET base_commit_sha" in sql:
            self.scan.update(base_commit_sha=params[0], commit_sha=params[1])
            return _Result()
        if "UPDATE workflows.step_metadata SET status = 'stopped'" in sql:
            for record in self.metadata.values():
                if record["status"] == "running":
                    record["status"] = "stopped"
            return _Result()
        if "UPDATE public.scans SET extras" in sql:
            payload, _, marker = params
            if self.scan["status"] != "running" or self.scan["last_resumed_at"] != marker:
                return _Result()
            self.scan.setdefault("extras", {}).update(payload.obj)
            if "status = 'completed'" in sql:
                self.scan["status"] = "completed"
            return _Result({"id": self.scan["id"]})
        if "UPDATE public.scans SET status = 'failed'" in sql:
            self.scan.update(status="failed", reasoning=params[0].obj)
            return _Result()
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        pass


class _Database:
    def __init__(self, scan, workflow):
        self.connection = _Connection(scan)
        self.workflow = workflow

    @contextmanager
    def connect(self):
        yield self.connection

    def load_scan(self, conn, scan_id):
        assert scan_id == conn.scan["id"]
        return deepcopy(conn.scan)

    def load_workflow(self, _conn, workflow_id):
        assert workflow_id == self.workflow.id
        return self.workflow

    def load_completed_metadata(self, conn, _scan_id):
        return {record["key"] for record in conn.metadata.values() if record["status"] == "completed"}

    def load_step_results(self, conn, _scan_id):
        return deepcopy(conn.rows)

    def claim_step_metadata(self, conn, **kwargs):
        key = (kwargs["step_id"], kwargs["prev_id"], kwargs["prev_table"], kwargs["repeat_run"])
        if key in self.load_completed_metadata(conn, kwargs["scan_id"]):
            return None
        metadata_id = conn.next_metadata_id
        conn.next_metadata_id += 1
        conn.metadata[metadata_id] = {"key": key, "status": "claimed", **kwargs}
        return metadata_id

    def update_metadata(self, conn, metadata_id, **kwargs):
        conn.metadata[metadata_id].update(kwargs)

    def insert_step_result(self, conn, **kwargs):
        key = (kwargs["step_id"], kwargs["prev_id"], kwargs["prev_table"], kwargs["repeat_run"])
        row_id = conn.next_row_id
        conn.next_row_id += 1
        conn.rows.setdefault(key, []).append(
            StepResultRow(
                id=row_id,
                step_id=kwargs["step_id"],
                prev_id=kwargs["prev_id"],
                prev_table=kwargs["prev_table"],
                repeat_run=kwargs["repeat_run"],
                json_answer=deepcopy(kwargs["json_answer"]),
            )
        )
        return row_id


@pytest.fixture
def review(monkeypatch):
    monkeypatch.setenv("ENGINE_RETRY_COUNT", "0")
    workflow = Workflow(
        id=15,
        name=defensive_review.WORKFLOW_NAME,
        steps=tuple(
            Step(
                id=index,
                name=name,
                depth=depth,
                multi_output=multi,
                consumes_all=consume,
                content=content,
                output_format=json.dumps(defensive_review.OUTPUT_FORMAT),
                is_last_step=False,
                output_table="workflows.step_results",
                order=index,
            )
            for index, (name, depth, multi, consume, content) in enumerate(defensive_review.STAGES, 1)
        ),
    )
    scan = {
        "id": 4,
        "status": "running",
        "last_resumed_at": "attempt-one",
        "comparison_mode": "commits",
        "model_provider": "self_hosted",
        "harness": "self-hosted",
        "model": "fixture-model",
        "workflow_id": workflow.id,
        "repo_full": "local/fixture",
        "repo_scope": "single",
        "configuration": {
            "review_kind": "workflow",
            "source_review_version": defensive_review.VERSION,
            "self_hosted_base_url": "http://localhost:9000/v1",
        },
        "base_commit_sha": "a" * 40,
        "commit_sha": "b" * 40,
        "extras": {},
    }
    metadata = {
        "path": "main.rs",
        "base_path": "main.rs",
        "head_path": "main.rs",
        "status": "modified",
        "base_lines": 1,
        "head_lines": 1,
        "base_changed_lines": [[1, 1]],
        "head_changed_lines": [[1, 1]],
    }
    diff = {
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "patch": "--- a/main.rs\n+++ b/main.rs\n@@ -1 +1 @@\n-before\n+after\n",
        "files": [metadata],
        "unreviewed": [],
        "no_changes": False,
        "sources": [{"path": "main.rs", "base": "before\n", "head": "after\n"}],
        "context_unreviewed": [],
    }
    settings = {"base_url": "http://localhost:9000/v1", "model": "fixture-model", "api_key": "fixture-key"}
    monkeypatch.setattr(defensive_review, "local_settings", lambda: dict(settings))

    def collect(_scan, _config, *, include_sources, include_context):
        assert include_sources is True and include_context is True
        return deepcopy(diff)

    monkeypatch.setattr(defensive_review, "prepare_diff", collect)
    db = _Database(scan, workflow)
    return SimpleNamespace(scan=scan, workflow=workflow, diff=diff, settings=settings, db=db)


def _models(monkeypatch, *, access=None, state=None, on_review=None, on_validate=None):
    calls = []

    def context(sources, files, **_settings):
        calls.append("context")
        assert sources[0]["path"] == files[0]["path"]
        return {"summary": "Synthetic context", "invariants": [], "limitations": []}

    def review_unit(unit, sources, context, *, focus, **_settings):
        calls.append(focus)
        assert unit["metadata"]["path"] == sources[0]["path"]
        assert context["summary"] == "Synthetic context"
        if on_review:
            on_review(focus)
        return {"findings": access or [] if focus == "access" else state or []}

    def validate(_unit, _sources, _context, candidates, **_settings):
        calls.append("validate")
        if on_validate:
            on_validate(candidates)
        return {
            "decisions": [
                {"index": index, "status": "supported", "explanation": "Fixture decision"}
                for index in range(len(candidates))
            ]
        }

    monkeypatch.setattr(defensive_review, "build_context", context)
    monkeypatch.setattr(defensive_review, "review_unit", review_unit)
    monkeypatch.setattr(defensive_review, "validate_candidates", validate)
    return calls


def test_full_one_unit_dag_persists_lineage_and_completed_report(review, monkeypatch):
    finding = {"path": "main.rs", "line": 1, "summary": "Synthetic candidate"}
    calls = _models(monkeypatch, access=[finding])

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])

    conn = review.db.connection
    assert review.scan["status"] == "completed", review.scan.get("reasoning")
    assert calls == ["context", "access", "validate", "state"]
    assert len(conn.metadata) == 7
    assert len([row for values in conn.rows.values() for row in values]) == 7
    assert all(record["status"] == "completed" for record in conn.metadata.values())
    assert {record["key"][0] for record in conn.metadata.values()} == {step.id for step in review.workflow.steps}
    assert all(record["checked_out_commit"] == review.diff["head_commit"] for record in conn.metadata.values())
    result = review.scan["extras"]["diff_review"]
    assert result["findings"] == [finding]
    assert result["workflow"]["report"]["review_passes"] == 2
    assert all(stage["status"] == "completed" for stage in result["workflow"]["stages"])
    assert "before\\n" not in repr(result)
    assert "fixture-key" not in repr(result)
    assert all("before\\n" not in record["prompt_filled"] for record in conn.metadata.values())


def test_empty_candidate_checks_skip_validation_model(review, monkeypatch):
    calls = _models(monkeypatch)

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])

    assert calls == ["context", "access", "state"]
    assert review.scan["extras"]["diff_review"]["workflow"]["report"]["supported"] == 0


def test_pickup_capacity_pause_releases_lock_and_resumes_after_scope(review, monkeypatch):
    calls = _models(monkeypatch)
    allowed = iter([True, False])
    conn = review.db.connection

    assert not defensive_review.process_defensive_review(
        review.db, SimpleNamespace(), review.scan["id"], can_pick_job=lambda: next(allowed)
    )

    assert review.scan["status"] == "running"
    assert calls == []
    assert len(conn.metadata) == 1
    assert len([row for values in conn.rows.values() for row in values]) == 1
    assert conn.metadata[1]["status"] == "completed"
    assert not conn.lock_held
    assert conn.unlock_count == 1

    assert defensive_review.process_defensive_review(
        review.db, SimpleNamespace(), review.scan["id"], can_pick_job=lambda: True
    )

    assert review.scan["status"] == "completed"
    assert calls == ["context", "access", "state"]
    assert len(conn.metadata) == 7
    assert sum(record["key"][0] == review.workflow.steps[0].id for record in conn.metadata.values()) == 1
    assert not conn.lock_held
    assert conn.unlock_count == 2


def test_scope_covers_every_changed_unit_and_rejects_missing_context(review, monkeypatch):
    units = defensive_review._units(review.diff)
    scope = Job(review.workflow.steps[0], State(prev_id=0, prev_table=None, repeat_run=1, context={}))
    assert defensive_review.run_stage(scope, review.diff, units, review.settings) == [
        {"unit_id": 1, "context": {}, "findings": []}
    ]
    context = Job(
        review.workflow.steps[1],
        State(prev_id=0, prev_table=None, repeat_run=1, context={"multi_output_depth_0": []}),
    )
    monkeypatch.setattr(defensive_review, "build_context", lambda *_args, **_kwargs: pytest.fail("unexpected model"))
    with pytest.raises(defensive_review.DefensiveReviewError, match="every changed source file"):
        defensive_review.run_stage(context, review.diff, units, review.settings)


def test_no_reviewable_source_completes_without_jobs_or_model(review, monkeypatch):
    review.diff.update(files=[], sources=[], patch="", unreviewed=[{"path": "image.bin", "reason": "binary content"}])
    calls = _models(monkeypatch)

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])

    result = review.scan["extras"]["diff_review"]
    assert review.scan["status"] == "completed"
    assert result["workflow"]["report"]["files_reviewed"] == 0
    assert all(stage["status"] == "skipped" for stage in result["workflow"]["stages"])
    assert review.db.connection.metadata == {}
    assert calls == []


@pytest.mark.parametrize("tamper", ["stage", "configuration", "provider"])
def test_workflow_tamper_and_unsupported_configuration_stop_before_model(review, monkeypatch, tamper):
    calls = _models(monkeypatch)
    if tamper == "stage":
        review.db.workflow = replace(
            review.workflow,
            steps=(replace(review.workflow.steps[0], content="untrusted stage"), *review.workflow.steps[1:]),
        )
    elif tamper == "configuration":
        review.scan["configuration"]["repeat_runs"] = 2
    else:
        review.scan["model_provider"] = "external"

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])

    assert review.scan["status"] == "failed"
    assert calls == []
    assert review.db.connection.metadata == {}


def test_report_requires_two_checked_passes_and_marks_conflicts_uncertain():
    finding = {"path": "main.rs", "line": 1, "summary": "Synthetic candidate"}
    supported = {
        "unit_id": 1,
        "findings": [finding],
        "context": {"decisions": [{"index": 0, "status": "supported", "explanation": "yes"}]},
    }
    dismissed = {
        "unit_id": 1,
        "findings": [finding],
        "context": {"decisions": [{"index": 0, "status": "dismissed", "explanation": "no"}]},
    }

    with pytest.raises(defensive_review.DefensiveReviewError, match="both review passes"):
        defensive_review.assemble_report([supported], 1)
    with pytest.raises(defensive_review.DefensiveReviewError, match="missing its source validation"):
        defensive_review.assemble_report([supported, {**dismissed, "context": {"decisions": []}}], 1)

    result = defensive_review.assemble_report([supported, dismissed], 1)
    assert result["findings"] == []
    assert result["uncertain_findings"] == [
        {**finding, "validation_explanation": "Review passes disagree. Manual review is required."}
    ]
    assert result["report"]["uncertain"] == 1


def test_model_failure_keeps_completed_jobs_and_retry_skips_them(review, monkeypatch):
    seen = []

    def fail_once(focus):
        seen.append(focus)
        if focus == "state" and seen.count("state") == 1:
            raise SelfHostedConnectionError()

    calls = _models(monkeypatch, on_review=fail_once)
    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])
    assert review.scan["status"] == "failed"
    assert calls == ["context", "access", "state"]
    completed_before = review.db.load_completed_metadata(review.db.connection, review.scan["id"])
    assert len(completed_before) == 4
    assert review.scan["extras"]["diff_review"]["workflow"]["report"] is None

    review.scan.update(status="running", last_resumed_at="attempt-two")
    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])

    assert review.scan["status"] == "completed"
    assert calls == ["context", "access", "state", "state"]
    assert completed_before <= review.db.load_completed_metadata(review.db.connection, review.scan["id"])
    assert len(review.db.connection.metadata) == 8


@pytest.mark.parametrize("error", [SelfHostedConnectionError, SelfHostedInvalidOutputError])
def test_retryable_stage_failure_retries_inline_without_extra_job_records(review, monkeypatch, error):
    monkeypatch.setenv("ENGINE_RETRY_COUNT", "2")
    sleeps = []
    monkeypatch.setattr(defensive_review.time, "sleep", sleeps.append)
    attempts = 0
    commits_before_attempt = []

    def fail_twice(focus):
        nonlocal attempts
        if focus == "access":
            commits_before_attempt.append(review.db.connection.commit_count)
            attempts += 1
            if attempts <= 2:
                raise error()

    calls = _models(monkeypatch, on_review=fail_twice)

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(retry_count=0), review.scan["id"])

    assert review.scan["status"] == "completed"
    assert attempts == 3
    assert calls == ["context", "access", "access", "access", "state"]
    assert sleeps == [1, 2]
    assert commits_before_attempt == sorted(set(commits_before_attempt))
    assert all(
        after > before for before, after in zip(commits_before_attempt, commits_before_attempt[1:], strict=False)
    )
    assert len(review.db.connection.metadata) == 7
    assert len([row for values in review.db.connection.rows.values() for row in values]) == 7


def test_retry_count_uses_worker_configuration_without_runtime_override(review, monkeypatch):
    monkeypatch.delenv("ENGINE_RETRY_COUNT", raising=False)
    sleeps = []
    monkeypatch.setattr(defensive_review.time, "sleep", sleeps.append)
    attempts = 0

    def fail_once(focus):
        nonlocal attempts
        if focus == "access":
            attempts += 1
            if attempts == 1:
                raise SelfHostedConnectionError()

    _models(monkeypatch, on_review=fail_once)

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(retry_count=1), review.scan["id"])

    assert review.scan["status"] == "completed"
    assert attempts == 2
    assert sleeps == [1]


@pytest.mark.parametrize("error", [SelfHostedAuthenticationError, SelfHostedModelError, SelfHostedConfigurationError])
def test_nonretryable_model_errors_fail_once(review, monkeypatch, error):
    monkeypatch.setenv("ENGINE_RETRY_COUNT", "2")
    sleeps = []
    monkeypatch.setattr(defensive_review.time, "sleep", sleeps.append)

    def fail(focus):
        if focus == "access":
            raise error()

    calls = _models(monkeypatch, on_review=fail)

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(retry_count=2), review.scan["id"])

    assert review.scan["status"] == "failed"
    assert calls == ["context", "access"]
    assert sleeps == []
    assert len(review.db.connection.rows) == 2


@pytest.mark.parametrize("change", ["stop", "new_attempt", "settings"])
def test_retry_rechecks_attempt_and_settings_before_another_model_call(review, monkeypatch, change):
    monkeypatch.setenv("ENGINE_RETRY_COUNT", "2")
    sleeps = []
    monkeypatch.setattr(defensive_review.time, "sleep", sleeps.append)

    def interrupt(focus):
        if focus == "access":
            if change == "stop":
                review.scan["status"] = "stopped"
            elif change == "new_attempt":
                review.scan["last_resumed_at"] = "attempt-two"
            else:
                review.settings["thinking_token_budget"] = 1024
            raise SelfHostedConnectionError()

    calls = _models(monkeypatch, on_review=interrupt)
    result = defensive_review.process_defensive_review(review.db, SimpleNamespace(retry_count=2), review.scan["id"])

    assert calls == ["context", "access"]
    assert len(review.db.connection.rows) == 2
    if change == "settings":
        assert result
        assert review.scan["status"] == "failed"
        assert "settings changed" in review.scan["reasoning"]["error"]
    else:
        assert not result
        assert review.scan["status"] == ("stopped" if change == "stop" else "running")
    assert len(sleeps) <= 1


@pytest.mark.parametrize("change", ["stop", "new_attempt"])
def test_cancellation_and_new_attempt_guard_before_writing_model_output(review, monkeypatch, change):
    def interrupt(focus):
        if focus == "access":
            if change == "stop":
                review.scan["status"] = "stopped"
            else:
                review.scan["last_resumed_at"] = "attempt-two"

    calls = _models(monkeypatch, on_review=interrupt)

    assert not defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])
    assert calls == ["context", "access"]
    assert len(review.db.connection.rows) == 2
    assert sum(record["status"] == "stopped" for record in review.db.connection.metadata.values()) == 1
    assert review.scan["extras"]["diff_review"]["workflow"]["report"] is None


@pytest.mark.parametrize("change", ["source", "settings"])
def test_changed_inputs_or_settings_reject_resume_before_model(review, monkeypatch, change):
    def fail_on_access(focus):
        if focus == "access":
            raise SelfHostedConnectionError()

    calls = _models(monkeypatch, on_review=fail_on_access)
    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])
    assert review.scan["status"] == "failed"
    completed = review.db.load_completed_metadata(review.db.connection, review.scan["id"])
    assert len(completed) == 2

    review.scan.update(status="running", last_resumed_at="attempt-two")
    if change == "source":
        review.diff["sources"][0]["head"] = "changed again\n"
    else:
        review.settings["thinking_token_budget"] = 1024
    before = list(calls)

    assert defensive_review.process_defensive_review(review.db, SimpleNamespace(), review.scan["id"])

    assert review.scan["status"] == "failed"
    assert "Source inputs or model settings changed" in review.scan["reasoning"]["error"]
    assert calls == before
    assert review.db.load_completed_metadata(review.db.connection, review.scan["id"]) == completed
