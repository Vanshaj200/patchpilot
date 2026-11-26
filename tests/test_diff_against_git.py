"""Check the parser against a real repository, using the files themselves as ground truth."""

import os
import subprocess
from pathlib import Path

from patchpilot.diff import ChangeType, parse_diff

# Ignore the machine's git settings so the diff looks the same everywhere.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args]
    result = subprocess.run(command, cwd=repo, env=GIT_ENV, check=True, capture_output=True)
    # Decode by hand: text=True would turn CRLF inside the diff into LF.
    return result.stdout.decode("utf-8")


def write(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


def numbered(count: int) -> str:
    return "".join(f"value_{i} = {i}\n" for i in range(1, count + 1))


def test_line_numbers_and_content_match_the_files(tmp_path: Path) -> None:
    before = {
        "many_hunks.py": numbered(60),
        "gone.py": "x = 1\n",
        "old name.py": numbered(20),
        "café.py": "a = 1\nb = 2",
        "windows.py": "a = 1\r\nb = 2\r\n",
    }
    changed_lines = numbered(60).replace("value_3 = 3\n", "").replace("= 30\n", "= 300\n")
    after = {
        "many_hunks.py": changed_lines + "tail = True\n",
        "new name.py": numbered(20).replace("= 10\n", "= 100\n"),
        "café.py": "a = 1\nb = 3",
        "windows.py": "a = 1\r\nb = 3\r\n",
        "pkg/fresh.py": "y = 2\n",
    }

    git(tmp_path, "init", "--quiet")
    write(tmp_path, before)
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "--quiet", "--message", "before")
    (tmp_path / "gone.py").unlink()
    (tmp_path / "old name.py").unlink()
    write(tmp_path, after)
    git(tmp_path, "add", "--all")

    files = parse_diff(git(tmp_path, "diff", "--cached", "--find-renames"))

    assert {(f.change_type, f.old_path, f.new_path) for f in files} == {
        (ChangeType.MODIFIED, "many_hunks.py", "many_hunks.py"),
        (ChangeType.DELETED, "gone.py", None),
        (ChangeType.RENAMED, "old name.py", "new name.py"),
        (ChangeType.MODIFIED, "café.py", "café.py"),
        (ChangeType.MODIFIED, "windows.py", "windows.py"),
        (ChangeType.ADDED, None, "pkg/fresh.py"),
    }

    checked = 0
    for changed in files:
        for hunk in changed.hunks:
            for line in hunk.lines:
                if line.old_lineno is not None:
                    assert changed.old_path is not None
                    assert before[changed.old_path].split("\n")[line.old_lineno - 1] == line.content
                    checked += 1
                if line.new_lineno is not None:
                    assert changed.new_path is not None
                    assert after[changed.new_path].split("\n")[line.new_lineno - 1] == line.content
                    checked += 1
    assert checked > 50
    assert len(next(f for f in files if f.new_path == "many_hunks.py").hunks) == 3
