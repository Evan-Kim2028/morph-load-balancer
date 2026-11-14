"""Background cleanup for the laptop: the worktree reaper, stream gc and log rotation.

``agentq housekeeping`` runs all three. It is a dry run unless ``--apply`` is given;
the launchd cleanup job (``com.agentq.housekeeping``) passes ``--apply``.

Worktree reaper: a job that ran in its own auto worktree (see ``auto_worktree`` in
``broker.json``) leaves a checkout behind. The reaper removes a worktree only when all
of these hold:

- the job is in a terminal state and finished more than ``min_age`` ago;
- the worktree lives under the configured auto-worktree root;
- ``git status --porcelain`` is empty, so no uncommitted work is lost;
- git reports it as a linked worktree, not the main checkout.

It removes it with ``git worktree remove`` and never with ``--force``. The branch is
kept, so any commits stay reachable.

Log rotation: a file in ``$AGENTQ_HOME/logs`` over ``max_bytes`` is copied to ``.1``
(older copies shift to ``.2``, ``.3``) and truncated in place, so launchd keeps writing
to the same file descriptor.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from agentq import db
from agentq import gc as gc_lib

GIT_TIMEOUT_S = 60.0
DEFAULT_MIN_AGE_H = 4.0
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_KEEP = 3


def _git(args: list[str], cwd: Path, run=subprocess.run) -> subprocess.CompletedProcess | None:
    """Run git in ``cwd``; None when git cannot run or times out."""
    try:
        return run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                   timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def worktree_verdict(path: Path, run=subprocess.run) -> tuple[str, str | None]:
    """``("remove", git common dir)`` for a clean linked worktree, else ``("keep", reason)``."""
    if not path.is_dir():
        return "keep", "folder is gone"
    common = _git(["rev-parse", "--git-common-dir", "--git-dir"], path, run=run)
    if common is None or common.returncode != 0:
        return "keep", "not a git checkout"
    common_dir, git_dir = (line.strip() for line in common.stdout.splitlines()[:2])
    if Path(common_dir).resolve() == Path(git_dir).resolve():
        return "keep", "main checkout, not a linked worktree"
    status = _git(["status", "--porcelain"], path, run=run)
    if status is None or status.returncode != 0:
        return "keep", "git status failed"
    if status.stdout.strip():
        return "keep", "uncommitted changes"
    return "remove", str(Path(common_dir).resolve())


def plan_worktrees(conn, worktree_root: Path, *, min_age_s: float, now: float, run=subprocess.run) -> list[dict]:
    """One row per finished auto-worktree job: ``job``, ``path``, ``action`` (remove or keep) and ``reason``."""
    rows = db.select_jobs(conn, "state IN (?, ?, ?)", gc_lib.TERMINAL)
    out = []
    for job in rows:
        path = Path(str(job.get("workspace") or ""))
        finished = job.get("finished_at")
        if not path or not _is_inside(path, worktree_root) or path.resolve() == worktree_root.resolve():
            continue
        if finished is None or float(finished) > now - min_age_s:
            continue
        action, detail = worktree_verdict(path, run=run)
        if action == "remove":
            out.append({"job": job["id"], "path": str(path), "action": "remove", "common": detail})
        else:
            out.append({"job": job["id"], "path": str(path), "action": "keep", "reason": detail})
    return out


def remove_worktrees(items: list[dict], run=subprocess.run) -> tuple[int, list[str]]:
    """Run ``git worktree remove`` for each planned removal. Returns (removed count, error lines)."""
    removed, errors = 0, []
    for item in items:
        if item["action"] != "remove":
            continue
        proc = _git(["--git-dir", item["common"], "worktree", "remove", item["path"]],
                    cwd=Path(item["path"]).parent if Path(item["path"]).parent.is_dir() else Path.home(), run=run)
        if proc is not None and proc.returncode == 0:
            removed += 1
        else:
            detail = (proc.stderr if proc is not None else "git did not run").strip()[-200:]
            errors.append(f"{item['path']}: {detail}")
    return removed, errors


def rotate_logs(log_dir: Path, *, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> list[str]:
    """Rotate every ``*.log`` over ``max_bytes`` by copy and truncate. Returns the rotated names."""
    rotated = []
    if not log_dir.is_dir():
        return rotated
    for log in sorted(log_dir.glob("*.log")):
        try:
            if log.is_symlink() or log.stat().st_size <= max_bytes:
                continue
        except OSError:
            continue
        for n in range(keep, 1, -1):
            older, newer = log_dir / f"{log.name}.{n - 1}", log_dir / f"{log.name}.{n}"
            if older.exists():
                os.replace(older, newer)
        shutil.copyfile(log, log_dir / f"{log.name}.1")
        with open(log, "r+b") as fh:
            fh.truncate(0)
        rotated.append(log.name)
    return rotated
