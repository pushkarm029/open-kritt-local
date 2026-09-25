import hashlib
import json
import os
import subprocess

import pytest

from open_kritt_engine import commit_diff, self_hosted
from open_kritt_engine.commit_diff import CommitDiffError, collect_commit_diff


def _git(repo, *args, input_bytes=None):
    return (
        subprocess.run(
            ["git", *args],
            cwd=repo,
            input=input_bytes,
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .strip()
    )


def _repo(path):
    path.mkdir()
    _git(path, "init", "--quiet", "-b", "main")
    _git(path, "config", "user.name", "Commit Diff Tests")
    _git(path, "config", "user.email", "commit-diff@example.invalid")
    return path


def _commit(repo, message):
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _changed_lines(ranges):
    return [line for start, end in ranges for line in range(start, end + 1)]


def _assert_batches_cover_changes(result, batch_bytes):
    assert set(result) == {
        "base_commit",
        "head_commit",
        "patch",
        "files",
        "unreviewed",
        "no_changes",
        "batches",
    }
    covered = {}
    for batch in result["batches"]:
        assert set(batch) == set(result) - {"batches"}
        assert len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= batch_bytes
        assert len({item["path"] for item in batch["files"]}) == len(batch["files"])
        self_hosted._validate_diff(batch)
        for item in batch["files"]:
            for side in ("base", "head"):
                key = (item["path"], side)
                covered.setdefault(key, []).extend(_changed_lines(item[f"{side}_changed_lines"]))
    for item in result["files"]:
        for side in ("base", "head"):
            key = (item["path"], side)
            assert sorted(covered.get(key, [])) == _changed_lines(item[f"{side}_changed_lines"])


def test_collects_edit_deletion_and_rename_from_committed_trees(tmp_path):
    repo = _repo(tmp_path / "repo")
    original = "".join(f"line {number}\n" for number in range(1, 25))
    (repo / "edit.txt").write_text(original, encoding="utf-8")
    (repo / "gone.txt").write_text("removed\n", encoding="utf-8")
    (repo / "old-name.txt").write_text("keep this file\n", encoding="utf-8")
    base = _commit(repo, "base")

    (repo / "edit.txt").write_text(original.replace("line 12\n", "updated line 12\n"), encoding="utf-8")
    (repo / "gone.txt").unlink()
    (repo / "old-name.txt").rename(repo / "new-name.txt")
    head = _commit(repo, "head")

    # Dirty work-tree and index state must not enter the comparison or change.
    (repo / "edit.txt").write_text("working tree only\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged only\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    index_before = (repo / ".git" / "index").read_bytes()
    status_before = _git(repo, "status", "--porcelain")

    result = collect_commit_diff(repo, base[:12], head)

    assert result["base_commit"] == base
    assert result["head_commit"] == head
    assert result["no_changes"] is False
    assert result["unreviewed"] == [{"path": "new-name.txt", "reason": "rename has no changed source lines"}]
    entries = {entry["path"]: entry for entry in result["files"]}
    assert entries["edit.txt"] == {
        "path": "edit.txt",
        "base_path": "edit.txt",
        "head_path": "edit.txt",
        "status": "modified",
        "base_lines": 24,
        "head_lines": 24,
        "base_changed_lines": [[12, 12]],
        "head_changed_lines": [[12, 12]],
    }
    assert entries["gone.txt"]["status"] == "deleted"
    assert entries["gone.txt"]["base_changed_lines"] == [[1, 1]]
    assert entries["gone.txt"]["head_changed_lines"] == []
    assert "new-name.txt" not in entries
    assert "+updated line 12" in result["patch"]
    assert "working tree only" not in result["patch"]
    assert "staged only" not in result["patch"]
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert _git(repo, "status", "--porcelain") == status_before


def test_metadata_only_rename_and_mode_change_are_explicitly_unreviewed(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    (repo / "old.txt").write_text("unchanged\n", encoding="utf-8")
    (repo / "mode.txt").write_text("same lines\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "old.txt").rename(repo / "new.txt")
    (repo / "mode.txt").chmod(0o755)
    head = _commit(repo, "metadata only")

    result = collect_commit_diff(repo, base, head)

    assert result["no_changes"] is False
    assert result["files"] == []
    assert result["patch"] == ""
    assert {item["path"]: item["reason"] for item in result["unreviewed"]} == {
        "mode.txt": "change has no source lines to review",
        "new.txt": "rename has no changed source lines",
    }
    monkeypatch.setattr(self_hosted, "_post_chat_completion", lambda **_kwargs: pytest.fail("unexpected inference"))
    assert self_hosted.review_diff(result, base_url="https://unused.example/v1", model="m", api_key="k") == {
        "findings": []
    }


def test_metadata_only_large_rename_does_not_read_source_blob(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "old.txt").write_text("source\n" * 180_000, encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "old.txt").rename(repo / "new.txt")
    head = _commit(repo, "rename")

    result = collect_commit_diff(repo, base, head)

    assert result["files"] == []
    assert result["unreviewed"] == [{"path": "new.txt", "reason": "rename has no changed source lines"}]


def test_rename_with_changed_source_remains_reviewable(tmp_path):
    repo = _repo(tmp_path / "repo")
    original = "".join(f"line {number}\n" for number in range(30))
    (repo / "old.txt").write_text(original, encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "old.txt").rename(repo / "new.txt")
    (repo / "new.txt").write_text(original.replace("line 15\n", "changed line 15\n"), encoding="utf-8")
    head = _commit(repo, "rename and edit")

    result = collect_commit_diff(repo, base, head)

    assert result["unreviewed"] == []
    assert result["files"][0]["status"] == "renamed"
    assert result["files"][0]["base_path"] == "old.txt"
    assert result["files"][0]["head_path"] == "new.txt"
    assert result["files"][0]["head_changed_lines"] == [[16, 16]]
    assert "+changed line 15" in result["patch"]


def test_identical_trees_are_explicit_and_abbreviations_resolve(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "file.txt").write_text("same\n", encoding="utf-8")
    base = _commit(repo, "base")
    head = _git(repo, "commit-tree", f"{base}^{{tree}}", "-m", "same tree")

    result = collect_commit_diff(repo, base[:9], head[:9])

    assert result == {
        "base_commit": base,
        "head_commit": head,
        "patch": "",
        "files": [],
        "unreviewed": [],
        "no_changes": True,
    }

    batched = collect_commit_diff(repo, base, head, batch_bytes=120_000)
    assert batched == {**result, "batches": []}


def test_batch_mode_keeps_default_diff_contract_for_small_change(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "code.py").write_text("before\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "code.py").write_text("after\n", encoding="utf-8")
    head = _commit(repo, "edit")

    ordinary = collect_commit_diff(repo, base, head)
    batched = collect_commit_diff(repo, base, head, batch_bytes=120_000)

    assert set(ordinary) == {"base_commit", "head_commit", "patch", "files", "unreviewed", "no_changes"}
    assert {key: value for key, value in batched.items() if key != "batches"} == ordinary
    assert len(batched["batches"]) == 1
    _assert_batches_cover_changes(batched, 120_000)


def test_batch_mode_packs_more_than_one_hundred_small_files(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    for number in range(161):
        (repo / f"file-{number:03d}.txt").write_text(f"line {number}\n", encoding="utf-8")
    head = _commit(repo, "many files")

    with pytest.raises(CommitDiffError, match="file limit"):
        collect_commit_diff(repo, base, head)
    result = collect_commit_diff(repo, base, head, max_files=200, max_bytes=8 * 1024 * 1024, batch_bytes=6000)

    assert len(result["files"]) == 161
    assert len(result["batches"]) > 1
    assert any(len(batch["files"]) > 1 for batch in result["batches"])
    _assert_batches_cover_changes(result, 6000)


def test_batch_mode_splits_large_addition_without_losing_lines(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "new.txt").write_text("".join(f"added {number:05d}\n" for number in range(18_000)), encoding="utf-8")
    head = _commit(repo, "large addition")

    result = collect_commit_diff(repo, base, head, max_bytes=8 * 1024 * 1024, batch_bytes=40_000)

    assert len(result["batches"]) > 1
    assert all(batch["files"][0]["path"] == "new.txt" for batch in result["batches"])
    _assert_batches_cover_changes(result, 40_000)


def test_batch_mode_splits_large_replacement_on_both_sides(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "code.txt").write_text("".join(f"old {number:05d}\n" for number in range(8000)), encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "code.txt").write_text("".join(f"new {number:05d}\n" for number in range(8000)), encoding="utf-8")
    head = _commit(repo, "replace")

    result = collect_commit_diff(repo, base, head, max_bytes=8 * 1024 * 1024, batch_bytes=30_000)

    assert len(result["batches"]) > 1
    _assert_batches_cover_changes(result, 30_000)
    assert _changed_lines(result["files"][0]["base_changed_lines"]) == list(range(1, 8001))
    assert _changed_lines(result["files"][0]["head_changed_lines"]) == list(range(1, 8001))


def test_batch_mode_drops_oversized_context_but_keeps_changed_line(tmp_path):
    repo = _repo(tmp_path / "repo")
    context = "c" * 130_000 + "\n"
    (repo / "code.txt").write_text(context + "old\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "code.txt").write_text(context + "new\n", encoding="utf-8")
    head = _commit(repo, "edit")

    result = collect_commit_diff(repo, base, head, max_bytes=8 * 1024 * 1024, batch_bytes=120_000)

    assert len(result["batches"]) == 1
    assert "+new" in result["batches"][0]["patch"]
    assert context not in result["batches"][0]["patch"]
    _assert_batches_cover_changes(result, 120_000)


def test_batch_mode_retains_rename_and_deletion_sides(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "old.txt").write_text("".join(f"keep {number}\n" for number in range(50)), encoding="utf-8")
    (repo / "deleted.txt").write_text("".join(f"remove {number}\n" for number in range(200)), encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "old.txt").rename(repo / "new.txt")
    (repo / "new.txt").write_text("".join(f"keep {number}\n" for number in range(49)) + "changed\n", encoding="utf-8")
    (repo / "deleted.txt").unlink()
    head = _commit(repo, "rename and delete")

    result = collect_commit_diff(repo, base, head, max_bytes=8 * 1024 * 1024, batch_bytes=1100)

    assert {item["status"] for item in result["files"]} == {"renamed", "deleted"}
    assert len(result["batches"]) > 1
    _assert_batches_cover_changes(result, 1100)
    assert any(item["base_changed_lines"] for batch in result["batches"] for item in batch["files"])


def test_batch_mode_rejects_one_line_that_cannot_fit(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "large.txt").write_text("x" * 130_000 + "\n", encoding="utf-8")
    head = _commit(repo, "long line")

    with pytest.raises(CommitDiffError, match="changed source line or its file metadata"):
        collect_commit_diff(repo, base, head, max_bytes=8 * 1024 * 1024, batch_bytes=120_000)


def test_unrelated_commits_compare_directly_and_reverse_cleanly(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "left.txt").write_text("left\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git(repo, "checkout", "--quiet", "--orphan", "unrelated")
    _git(repo, "rm", "-rf", "--quiet", ".")
    (repo / "right.txt").write_text("right\n", encoding="utf-8")
    head = _commit(repo, "unrelated")

    forward = collect_commit_diff(repo, base, head)
    reverse = collect_commit_diff(repo, head, base)

    assert {entry["path"]: entry["status"] for entry in forward["files"]} == {
        "left.txt": "deleted",
        "right.txt": "added",
    }
    assert {entry["path"]: entry["status"] for entry in reverse["files"]} == {
        "left.txt": "added",
        "right.txt": "deleted",
    }
    assert forward["base_commit"] == base and forward["head_commit"] == head
    assert reverse["base_commit"] == head and reverse["head_commit"] == base


def test_ignores_replacement_refs_and_local_external_diff_configuration(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt diff=review-test\n", encoding="utf-8")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "file.txt").write_text("head\n", encoding="utf-8")
    head = _commit(repo, "head")
    replacement = _git(repo, "commit-tree", f"{head}^{{tree}}", "-m", "replacement")
    _git(repo, "replace", base, replacement)

    marker = tmp_path / "external-command-ran"
    script = tmp_path / "external-diff"
    script.write_text(f"#!/bin/sh\nprintf invoked > '{marker}'\n", encoding="utf-8")
    script.chmod(0o700)
    _git(repo, "config", "diff.external", str(script))
    _git(repo, "config", "diff.review-test.textconv", str(script))
    hook_dir = repo / ".git" / "hooks"
    hook_dir.mkdir(exist_ok=True)
    hook = hook_dir / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf invoked > '{marker}'\n", encoding="utf-8")
    hook.chmod(0o700)
    _git(repo, "config", "core.hooksPath", str(hook_dir))

    result = collect_commit_diff(repo, base, head)

    assert result["no_changes"] is False
    assert result["files"][0]["status"] == "modified"
    assert "+head" in result["patch"]
    assert not marker.exists()


def test_rejects_branches_tags_blobs_missing_and_ambiguous_ids(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git(repo, "tag", "--annotate", "release", "--message", "release")
    annotated_tag = _git(repo, "rev-parse", "release^{tag}")
    blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=b"blob")

    for invalid in ("main", "release", "f" * 40):
        with pytest.raises(CommitDiffError):
            collect_commit_diff(repo, invalid, base)
    with pytest.raises(CommitDiffError, match="hexadecimal"):
        collect_commit_diff(repo, "abcdef", base)
    with pytest.raises(CommitDiffError, match="not a commit"):
        collect_commit_diff(repo, blob, base)
    with pytest.raises(CommitDiffError, match="not a commit"):
        collect_commit_diff(repo, annotated_tag, base)

    seen = {}
    colliding_contents = None
    for number in range(100_000):
        content = f"collision-candidate-{number:06d}".encode()
        object_id = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        prefix = object_id[:7]
        if prefix in seen:
            colliding_contents = (seen[prefix], content)
            break
        seen[prefix] = content
    assert colliding_contents is not None
    for content in colliding_contents:
        _git(repo, "hash-object", "-w", "--stdin", input_bytes=content)
    with pytest.raises(CommitDiffError, match="ambiguous"):
        collect_commit_diff(repo, object_id[:7], base)


@pytest.mark.parametrize(("invalid_side", "field"), [("base", "base_commit_sha"), ("head", "commit_sha")])
def test_missing_commit_ids_include_the_matching_field(tmp_path, invalid_side, field):
    repo = _repo(tmp_path / "repo")
    (repo / "file.txt").write_text("source\n", encoding="utf-8")
    commit = _commit(repo, "base")
    missing = "f" * 40

    with pytest.raises(CommitDiffError) as error:
        collect_commit_diff(
            repo,
            missing if invalid_side == "base" else commit,
            missing if invalid_side == "head" else commit,
        )

    assert error.value.field == field


@pytest.mark.parametrize(("failing_side", "field"), [("base", "base_commit_sha"), ("head", "commit_sha")])
def test_tree_read_errors_include_the_matching_field(tmp_path, monkeypatch, failing_side, field):
    repo = _repo(tmp_path / "repo")
    (repo / "file.txt").write_text("source\n", encoding="utf-8")
    commit = _commit(repo, "base")
    read_tree = commit_diff._tree_id

    def fail_selected_tree(repo_dir, object_id, label, deadline, location=None):
        if label == failing_side:
            raise CommitDiffError("tree read failed")
        return read_tree(repo_dir, object_id, label, deadline, location)

    monkeypatch.setattr(commit_diff, "_tree_id", fail_selected_tree)
    with pytest.raises(CommitDiffError, match="tree read failed") as error:
        collect_commit_diff(repo, commit, commit)

    assert error.value.field == field


def test_binary_symlink_and_submodule_changes_are_unreviewed(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "binary.dat").write_bytes(b"plain\n")
    base = _commit(repo, "base")

    (repo / "binary.dat").write_bytes(b"\x00binary\x00")
    os.symlink("target", repo / "link")
    _commit(repo, "unsupported files")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},submodule")
    _git(repo, "commit", "--quiet", "-m", "submodule")
    head = _git(repo, "rev-parse", "HEAD")

    result = collect_commit_diff(repo, base, head)

    assert result["files"] == []
    assert {item["path"]: item["reason"] for item in result["unreviewed"]} == {
        "binary.dat": "binary content is unsupported",
        "link": "symlink changes are unsupported",
        "submodule": "submodule changes are unsupported",
    }


def test_large_binary_is_skipped_without_blocking_small_text_edit(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "code.py").write_text("safe = True\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "code.py").write_text("safe = False\n", encoding="utf-8")
    (repo / "image.bin").write_bytes(b"\0" + b"x" * (1024 * 1024 + 1))
    head = _commit(repo, "text and binary")

    result = collect_commit_diff(repo, base, head)

    assert [item["path"] for item in result["files"]] == ["code.py"]
    assert result["unreviewed"] == [{"path": "image.bin", "reason": "binary content is unsupported"}]
    assert "+safe = False" in result["patch"]


def test_invalid_utf8_text_and_path_are_reported_without_lossy_review(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "text.txt").write_bytes(b"valid\n")
    base = _commit(repo, "base")
    (repo / "text.txt").write_bytes(b"invalid \xff text\n")
    invalid_path = os.fsencode(repo) + b"/path-\xff.txt"
    invalid_path_supported = True
    try:
        fd = os.open(invalid_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        invalid_path_supported = False
    else:
        try:
            os.write(fd, b"text\n")
        finally:
            os.close(fd)
    head = _commit(repo, "invalid UTF-8")

    result = collect_commit_diff(repo, base, head)

    assert result["files"] == []
    reasons = {item["reason"] for item in result["unreviewed"]}
    assert "text is not valid UTF-8" in reasons
    if invalid_path_supported:
        assert "path is not valid UTF-8" in reasons
        assert any("\\xff" in item["path"] for item in result["unreviewed"])


def test_file_and_byte_limits_fail_whole_comparison(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "one.txt").write_text("old\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "one.txt").write_text("new content\n", encoding="utf-8")
    (repo / "two.txt").write_text("added\n", encoding="utf-8")
    head = _commit(repo, "head")

    with pytest.raises(CommitDiffError, match="file limit"):
        collect_commit_diff(repo, base, head, max_files=1)
    with pytest.raises(CommitDiffError, match="byte limit"):
        collect_commit_diff(repo, base, head, max_bytes=4)


def test_small_patch_in_large_text_file_uses_review_input_limit(tmp_path):
    repo = _repo(tmp_path / "repo")
    lines = [f"line {number:05d}\n" for number in range(8000)]
    (repo / "code.txt").write_text("".join(lines), encoding="utf-8")
    base = _commit(repo, "base")
    lines[4000] = "changed line 04000\n"
    (repo / "code.txt").write_text("".join(lines), encoding="utf-8")
    head = _commit(repo, "one line")

    result = collect_commit_diff(repo, base, head)

    assert len(result["patch"].encode("utf-8")) < 1000
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 120_000
    assert result["files"][0]["head_changed_lines"] == [[4001, 4001]]


def test_source_object_processing_limit_is_separate_from_review_input(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "large.txt").write_text("a" * (1024 * 1024 + 1), encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "large.txt").write_text("a" * (1024 * 1024) + "b", encoding="utf-8")
    head = _commit(repo, "edit")

    with pytest.raises(CommitDiffError, match="1 MiB processing limit"):
        collect_commit_diff(repo, base, head)


def test_total_source_processing_limit_is_bounded_across_files(tmp_path):
    repo = _repo(tmp_path / "repo")
    for number in range(9):
        (repo / f"file-{number}.txt").write_text(str(number) * 480_000, encoding="utf-8")
    base = _commit(repo, "base")
    for number in range(9):
        (repo / f"file-{number}.txt").write_text(str(number) * 479_999 + "x", encoding="utf-8")
    head = _commit(repo, "edit")

    with pytest.raises(CommitDiffError, match="8 MiB source processing limit"):
        collect_commit_diff(repo, base, head)


def test_serialized_review_input_includes_metadata_in_byte_limit(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "initial.txt").write_text("initial\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "new.txt").write_text("source\n", encoding="utf-8")
    head = _commit(repo, "add")
    result = collect_commit_diff(repo, base, head)
    patch_bytes = len(result["patch"].encode("utf-8"))

    with pytest.raises(CommitDiffError, match="review input byte limit"):
        collect_commit_diff(repo, base, head, max_bytes=patch_bytes + 1)


def test_local_collector_uses_pinned_repository_and_git_directory_descriptors(tmp_path, monkeypatch):
    root = tmp_path / "repositories"
    root.mkdir()
    repo = _repo(root / "selected")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "file.txt").write_text("selected head\n", encoding="utf-8")
    head = _commit(repo, "head")

    decoy = _repo(root / "decoy")
    (decoy / "file.txt").write_text("decoy content\n", encoding="utf-8")
    _commit(decoy, "decoy")
    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(root, target_is_directory=True)

    run_git = commit_diff._run_git
    swapped = False

    def swap_path_before_first_git(repo_dir, args, **kwargs):
        nonlocal swapped
        if not swapped:
            pinned_repo = root / "selected-pinned"
            repo.rename(pinned_repo)
            git_entry = pinned_repo / ".git"
            git_entry.rename(pinned_repo / ".git-pinned")
            git_entry.symlink_to(decoy / ".git", target_is_directory=True)
            (root / "selected").symlink_to(decoy, target_is_directory=True)
            swapped = True
        return run_git(repo_dir, args, **kwargs)

    monkeypatch.setattr(commit_diff, "_run_git", swap_path_before_first_git)
    result = commit_diff.collect_local_commit_diff(root_alias, "selected", base, head)

    assert swapped
    assert result["files"][0]["path"] == "file.txt"
    assert "+selected head" in result["patch"]
    assert "decoy content" not in result["patch"]


def test_local_collector_passes_batch_budget_through_pinned_directory(tmp_path):
    root = tmp_path / "repositories"
    root.mkdir()
    repo = _repo(root / "selected")
    (repo / "code.txt").write_text("before\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "code.txt").write_text("after\n", encoding="utf-8")
    head = _commit(repo, "head")

    result = commit_diff.collect_local_commit_diff(root, "selected", base, head, batch_bytes=120_000)

    assert len(result["batches"]) == 1
    _assert_batches_cover_changes(result, 120_000)


def test_local_collector_rejects_repository_symlink_git_symlink_and_linked_worktree(tmp_path):
    root = tmp_path / "repositories"
    root.mkdir()
    outside = _repo(tmp_path / "outside")
    (outside / "file.txt").write_text("content\n", encoding="utf-8")
    commit = _commit(outside, "outside")
    (root / "repo-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CommitDiffError):
        commit_diff.collect_local_commit_diff(root, "repo-link", commit, commit)

    repo = _repo(root / "git-link")
    (repo / "file.txt").write_text("content\n", encoding="utf-8")
    local_commit = _commit(repo, "base")
    (repo / ".git").rename(repo / "git-dir")
    (repo / ".git").symlink_to(repo / "git-dir", target_is_directory=True)
    with pytest.raises(CommitDiffError, match="symlink"):
        commit_diff.collect_local_commit_diff(root, "git-link", local_commit, local_commit)

    linked = root / "linked-worktree"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    with pytest.raises(CommitDiffError, match="Linked worktrees are not supported"):
        commit_diff.collect_local_commit_diff(root, "linked-worktree", local_commit, local_commit)
