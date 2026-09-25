"""Read-only, bounded comparison of two committed Git trees."""

from __future__ import annotations

import difflib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class CommitDiffError(ValueError):
    """A safe, user-facing error while collecting a commit comparison."""

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


_COMMIT_ID_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_FULL_COMMIT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ZERO_OBJECT_ID_RE = re.compile(r"^0+$")
_MAX_BYTES_LIMIT = 8 * 1024 * 1024
_MAX_FILES_LIMIT = 1000
_COMMAND_TIMEOUT_SECONDS = 10.0
_TOTAL_TIMEOUT_SECONDS = 30.0
_STDERR_LIMIT = 4096


@dataclass(frozen=True)
class _Change:
    status_code: str
    old_mode: str
    new_mode: str
    old_object: str
    new_object: str
    old_path: bytes
    new_path: bytes


@dataclass(frozen=True)
class _GitLocation:
    git_dir_fd: int


_FD_EXEC_BOOTSTRAP = "import os,sys; os.fchdir(int(sys.argv[1])); os.execv(sys.argv[2],sys.argv[2:])"


def _git_environment() -> dict[str, str]:
    """Return a deliberately small environment without user Git settings."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.devnull,
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _run_git(
    repo_dir: str | os.PathLike[str],
    args: list[str],
    *,
    stdout_limit: int,
    deadline: float,
    location: _GitLocation | None = None,
) -> bytes:
    git = shutil.which("git")
    if not git:
        raise CommitDiffError("Git is unavailable")

    command = [
        git,
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=/dev/null",
        *args,
    ]
    if location is not None:
        command = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _FD_EXEC_BOOTSTRAP,
            str(location.git_dir_fd),
            *command,
        ]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CommitDiffError("Git comparison exceeded its time limit")
    timeout = min(_COMMAND_TIMEOUT_SECONDS, remaining)

    environment = _git_environment()
    if location is not None:
        environment["GIT_DIR"] = "."

    try:
        process = subprocess.Popen(
            command,
            cwd="/" if location is not None else repo_dir,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            bufsize=0,
            pass_fds=(location.git_dir_fd,) if location is not None else (),
        )
    except (OSError, ValueError) as exc:
        raise CommitDiffError("could not start Git comparison") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    selector = selectors.DefaultSelector()
    buffers: dict[int, bytearray] = {
        stdout_fd: bytearray(),
        stderr_fd: bytearray(),
    }
    limits = {
        stdout_fd: stdout_limit,
        stderr_fd: _STDERR_LIMIT,
    }
    streams = {process.stdout.fileno(): process.stdout, process.stderr.fileno(): process.stderr}
    try:
        for fd, stream in streams.items():
            selector.register(stream, selectors.EVENT_READ, fd)

        while selector.get_map():
            remaining = min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                raise CommitDiffError("Git comparison exceeded its time limit")
            for key, _ in selector.select(min(remaining, 0.2)):
                fd = key.data
                chunk = os.read(fd, min(65536, limits[fd] - len(buffers[fd]) + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffers[fd].extend(chunk)
                if len(buffers[fd]) > limits[fd]:
                    raise CommitDiffError("Git comparison output exceeds its configured limit")

        remaining = min(timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise CommitDiffError("Git comparison exceeded its time limit")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise CommitDiffError("Git comparison exceeded its time limit") from exc
        if return_code != 0:
            raise CommitDiffError("Git could not read the requested commit data")
        return bytes(buffers[stdout_fd])
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in streams.values():
            if not stream.closed:
                stream.close()


def _small_git_output(
    repo_dir: str | os.PathLike[str],
    args: list[str],
    deadline: float,
    location: _GitLocation | None = None,
) -> str:
    output = _run_git(repo_dir, args, stdout_limit=256, deadline=deadline, location=location)
    try:
        return output.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise CommitDiffError("Git returned invalid object metadata") from exc


def _resolve_commit(
    repo_dir: str | os.PathLike[str],
    value: str,
    label: str,
    deadline: float,
    location: _GitLocation | None = None,
) -> str:
    if not isinstance(value, str) or not _COMMIT_ID_RE.fullmatch(value):
        raise CommitDiffError(f"{label} must be a hexadecimal commit object ID")
    prefix = value.lower()
    matches = _run_git(
        repo_dir,
        ["rev-parse", f"--disambiguate={prefix}"],
        stdout_limit=4096,
        deadline=deadline,
        location=location,
    )
    object_ids = [line.decode("ascii", errors="ignore") for line in matches.splitlines()]
    object_ids = [item for item in object_ids if _FULL_COMMIT_ID_RE.fullmatch(item)]
    if not object_ids:
        raise CommitDiffError(f"{label} commit object was not found")
    if len(object_ids) != 1:
        raise CommitDiffError(f"{label} commit object ID is ambiguous")
    object_id = object_ids[0]
    if len(prefix) == len(object_id) and prefix != object_id:
        raise CommitDiffError(f"{label} commit object was not found")
    if not object_id.startswith(prefix):
        raise CommitDiffError(f"{label} commit object was not found")
    object_type = _small_git_output(repo_dir, ["cat-file", "-t", object_id], deadline, location)
    if object_type != "commit":
        raise CommitDiffError(f"{label} object is not a commit")
    return object_id


def _tree_id(
    repo_dir: str | os.PathLike[str],
    commit: str,
    label: str,
    deadline: float,
    location: _GitLocation | None = None,
) -> str:
    tree = _small_git_output(repo_dir, ["rev-parse", "--verify", f"{commit}^{{tree}}"], deadline, location)
    if not _FULL_COMMIT_ID_RE.fullmatch(tree):
        raise CommitDiffError(f"could not read the {label} commit tree")
    return tree


def _parse_raw_diff(output: bytes) -> list[_Change]:
    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    changes: list[_Change] = []
    index = 0
    while index < len(fields):
        metadata = fields[index].split()
        index += 1
        if len(metadata) != 5 or not metadata[0].startswith(b":"):
            raise CommitDiffError("Git returned invalid change metadata")
        try:
            old_mode = metadata[0][1:].decode("ascii")
            new_mode = metadata[1].decode("ascii")
            old_object = metadata[2].decode("ascii")
            new_object = metadata[3].decode("ascii")
            status = metadata[4].decode("ascii")
        except UnicodeDecodeError as exc:
            raise CommitDiffError("Git returned invalid change metadata") from exc
        path_count = 2 if status[:1] in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise CommitDiffError("Git returned incomplete path metadata")
        first_path = fields[index]
        index += 1
        second_path = fields[index] if path_count == 2 else first_path
        if path_count == 2:
            index += 1
        if status.startswith("D"):
            old_path, new_path = first_path, b""
        elif status.startswith("A"):
            old_path, new_path = b"", first_path
        elif path_count == 2:
            old_path, new_path = first_path, second_path
        else:
            old_path = new_path = first_path
        changes.append(_Change(status, old_mode, new_mode, old_object, new_object, old_path, new_path))
    return changes


def _display_path(path: bytes) -> str:
    return path.decode("utf-8", errors="backslashreplace")


def _status_name(change: _Change) -> str | None:
    if change.status_code.startswith("A"):
        return "added"
    if change.status_code.startswith("D"):
        return "deleted"
    if change.status_code.startswith("R"):
        return "renamed"
    if change.status_code == "M":
        return "modified"
    return None


def _unsupported_reason(change: _Change) -> str | None:
    if change.old_mode == "160000" or change.new_mode == "160000":
        return "submodule changes are unsupported"
    if change.old_mode == "120000" or change.new_mode == "120000":
        return "symlink changes are unsupported"
    if (
        change.status_code.startswith("T")
        or change.old_mode not in {"000000", "100644", "100755"}
        or (change.new_mode not in {"000000", "100644", "100755"})
    ):
        return "file type changes are unsupported"
    if _status_name(change) is None:
        return "change type is unsupported"
    if (change.old_path and not _is_utf8(change.old_path)) or (change.new_path and not _is_utf8(change.new_path)):
        return "path is not valid UTF-8"
    return None


def _is_utf8(value: bytes) -> bool:
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _object_size(
    repo_dir: str | os.PathLike[str],
    object_id: str,
    deadline: float,
    location: _GitLocation | None = None,
) -> int:
    if _ZERO_OBJECT_ID_RE.fullmatch(object_id):
        return 0
    value = _small_git_output(repo_dir, ["cat-file", "-s", object_id], deadline, location)
    if not value.isdecimal():
        raise CommitDiffError("Git returned invalid object size metadata")
    return int(value)


def _read_blob(
    repo_dir: str | os.PathLike[str],
    object_id: str,
    expected_size: int,
    deadline: float,
    location: _GitLocation | None = None,
) -> bytes:
    if _ZERO_OBJECT_ID_RE.fullmatch(object_id):
        return b""
    content = _run_git(
        repo_dir,
        ["cat-file", "blob", object_id],
        stdout_limit=expected_size,
        deadline=deadline,
        location=location,
    )
    if len(content) != expected_size:
        raise CommitDiffError("Git returned an incomplete source object")
    return content


def _source_lines(text: str) -> list[str]:
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _changed_ranges(opcodes: list[tuple[str, int, int, int, int]], side: str) -> list[list[int]]:
    intervals: list[tuple[int, int]] = []
    for tag, base_start, base_end, head_start, head_end in opcodes:
        if tag == "equal":
            continue
        start, end = (base_start, base_end) if side == "base" else (head_start, head_end)
        if end > start:
            intervals.append((start + 1, end))
    ranges: list[list[int]] = []
    for start, end in intervals:
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1][1] = end
        else:
            ranges.append([start, end])
    return ranges


def _patch_path(prefix: str, path: str) -> str:
    value = f"{prefix}/{path}"
    if any(char in value for char in '\\"\t\r\n') or any(ord(char) < 32 for char in value):
        return json.dumps(value, ensure_ascii=True)
    return value


def _unified_patch(old_text: str, new_text: str, old_path: str, new_path: str) -> str:
    lines = list(
        difflib.unified_diff(
            _source_lines(old_text),
            _source_lines(new_text),
            fromfile=_patch_path("a", old_path),
            tofile=_patch_path("b", new_path),
            n=10,
            lineterm="\n",
        )
    )
    patch: list[str] = []
    for line in lines:
        patch.append(line)
        if not line.endswith("\n"):
            patch.append("\n\\ No newline at end of file\n")
    return "".join(patch)


def collect_commit_diff(
    repo_dir: str | os.PathLike[str],
    base_commit: str,
    head_commit: str,
    *,
    max_bytes: int = 120_000,
    max_files: int = 100,
) -> dict:
    """Compare explicit committed object IDs in an existing Git directory."""

    return _collect_commit_diff(repo_dir, base_commit, head_commit, max_bytes=max_bytes, max_files=max_files)


def collect_local_commit_diff(
    source_root: str | os.PathLike[str],
    repo_name: str,
    base_commit: str,
    head_commit: str,
    *,
    max_bytes: int = 120_000,
    max_files: int = 100,
) -> dict:
    """Compare commits from a repository pinned beneath a configured local root.

    The configured root may intentionally be a symlink, so it is resolved once.
    The repository and its ``.git`` directory are opened relative to pinned
    directory descriptors, then Git receives only inherited descriptor paths.
    """

    if (
        not isinstance(repo_name, str)
        or not repo_name
        or repo_name in {".", ".."}
        or "/" in repo_name
        or "\0" in repo_name
    ):
        raise CommitDiffError("repository must be a direct child of the configured local repository root")
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise CommitDiffError("secure local repository access is unavailable on this platform")

    root_fd = repo_fd = git_dir_fd = None
    try:
        root = Path(source_root).resolve(strict=True)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root, directory_flags)
        repo_fd = os.open(repo_name, directory_flags, dir_fd=root_fd)
        try:
            git_info = os.stat(".git", dir_fd=repo_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise CommitDiffError("select a Git repository with a .git directory") from exc
        if stat.S_ISREG(git_info.st_mode):
            raise CommitDiffError("Linked worktrees are not supported; select a repository with a .git directory")
        if not stat.S_ISDIR(git_info.st_mode):
            raise CommitDiffError("repository .git must be a real directory, not a symlink or file")
        git_dir_fd = os.open(".git", directory_flags, dir_fd=repo_fd)

        location = _GitLocation(git_dir_fd=git_dir_fd)
        return _collect_commit_diff(
            "/",
            base_commit,
            head_commit,
            max_bytes=max_bytes,
            max_files=max_files,
            location=location,
        )
    except CommitDiffError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CommitDiffError("could not safely open the selected local Git repository") from exc
    finally:
        for fd in (git_dir_fd, repo_fd, root_fd):
            if fd is not None:
                os.close(fd)


def _collect_commit_diff(
    repo_dir: str | os.PathLike[str],
    base_commit: str,
    head_commit: str,
    *,
    max_bytes: int,
    max_files: int,
    location: _GitLocation | None = None,
) -> dict:
    """Return bounded source changes between two explicit commit object IDs.

    Only committed Git objects are read. The work tree, index, refs, and Git
    configuration are not modified. Oversized comparisons fail as a whole so
    callers never mistake a truncated patch for complete review input.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= _MAX_BYTES_LIMIT:
        raise CommitDiffError(f"max_bytes must be between 1 and {_MAX_BYTES_LIMIT}")
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= _MAX_FILES_LIMIT:
        raise CommitDiffError(f"max_files must be between 1 and {_MAX_FILES_LIMIT}")
    if location is None and not Path(repo_dir).is_dir():
        raise CommitDiffError("repository directory is unavailable")

    deadline = time.monotonic() + _TOTAL_TIMEOUT_SECONDS
    try:
        base = _resolve_commit(repo_dir, base_commit, "base", deadline, location)
        base_tree = _tree_id(repo_dir, base, "base", deadline, location)
    except CommitDiffError as exc:
        raise CommitDiffError(str(exc), field="base_commit_sha") from exc
    try:
        head = _resolve_commit(repo_dir, head_commit, "head", deadline, location)
        head_tree = _tree_id(repo_dir, head, "head", deadline, location)
    except CommitDiffError as exc:
        raise CommitDiffError(str(exc), field="commit_sha") from exc
    if base_tree == head_tree:
        return {
            "base_commit": base,
            "head_commit": head,
            "patch": "",
            "files": [],
            "unreviewed": [],
            "no_changes": True,
        }

    raw_limit = max_files * 8400 + 1
    raw_output = _run_git(
        repo_dir,
        [
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "--find-renames=50%",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            base,
            head,
            "--",
        ],
        stdout_limit=raw_limit,
        deadline=deadline,
        location=location,
    )
    changes = _parse_raw_diff(raw_output)
    if len(changes) > max_files:
        raise CommitDiffError("comparison exceeds the configured file limit")

    unreviewed: list[dict[str, str]] = []
    candidates: list[_Change] = []
    for change in changes:
        reason = _unsupported_reason(change)
        if reason:
            path = change.new_path or change.old_path
            unreviewed.append({"path": _display_path(path), "reason": reason})
        else:
            candidates.append(change)

    sizes: dict[str, int] = {}
    source_bytes = 0
    for change in candidates:
        for object_id in (change.old_object, change.new_object):
            if _ZERO_OBJECT_ID_RE.fullmatch(object_id):
                continue
            if object_id not in sizes:
                sizes[object_id] = _object_size(repo_dir, object_id, deadline, location)
            source_bytes += sizes[object_id]
            if source_bytes > max_bytes:
                raise CommitDiffError("comparison exceeds the configured source byte limit")

    blobs: dict[str, bytes] = {}
    for object_id, size in sizes.items():
        blobs[object_id] = _read_blob(repo_dir, object_id, size, deadline, location)

    files: list[dict] = []
    patch_parts: list[str] = []
    for change in candidates:
        old_bytes = b"" if _ZERO_OBJECT_ID_RE.fullmatch(change.old_object) else blobs[change.old_object]
        new_bytes = b"" if _ZERO_OBJECT_ID_RE.fullmatch(change.new_object) else blobs[change.new_object]
        if b"\0" in old_bytes or b"\0" in new_bytes:
            unreviewed.append(
                {"path": _display_path(change.new_path or change.old_path), "reason": "binary content is unsupported"}
            )
            continue
        try:
            old_text = old_bytes.decode("utf-8")
            new_text = new_bytes.decode("utf-8")
        except UnicodeDecodeError:
            unreviewed.append(
                {"path": _display_path(change.new_path or change.old_path), "reason": "text is not valid UTF-8"}
            )
            continue

        old_path = _display_path(change.old_path) if change.old_path else None
        new_path = _display_path(change.new_path) if change.new_path else None
        status = _status_name(change)
        assert status is not None
        old_lines = _source_lines(old_text)
        new_lines = _source_lines(new_text)
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
        opcodes = matcher.get_opcodes()
        file_patch = _unified_patch(old_text, new_text, old_path or "", new_path or "")
        patch_parts.append(file_patch)
        files.append(
            {
                "path": new_path or old_path,
                "base_path": old_path,
                "head_path": new_path,
                "status": status,
                "base_lines": len(old_lines),
                "head_lines": len(new_lines),
                "base_changed_lines": _changed_ranges(opcodes, "base"),
                "head_changed_lines": _changed_ranges(opcodes, "head"),
            }
        )

    patch = "".join(patch_parts)
    if len(patch.encode("utf-8")) > max_bytes:
        raise CommitDiffError("comparison exceeds the configured patch byte limit")
    return {
        "base_commit": base,
        "head_commit": head,
        "patch": patch,
        "files": files,
        "unreviewed": unreviewed,
        "no_changes": False,
    }
