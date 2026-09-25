"""Run bounded source reviews without entering the executable workflow pipeline."""

import fcntl
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from .account_activity import assert_account_assignment
from .commit_diff import CommitDiffError, collect_commit_diff, collect_local_commit_diff
from .provider_credentials import provider_environment
from .repository import REPO_FULL_RE, _authenticated_git_command, _github_auth_environment
from .self_hosted import SelfHostedError, check_connection, review_diff


def settings_root():
    return Path(os.getenv("OPEN_KRITT_PROVIDER_CREDENTIALS_PATH", "/credentials/providers.json")).parent


def read_json(path):
    with path.open("rb") as source:
        data = source.read(32_769)
    if len(data) > 32_768:
        raise ValueError("Self-hosted AI configuration is too large.")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Self-hosted AI configuration is invalid.")
    return value


def local_settings():
    try:
        settings = read_json(settings_root() / "self-hosted.json")
        base_url, model = settings["baseUrl"], settings["model"]
        if not isinstance(base_url, str) or not isinstance(model, str) or not base_url or not model:
            raise ValueError
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError("Configure Self-hosted AI in Accounts before starting a review.") from exc
    env = provider_environment()
    assert_account_assignment("self_hosted", env=env)
    key = env.get("SELF_HOSTED_API_KEY")
    if not key:
        raise ValueError("Configure and activate the Self-hosted AI key in Accounts.")
    return {"base_url": base_url, "model": model, "api_key": key}


def _write_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_connection_check():
    root = settings_root()
    request_path = root / "self-hosted-check-request.json"
    if not request_path.is_file():
        return False
    with (root / "self-hosted-check.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        request = read_json(request_path)
        result_path = root / "self-hosted-check-result.json"
        try:
            if read_json(result_path).get("id") == request.get("id"):
                return False
        except (OSError, ValueError):
            pass
        result = {"id": request.get("id"), "status": "failed"}
        try:
            settings = local_settings()
            if request.get("baseUrl") != settings["base_url"] or request.get("model") != settings["model"]:
                raise ValueError("Configuration changed. Run the connection check again.")
            check_connection(**settings)
            result.update(status="passed")
        except SelfHostedError as exc:
            result.update(error=str(exc), code=exc.code)
        except (ValueError, RuntimeError):
            result.update(
                error="Configure and activate Self-hosted AI in Accounts, then retry the check.", code="configuration"
            )
        result["checkedAt"] = datetime.now(timezone.utc).isoformat()
        _write_json(result_path, result)
        return True


def _remote_repository(repo_full, directory, github_token, commits):
    if not REPO_FULL_RE.fullmatch(repo_full):
        raise ValueError("Select a valid GitHub repository.")
    with _github_auth_environment(github_token) as auth:
        env = dict(auth or {})
        env.update(
            PATH=os.environ.get("PATH", "/usr/bin:/bin"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
        )
        command = _authenticated_git_command(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--bare",
                "--quiet",
                "--no-tags",
                f"https://github.com/{repo_full}.git",
                str(directory),
            ],
            env,
        )
        try:
            subprocess.run(
                command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Could not fetch the GitHub repository. Check access and retry.") from exc
        for commit in commits:
            if not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", commit):
                continue
            # Full IDs can identify objects reachable only through a tag or PR.
            # A failed fetch is resolved into a field error by the collector.
            fetch = _authenticated_git_command(
                ["git", "-c", "core.hooksPath=/dev/null", "fetch", "--quiet", "--no-tags", "origin", commit], env
            )
            try:
                subprocess.run(
                    fetch,
                    cwd=directory,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError("Could not fetch the requested GitHub commits. Check access and retry.") from exc


def prepare_diff(scan, config):
    if scan.get("repo_kind") == "local":
        return collect_local_commit_diff(
            os.getenv("LOCAL_REPOS_PATH", "/local_repos"),
            scan["repo_full"],
            scan["base_commit_sha"],
            scan["commit_sha"],
        )
    with tempfile.TemporaryDirectory(prefix="commit-review-") as temporary:
        source = Path(temporary) / "repository.git"
        _remote_repository(
            scan["repo_full"], source, config.github_token, [scan["base_commit_sha"], scan["commit_sha"]]
        )
        return collect_commit_diff(str(source), scan["base_commit_sha"], scan["commit_sha"])


def process_commit_review(db, config, scan_id):
    # A session lock prevents two engine workers from reviewing the same scan.
    with db.connect() as conn:
        locked = conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (-int(scan_id),)).fetchone()
        if not locked["acquired"]:
            return False
        marker = None
        try:
            scan = db.load_scan(conn, scan_id)
            if not scan or scan["status"] != "running":
                return False
            if scan.get("comparison_mode") != "commits" or scan.get("model_provider") != "self_hosted":
                raise ValueError("This review requires the Self-hosted AI source-review provider.")
            conn.commit()
            marker = scan.get("last_resumed_at")
            settings = local_settings()
            if (
                scan["model"] != settings["model"]
                or scan["configuration"].get("self_hosted_base_url") != settings["base_url"]
            ):
                raise ValueError("Self-hosted AI configuration changed. Create a new review with the current settings.")
            diff = prepare_diff(scan, config)
            row = conn.execute(
                """UPDATE public.scans SET base_commit_sha = %s, commit_sha = %s, updated_at = now()
                   WHERE id = %s AND status = 'running' AND last_resumed_at IS NOT DISTINCT FROM %s
                   RETURNING id""",
                (diff["base_commit"], diff["head_commit"], scan_id, marker),
            ).fetchone()
            conn.commit()
            if not row:
                return False
            findings = []
            if not diff["no_changes"] and diff["files"]:
                settings = local_settings()
                if (
                    settings["base_url"] != scan["configuration"]["self_hosted_base_url"]
                    or settings["model"] != scan["model"]
                ):
                    raise ValueError("Self-hosted AI configuration changed. Create a new review.")
                findings = review_diff(diff, **settings)["findings"]
            result = {key: value for key, value in diff.items() if key != "patch"}
            result["findings"] = findings
            conn.execute(
                """UPDATE public.scans SET extras = CASE WHEN jsonb_typeof(extras) = 'object' THEN extras ELSE '{}'::jsonb END || %s,
                   status = 'completed', reasoning = NULL, updated_at = now()
                   WHERE id = %s AND status = 'running' AND last_resumed_at IS NOT DISTINCT FROM %s""",
                (Jsonb({"diff_review": result}), scan_id, marker),
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            message = (
                str(exc)
                if isinstance(exc, (SelfHostedError, CommitDiffError, ValueError, RuntimeError))
                else "The source review failed unexpectedly. Retry or check the engine logs."
            )
            reasoning = {"error": message}
            if isinstance(exc, CommitDiffError):
                field = exc.field or "repo_full"
                reasoning["errors"] = [{"field": field, "message": message}]
            conn.execute(
                """UPDATE public.scans SET status = 'failed', reasoning = %s, updated_at = now()
                   WHERE id = %s AND status = 'running' AND last_resumed_at IS NOT DISTINCT FROM %s""",
                (Jsonb(reasoning), scan_id, marker),
            )
            conn.commit()
            return True
        finally:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (-int(scan_id),))
            conn.commit()


def fail_commit_review(db, scan):
    with db.connect() as conn:
        conn.execute(
            """UPDATE public.scans SET status = 'failed', reasoning = %s, updated_at = now()
               WHERE id = %s AND status = 'running' AND last_resumed_at IS NOT DISTINCT FROM %s""",
            (
                Jsonb({"error": "The source review failed unexpectedly. Retry or check the engine logs."}),
                int(scan["id"]),
                scan.get("last_resumed_at"),
            ),
        )
        conn.commit()
