"""Fixed, read-only review stages using Kritt's workflow queue and job records."""

import hashlib
import json
import time
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .commit_diff import CommitDiffError
from .commit_review import _model_snapshot, _save_checkpoint, local_settings, prepare_diff
from .defensive_model import build_context, review_unit, validate_candidates
from .queue import build_pending_jobs
from .runtime_config import runtime_int
from .self_hosted import SelfHostedConnectionError, SelfHostedError, SelfHostedInvalidOutputError

WORKFLOW_NAME = "Local defensive review v1"
VERSION = 1
STAGES = (
    ("Scope", 0, True, False, "Identify changed source units."),
    (
        "Contract context",
        1,
        True,
        True,
        "Record affected contracts and trust boundaries from the changed source.",
    ),
    (
        "Access and validation review",
        2,
        False,
        False,
        "Review changed access checks and input validation for source-supported defects.",
    ),
    (
        "State and accounting review",
        2,
        False,
        False,
        "Review changed state transitions and accounting for source-supported defects.",
    ),
    (
        "Check findings",
        3,
        False,
        False,
        "Check each finding against changed source lines and identify a correction.",
    ),
    ("Report", 4, False, True, "Summarize checked findings, review coverage, and limitations."),
)
OUTPUT_FORMAT = {"unit_id": "number", "context": "object", "findings": "array"}
MAX_RESULT_BYTES = 2 * 1024 * 1024
LIMITATION = "Source review only. No runtime validation or exploit reproduction was performed."


class DefensiveReviewError(ValueError):
    """An error safe to display without source, credentials, or provider output."""


def validate_workflow(scan, workflow):
    configuration = scan.get("configuration") or {}
    if (
        scan.get("comparison_mode") != "commits"
        or scan.get("model_provider") != "self_hosted"
        or scan.get("harness") != "self-hosted"
        or configuration.get("review_kind") != "workflow"
        or configuration.get("source_review_version") != VERSION
        or set(configuration) != {"review_kind", "source_review_version", "self_hosted_base_url"}
        or scan.get("model_overrides")
        or scan.get("dependencies")
        or scan.get("agent_skill_ids")
        or scan.get("post_script_ids")
        or int(scan.get("post_script_id") or 0) != 0
        or workflow.id != int(scan["workflow_id"])
        or workflow.name != WORKFLOW_NAME
        or len(workflow.steps) != len(STAGES)
        or len({step.id for step in workflow.steps}) != len(STAGES)
    ):
        raise DefensiveReviewError("This scan requires the unchanged built-in defensive review workflow.")
    for step, (name, depth, multi, consume, content) in zip(workflow.steps, STAGES, strict=True):
        if (
            step.name != name
            or step.depth != depth
            or step.multi_output != multi
            or step.consumes_all != consume
            or step.content != content
            or step.is_last_step
            or step.output_table != "workflows.step_results"
            or step.bound_source_step_id is not None
            or json.loads(step.output_format) != OUTPUT_FORMAT
        ):
            raise DefensiveReviewError(
                "The built-in defensive review workflow has changed. Restore it before retrying."
            )


def _units(diff):
    sources = {source["path"]: source for source in diff["sources"]}
    if len(sources) != len(diff["sources"]):
        raise DefensiveReviewError("Source context contains duplicate paths.")
    return {
        index: {
            "metadata": metadata,
            "base": sources[metadata["path"]]["base"],
            "head": sources[metadata["path"]]["head"],
        }
        for index, metadata in enumerate(diff["files"], 1)
    }


def _record(unit_id, context=None, findings=None):
    return {"unit_id": unit_id, "context": context or {}, "findings": findings or []}


def run_stage(job, diff, units, settings):
    """Dispatch trusted stage code; persisted workflow prose is never executed."""
    name = job.step.name
    if name == "Scope":
        return [_record(unit_id) for unit_id in units]
    if name == "Contract context":
        inputs = job.state.context.get("multi_output_depth_0", [])
        if sorted(row.get("unit_id") for row in inputs) != sorted(units):
            raise DefensiveReviewError("The scope stage did not account for every changed source file.")
        context = build_context(diff["sources"], diff["files"], **settings)
        return [_record(unit_id, context) for unit_id in units]
    if name == "Report":
        rows = job.state.context.get("multi_output_depth_3", [])
        return [_record(0, assemble_report(rows, len(units)))]

    unit_id = job.state.context.get("unit_id")
    if isinstance(unit_id, bool) or not isinstance(unit_id, int) or unit_id not in units:
        raise DefensiveReviewError("A workflow job references a source unit outside the pinned scope.")
    context = job.state.context.get("context") or {}
    unit = units[unit_id]
    if name in {"Access and validation review", "State and accounting review"}:
        result = review_unit(
            unit,
            diff["sources"],
            context,
            focus="access" if name == "Access and validation review" else "state",
            **settings,
        )
        return [_record(unit_id, context, result["findings"])]
    if name == "Check findings":
        candidates = job.state.context.get("findings") or []
        decisions = (
            validate_candidates(unit, diff["sources"], context, candidates, **settings)["decisions"]
            if candidates
            else []
        )
        return [_record(unit_id, {"decisions": decisions}, candidates)]
    raise DefensiveReviewError("Unsupported defensive review stage.")


def assemble_report(rows, unit_count):
    if len(rows) != unit_count * 2 or any(
        sum(row.get("unit_id") == unit_id for row in rows) != 2 for unit_id in range(1, unit_count + 1)
    ):
        raise DefensiveReviewError("The report requires both review passes and their checks for every source file.")
    supported, uncertain, dismissed = {}, {}, set()
    for row in rows:
        candidates = row["findings"]
        decisions = row["context"]["decisions"]
        if sorted(decision["index"] for decision in decisions) != list(range(len(candidates))):
            raise DefensiveReviewError("A candidate is missing its source validation decision.")
        for decision in decisions:
            finding = candidates[decision["index"]]
            key = json.dumps(finding, sort_keys=True, ensure_ascii=False)
            if decision["status"] == "supported":
                supported[key] = finding
            elif decision["status"] == "uncertain":
                uncertain[key] = {**finding, "validation_explanation": decision["explanation"]}
            elif decision["status"] == "dismissed":
                dismissed.add(key)
            else:
                raise DefensiveReviewError("Invalid source validation decision.")
    # Conflicting review decisions remain visible for human review.
    for key in set(supported) & (set(uncertain) | dismissed):
        uncertain[key] = {
            **supported.pop(key),
            "validation_explanation": "Review passes disagree. Manual review is required.",
        }
    report = {
        "summary": f"Completed two review passes and candidate checks for {unit_count} source files. {LIMITATION}",
        "files_reviewed": unit_count,
        "review_passes": unit_count * 2,
        "supported": len(supported),
        "uncertain": len(uncertain),
        "dismissed": len(dismissed - set(uncertain)),
    }
    result = {"report": report, "findings": list(supported.values()), "uncertain_findings": list(uncertain.values())}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
        raise DefensiveReviewError("The review report exceeds 2 MiB. Use a narrower committed comparison.")
    return result


def _guard(conn, scan_id, marker):
    row = conn.execute(
        "SELECT status, last_resumed_at FROM public.scans WHERE id = %s FOR UPDATE", (scan_id,)
    ).fetchone()
    return bool(row and row["status"] == "running" and row["last_resumed_at"] == marker)


def _result(diff, workflow, completed, rows, input_hash, settings, *, active_step=None):
    count = len(diff["files"])
    totals = (1, 1, count, count, count * 2, 1)
    stages = []
    for step, total in zip(workflow.steps, totals, strict=True):
        done = sum(key[0] == step.id for key in completed)
        status = "completed" if done == total else "running" if step.id == active_step else "pending"
        stages.append({"name": step.name, "status": status, "completed": done, "total": total})
    result_rows = [row.json_answer for values in rows.values() for row in values]
    limitations = [LIMITATION]
    for row in result_rows:
        for limitation in row.get("context", {}).get("limitations", []):
            if limitation not in limitations:
                limitations.append(limitation)
    if diff.get("context_unreviewed"):
        limitations.append(
            f"{len(diff['context_unreviewed'])} supporting context files could not be read. See export details."
        )
    final = next(
        (
            row.json_answer["context"]
            for values in rows.values()
            for row in values
            if row.step_id == workflow.steps[-1].id
        ),
        {},
    )
    return {
        **{key: diff[key] for key in ("base_commit", "head_commit", "files", "unreviewed", "no_changes")},
        "context_unreviewed": diff.get("context_unreviewed", []),
        "findings": final.get("findings", []),
        "workflow": {
            "name": WORKFLOW_NAME,
            "version": VERSION,
            "input_hash": input_hash,
            "model_snapshot": _model_snapshot(settings),
            "stages": stages,
            "limitations": limitations,
            "report": final.get("report"),
            "uncertain_findings": final.get("uncertain_findings", []),
        },
    }


def process_defensive_review(db, config, scan_id, *, can_pick_job=None):
    with db.connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (-int(scan_id),)).fetchone()["acquired"]:
            return False
        marker, metadata_id, started = None, None, datetime.now(timezone.utc)
        result = None
        try:
            scan = db.load_scan(conn, scan_id)
            if not scan or scan["status"] != "running":
                return False
            marker = scan.get("last_resumed_at")
            workflow = db.load_workflow(conn, int(scan["workflow_id"]))
            validate_workflow(scan, workflow)
            settings = local_settings()
            snapshot = _model_snapshot(settings)
            if (
                settings["model"] != scan["model"]
                or settings["base_url"] != scan["configuration"]["self_hosted_base_url"]
            ):
                raise DefensiveReviewError("Local model settings changed. Create a new review.")
            conn.commit()
            diff = prepare_diff(scan, config, include_sources=True, include_context=True)
            units = _units(diff)
            input_hash = hashlib.sha256(
                json.dumps(
                    {"diff": diff, "model": snapshot, "version": VERSION},
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            previous = (scan.get("extras") or {}).get("diff_review", {}).get("workflow", {})
            if previous and previous.get("input_hash") != input_hash:
                raise DefensiveReviewError(
                    "Source inputs or model settings changed. Create a new review to keep results separate."
                )
            if not _guard(conn, scan_id, marker):
                return False
            conn.execute(
                "UPDATE public.scans SET base_commit_sha = %s, commit_sha = %s WHERE id = %s",
                (diff["base_commit"], diff["head_commit"], scan_id),
            )
            # The session lock proves no other worker still owns these interrupted jobs.
            conn.execute(
                """UPDATE workflows.step_metadata SET status = 'stopped', phase = 'interrupted', updated_at = now()
                   WHERE scan_id = %s AND status = 'running' AND coalesce(kind, 'step') = 'step'""",
                (scan_id,),
            )
            conn.commit()
            if not units:
                empty = _result(diff, workflow, set(), {}, input_hash, settings)
                for stage in empty["workflow"]["stages"]:
                    stage.update(status="skipped", total=0)
                empty["workflow"]["report"] = {
                    "summary": "No changed source files could be reviewed. No model requests were made.",
                    "files_reviewed": 0,
                    "review_passes": 0,
                    "supported": 0,
                    "uncertain": 0,
                    "dismissed": 0,
                }
                return _save_checkpoint(conn, scan_id, marker, empty, completed=True)

            while True:
                if can_pick_job is not None and not can_pick_job():
                    return False
                if not _guard(conn, scan_id, marker):
                    return False
                completed = db.load_completed_metadata(conn, scan_id)
                rows = db.load_step_results(conn, scan_id)
                pending = build_pending_jobs(scan=scan, workflow=workflow, completed=completed, step_results=rows)
                result = _result(diff, workflow, completed, rows, input_hash, settings)
                if not pending:
                    if not result["workflow"]["report"]:
                        raise DefensiveReviewError("The workflow stopped before producing its final report.")
                    return _save_checkpoint(conn, scan_id, marker, result, completed=True)
                job = pending[0]
                settings = local_settings()
                if _model_snapshot(settings) != snapshot:
                    raise DefensiveReviewError("Local model settings changed during review. Create a new review.")
                started = datetime.now(timezone.utc)
                metadata_id = db.claim_step_metadata(
                    conn,
                    scan_id=scan_id,
                    workflow_id=workflow.id,
                    step_id=job.step.id,
                    prev_id=job.state.prev_id,
                    prev_table=job.state.prev_table,
                    repeat_run=job.state.repeat_run,
                    prompt_template=job.step.content,
                    prompt_filled=json.dumps(
                        {"stage": job.step.name, "unit_id": job.state.context.get("unit_id"), "input_hash": input_hash}
                    ),
                    checked_out_commit=diff["head_commit"],
                    run_started_at=started,
                    model=settings["model"],
                    model_provider="self_hosted",
                    harness="self-hosted",
                    thinking_effort=str(settings.get("thinking_token_budget", "default")),
                )
                if metadata_id is None:
                    conn.commit()
                    return False
                db.update_metadata(
                    conn,
                    metadata_id,
                    status="running",
                    error=None,
                    run_time_ms=0,
                    raw_token_usage=None,
                    phase="running_harness",
                )
                result = _result(diff, workflow, completed, rows, input_hash, settings, active_step=job.step.id)
                if not _save_checkpoint(conn, scan_id, marker, result):
                    return False
                retries = runtime_int(
                    "ENGINE_RETRY_COUNT",
                    getattr(config, "retry_count", 2),
                    data_dir=getattr(config, "data_dir", None),
                    minimum=0,
                    maximum=10,
                )
                for attempt in range(retries + 1):
                    if not _guard(conn, scan_id, marker):
                        break
                    conn.commit()
                    settings = local_settings()
                    if _model_snapshot(settings) != snapshot:
                        raise DefensiveReviewError("Local model settings changed during review. Create a new review.")
                    try:
                        output = run_stage(job, diff, units, settings)
                        break
                    except (SelfHostedConnectionError, SelfHostedInvalidOutputError):
                        if attempt == retries:
                            raise
                        time.sleep(min(2**attempt, 8))
                duration = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                if not _guard(conn, scan_id, marker):
                    db.update_metadata(
                        conn,
                        metadata_id,
                        status="stopped",
                        error="Review stopped before this result was saved.",
                        run_time_ms=duration,
                        raw_token_usage=None,
                        phase="interrupted",
                    )
                    conn.commit()
                    return False
                for row in output:
                    db.insert_step_result(
                        conn,
                        scan_id=scan_id,
                        workflow_id=workflow.id,
                        step_id=job.step.id,
                        depth=job.depth,
                        prev_id=job.state.prev_id,
                        prev_table=job.state.prev_table,
                        repeat_run=job.state.repeat_run,
                        json_answer=row,
                    )
                db.update_metadata(
                    conn,
                    metadata_id,
                    status="completed",
                    error=None,
                    run_time_ms=duration,
                    raw_token_usage=None,
                    phase="completed",
                    output_json={"results": output},
                )
                conn.commit()
                metadata_id = None
        except Exception as exc:
            conn.rollback()
            message = (
                str(exc)
                if isinstance(exc, (DefensiveReviewError, SelfHostedError, CommitDiffError))
                else "The defensive review failed unexpectedly. Check the engine configuration before retrying."
            )
            if metadata_id is not None:
                db.update_metadata(
                    conn,
                    metadata_id,
                    status="failed",
                    error=message,
                    run_time_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                    raw_token_usage=None,
                    phase="failed",
                )
            if _guard(conn, scan_id, marker):
                if result is not None:
                    for stage in result["workflow"]["stages"]:
                        if stage["status"] == "running":
                            stage["status"] = "failed"
                    if not _save_checkpoint(conn, scan_id, marker, result):
                        return False
                    if not _guard(conn, scan_id, marker):
                        return False
                conn.execute(
                    "UPDATE public.scans SET status = 'failed', reasoning = %s, updated_at = now() WHERE id = %s",
                    (Jsonb({"error": message}), scan_id),
                )
            conn.commit()
            return True
        finally:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (-int(scan_id),))
            conn.commit()
