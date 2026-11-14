"""Start one pi worker for the broker and report its pid.

Usage: run_worker.py <tier> <label> <workspace> <prompt-file> <run-dir> <max-wait> [--no-preflight]

The broker calls this once per launch with max-wait ``0``. The script validates the
arguments, starts ``agentq/launch_pi.py`` in its own session, writes the launcher's pid
to ``<run-dir>/<label>.pid``, prints one ``STATUS:`` line and exits. The worker keeps
running after this script exits; the broker finds it again by pid.

``STATUS: running out=<run-dir>/pi tier=<tier>`` on success. ``STATUS: failed reason=<why>``
with exit 1 on a refused launch. The model for each tier comes from the ``models`` key of
``broker.json`` over ``DEFAULT_MODELS``. Caps and queueing are the broker's job, not this
script's.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentq import db  # noqa: E402

DEFAULT_MODELS = {
    "pi-deepseek-flash": "morph/morph-dsv41flash-1017",
    "pi-kimi": "morph/morph-kimik31",
}
USAGE = "usage: run_worker.py <tier> <label> <workspace> <prompt-file> <run-dir> <max-wait> [--no-preflight]"


def models_for(home: Path) -> dict:
    """Tier to pi model: ``DEFAULT_MODELS`` with the ``models`` key of broker.json laid over it."""
    try:
        data = json.loads((home / "broker.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    override = data.get("models") if isinstance(data, dict) else None
    models = dict(DEFAULT_MODELS)
    if isinstance(override, dict):
        models.update({str(tier): str(model) for tier, model in override.items() if isinstance(model, str)})
    return models


def refuse(reason: str) -> int:
    print(f"STATUS: failed reason={reason}")
    return 1


def main(argv=None, env=None) -> int:
    args = [a for a in (sys.argv[1:] if argv is None else argv) if a != "--no-preflight"]
    if len(args) != 6:
        print(USAGE, file=sys.stderr)
        return 2
    tier, label, workspace, prompt, run_dir, _max_wait = args
    env = dict(os.environ if env is None else env)
    home = db.home_dir(env)
    models = models_for(home)
    if tier not in models:
        return refuse("unknown-tier")
    if not db.LAUNCH_LABEL_RE.fullmatch(label):
        return refuse("bad-label")
    workspace_path, prompt_path = Path(workspace).expanduser(), Path(prompt)
    if not workspace_path.is_dir():
        return refuse("no-workspace")
    if not prompt_path.is_file():
        return refuse("no-prompt")

    run = Path(run_dir)
    out = run / "pi"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "agentq.launch_pi", label, str(workspace_path), str(prompt_path), str(out),
        "--model", models[tier], "--tier", tier,
    ]
    child_env = dict(env, PYTHONPATH=os.pathsep.join(p for p in (str(PACKAGE_ROOT), env.get("PYTHONPATH", "")) if p))
    log = open(out / f"err-{label}.log", "ab")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(PACKAGE_ROOT), env=child_env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )
    except OSError as exc:
        log.close()
        return refuse(f"spawn-failed:{type(exc).__name__}")
    log.close()
    run.mkdir(parents=True, exist_ok=True)
    (run / f"{label}.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    print(f"STATUS: running out={out} tier={tier}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
