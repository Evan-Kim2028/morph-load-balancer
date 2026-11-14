#!/usr/bin/env python3
"""Run one pi (``pi -p --mode json``) headless worker for the agentq broker.

Usage: python3 -m agentq.launch_pi <label> <workspace> <prompt_file> <outdir> [--model M] [--tier T]

The label must match ``agentq.db.LAUNCH_LABEL_RE`` and is refused with a one-line
reason otherwise. The prompt gets a ``[label: <label>]`` first line when it lacks one;
the copy goes to ``<outdir>/prompt-<label>.md``, so the original is never modified.
``stdin`` is closed because pi hangs otherwise.

The brief goes in the system prompt (``--append-system-prompt``). Pi rebuilds the
system prompt every turn and compaction never summarises it, so a long run keeps its
scope rules and verdict format. The only user message points at the brief.

Writes, in ``outdir``:

  <outdir>/pi-sessions.tsv          label, session_id, start epoch, model, tier
  <outdir>/stream-<label>.ndjson    raw NDJSON event stream
  <outdir>/out-<label>.md           final assistant text
  <outdir>/err-<label>.log          stderr
  <outdir>/running/<label>          "<pid>\\t<model>" while the run is live
  <outdir>/QUOTA-<label>            only on quota/credit exhaustion

Quota/credit exhaustion (an ``errorMessage`` naming insufficient credit/quota/balance,
HTTP 402, or a rate-limit 429 the run never recovered from) finishes ``died_early``
with verdict ``BLOCKED`` and writes ``QUOTA-<label>``. A transient 429 that pi
auto-retried past is not quota: the summary line records it as ``rate_limit_retries``.

``AGENTQ_RESUME_SESSION`` (set by the broker on a relaunch after a run died without a
report) names the dead run's pi session file. When it names a non-empty file, the
launcher runs ``pi --fork <file> --session-dir <outdir>/sessions`` with a continuation
prompt instead of the task. A missing file, or a resume pi exits on without output,
falls back to a fresh launch with a note on stderr.

Pytest slots (``agentq/pyplugin/agentq_pytest_slot.py``), unless ``AGENTQ_SHIMS=0``.
The PATH shims only see ``pytest`` typed by name, so a worker that runs a venv
interpreter by absolute path gets the slot from the plugin instead. ``shim_environ``
sets, on the pi child:

- ``PYTHONPATH``: the plugin folder prepended to any existing value.
- ``PYTEST_ADDOPTS``: ``-p agentq_pytest_slot`` appended to any existing value.
- ``PYTEST_XDIST_AUTO_NUM_WORKERS=2`` and ``AGENTQ_XDIST_CAP=2``, each only when unset.
  ``AGENTQ_XDIST_CAP`` is the worker cap the plugin enforces on xdist's parsed count.

No API key is read, logged, or printed by this script.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from agentq import logfilter
from agentq import slots as agentq_slots
from agentq.db import LAUNCH_LABEL_MAX, LAUNCH_LABEL_RE

DEFAULT_MODEL = os.environ.get("AGENTQ_PI_MODEL", "morph/morph-dsv41flash-1017")
BRIEF_MESSAGE = (
    "Do the task described in the Task brief in your system prompt. Your final message is the report."
)
USAGE = "usage: python3 -m agentq.launch_pi <label> <workspace> <prompt_file> <outdir> [--model M] [--tier T]"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: A provider status code of 402 or 429 in an assistant ``errorMessage``:
#: ``HTTP 429``, ``status 402``, ``"status": 429``, ``code 429``, or a leading
#: ``429:``. Lowercased text; a bare number elsewhere is not a status.
_STATUS_CODE = re.compile(r'(?:^|\bhttp[ /]?|\bstatus"?\s*[:=]?\s*|\bcode"?\s*[:=]?\s*)(402|429)\b')

#: A JSON object body (``{"message": ...``), the structured half of a provider error.
_JSON_BODY = re.compile(r'\{\s*"[^"]+"\s*:')

#: Morph's rate-limit text, returned as a plain error line with no JSON body.
_MORPH_RATE_LIMIT = "rate limit exceeded"


def _status_with_body(text: str, code: str) -> bool:
    """True when the text names HTTP ``code`` and carries a JSON error body."""
    return any(m.group(1) == code for m in _STATUS_CODE.finditer(text)) and bool(_JSON_BODY.search(text))


def tier_of(model: str | None) -> str:
    """Tier name for a direct launch with no ``--tier``: ``kimi`` for a Kimi model, else ``deepseek``.

    The broker always passes the tier (``agentq/run_worker.py``), so this is only the fallback.
    """
    return "kimi" if "kimi" in (model or "").lower() else "deepseek"


def validate_label(label: str) -> str | None:
    """Return a one-line fix when ``label`` is not a valid launch label, else None."""
    if LAUNCH_LABEL_RE.fullmatch(label or ""):
        return None
    return f"use 1-{LAUNCH_LABEL_MAX} characters from letters, digits, '_', '.', '+' and '-'"


def ensure_label_prefix(prompt: str, label: str) -> str:
    """Prepend ``[label: <label>]`` when the prompt lacks it."""
    if prompt.startswith(f"[label: {label}]"):
        return prompt
    return f"[label: {label}]\n{prompt}"


def _event_text(ev) -> str:
    """Lowercased error text of one assistant message_end summary."""
    return (str(ev.get("error") or "") + " " + str(ev.get("stop") or "")).lower()


def _credit_hit(text: str) -> bool:
    """HTTP 402 with a JSON error body: credit or quota exhausted, never transient."""
    return _status_with_body(text, "402")


def _rate_limit_hit(text: str) -> bool:
    """HTTP 429 with a JSON error body, or Morph's ``Rate limit exceeded`` text."""
    return _status_with_body(text, "429") or _MORPH_RATE_LIMIT in text


def _recovered(events: list[dict], index: int) -> bool:
    """A successful assistant message_end follows ``events[index]``."""
    return any(not str(later.get("error") or "") for later in events[index + 1 :])


def quota_hit(events) -> bool:
    """Quota/credit exhaustion from the ordered assistant message_end events.

    Credit exhaustion (HTTP 402, insufficient credit/quota/balance,
    payment required) is an immediate quota hit. A rate-limit 429 counts
    only when the run never recovered: it is the last assistant
    message_end, or no successful assistant message_end follows it, which
    is the empty-final-text case. pi auto-retries a transient 429
    and finishes normally, so a 429 with a later successful message is a
    retry (``rate_limit_retries``), not quota.
    """
    events = list(events)
    for index, ev in enumerate(events):
        text = _event_text(ev)
        if _credit_hit(text):
            return True
        if _rate_limit_hit(text) and not _recovered(events, index):
            return True
    return False


def rate_limit_retries(events) -> int:
    """Transient rate-limit 429s pi auto-retried past (recovered runs)."""
    events = list(events)
    return sum(
        1
        for index, ev in enumerate(events)
        if _rate_limit_hit(_event_text(ev)) and _recovered(events, index)
    )


def assistant_events(raw: bytes) -> list[dict]:
    """Every assistant ``message_end`` in stream order, errors included.

    Each item is ``{"error": errorMessage-or-None, "stop": stopReason}``;
    an empty ``error`` marks a successful message, the recovery signal
    that separates a transient auto-retried 429 from quota exhaustion.
    """
    out = []
    for line in (raw or b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "message_end":
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        if message.get("role") != "assistant":
            continue
        out.append(
            {
                "error": message.get("errorMessage"),
                "stop": message.get("stopReason"),
            }
        )
    return out


def verdict_of(text: str) -> str:
    """Worker verdict from the first line of the final text."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    first = lines[0].upper() if lines else ""
    for word in ("BLOCKED", "PARTIAL", "PASS"):
        if word in first:
            return word
    return "PARTIAL"


def final_text(raw: bytes) -> str:
    """Last assistant text block in a stream (the worker's report), or ''."""
    text = ""
    for line in (raw or b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "message_end":
            continue
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        joined = "".join(
            c.get("text", "")
            for c in (message.get("content") or [])
            if isinstance(c, dict) and c.get("type") == "text"
        )
        if joined.strip():
            text = joined
    return text


def session_id_of(raw: bytes) -> str | None:
    """Session id from the stream's ``session`` event, or None."""
    for line in (raw or b"").split(b"\n")[:8]:
        if b'"session"' not in line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "session":
            sid = obj.get("id")
            if isinstance(sid, str) and sid:
                return sid
    return None


def brief_args(prompt_file):
    """Carry the brief in the system prompt. ``prompt_file`` must be an absolute path."""
    return ["--append-system-prompt", str(prompt_file)]


THINKING_ENV = "AGENTQ_THINKING"
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
THINKING_LEVEL_FILE = "thinking-level"


def thinking_level(environ):
    """The thinking level the broker set for this run (``AGENTQ_THINKING``), or None.

    The broker sets it from ``broker.json`` ``tiers_thinking`` or from the job's ``thinking`` field. Only the levels pi
    accepts count; an unset variable means pi's own default.
    """
    value = (environ.get(THINKING_ENV) or "").strip()
    return value if value in THINKING_LEVELS else None


def thinking_args(level):
    """``--thinking <level>`` when a level is set, else nothing."""
    return ["--thinking", level] if level else []


def write_thinking_level(outdir, level):
    """Record the level this run was launched with in its run folder. Best effort; nothing is written when unset."""
    if level is None:
        return
    try:
        (Path(outdir) / THINKING_LEVEL_FILE).write_text(level + "\n", encoding="utf-8")
    except OSError:
        pass


SKILLS_ENV = "AGENTQ_PI_SKILLS"


def skills_args(environ=None):
    """``--no-skills`` unless ``AGENTQ_PI_SKILLS=1``.

    pi lists every discovered skill in the system prompt, and that list rides every request of
    every worker (about 5,400 tokens for 48 skills). Workers can still read a skill file by path.
    """
    environ = os.environ if environ is None else environ
    return [] if (environ.get(SKILLS_ENV) or "").strip() == "1" else ["--no-skills"]


def build_argv(label, workspace, prompt_file, outdir, model, thinking=None):
    """The exact pi command line. ``stdin`` is closed by the caller."""
    return [
        "pi",
        "-p",
        "--mode",
        "json",
        "--session-dir",
        str(Path(outdir) / "sessions"),
        "--model",
        model,
        *thinking_args(thinking),
        *skills_args(),
        *brief_args(prompt_file),
        BRIEF_MESSAGE,
    ]


RESUME_ENV = "AGENTQ_RESUME_SESSION"
RESUME_PROMPT = (
    "Your previous run was interrupted. Continue the task from where the session "
    "left off; finish with the usual report and verdict line."
)


def resume_source(requested):
    """The resume session file when ``requested`` names a non-empty file, else None."""
    if not requested:
        return None
    path = Path(requested).expanduser()
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def build_resume_argv(session_file, outdir, model, prompt_file, thinking=None):
    """Fork the dead run's session into the new pi dir and send the continuation prompt.

    ``--fork`` makes a new session from the old one, and ``--session-dir``
    stores that new session under this run's ``sessions/``, so the old run's
    folder is only read, never appended to. The brief rides the system prompt
    again, so the forked session keeps it.
    """
    return [
        "pi",
        "-p",
        "--mode",
        "json",
        "--fork",
        str(session_file),
        "--session-dir",
        str(Path(outdir) / "sessions"),
        "--model",
        model,
        *thinking_args(thinking),
        *skills_args(),
        *brief_args(prompt_file),
        RESUME_PROMPT,
    ]


def agentq_home():
    """``$AGENTQ_HOME``, else the broker's default state folder."""
    env = (os.environ.get("AGENTQ_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "agentq"


RUN_ENV = "AGENTQ_RUN"
SHIMS_ENV = "AGENTQ_SHIMS"
SHIM_PYTHON_ENV = "AGENTQ_SHIM_PYTHON"
PACKAGE_DIR = Path(__file__).resolve().parent
SHIMS_DIR = PACKAGE_DIR / "shims"
PYTEST_PLUGIN_DIR = PACKAGE_DIR / "pyplugin"
PYTEST_PLUGIN = "agentq_pytest_slot"
XDIST_DEFAULTS = (("PYTEST_XDIST_AUTO_NUM_WORKERS", "2"), ("AGENTQ_XDIST_CAP", "2"))


def set_run_label(label, environ=os.environ):
    """Name the run for the agentq proxy's per-run accounting. The pi process reads it through
    AGENTQ_RUN, which the proxy reads for per-run accounting. An existing value
    stays; AGENTQ_JOB is never touched: the broker sets it on every job it launches."""
    if not (environ.get(RUN_ENV) or "").strip():
        environ[RUN_ENV] = label
    return environ


def shim_environ(environ, shims_dir=SHIMS_DIR, python=sys.executable, plugin_dir=PYTEST_PLUGIN_DIR):
    """Put the agentq command shims first on PATH for the pi child; ``AGENTQ_SHIMS=0`` skips it.

    The shims gate ssh to production hosts and pytest runs (see agentq/shims/dispatch.py).
    ``AGENTQ_SHIM_PYTHON`` names the interpreter the shims run their dispatcher with.
    The pytest slot plugin and its xdist caps are set in the same place (see the module
    docstring), under the same ``AGENTQ_SHIMS=0`` switch.
    """
    if (environ.get(SHIMS_ENV) or "").strip() == "0":
        return environ
    shims = str(shims_dir)
    rest = [part for part in (environ.get("PATH") or "").split(os.pathsep) if part and part != shims]
    environ["PATH"] = os.pathsep.join([shims, *rest])
    environ.setdefault(SHIM_PYTHON_ENV, python)
    plugin = str(plugin_dir)
    pythonpath = [part for part in (environ.get("PYTHONPATH") or "").split(os.pathsep) if part and part != plugin]
    environ["PYTHONPATH"] = os.pathsep.join([plugin, *pythonpath])
    addopts = (environ.get("PYTEST_ADDOPTS") or "").strip()
    if f"-p {PYTEST_PLUGIN}" not in addopts:
        environ["PYTEST_ADDOPTS"] = " ".join(part for part in (addopts, f"-p {PYTEST_PLUGIN}") if part)
    for key, value in XDIST_DEFAULTS:
        if not (environ.get(key) or "").strip():
            environ[key] = value
    return environ


def run_pi(argv, stream, workspace, se):
    """Run one pi command through the base64 filter into ``stream``; return its exit code."""
    return logfilter.run_filtered(
        argv,
        stream,
        cwd=str(Path(workspace).expanduser()),
        stderr=se,
        stdin=subprocess.DEVNULL,
        warn=logfilter.warn_to(se),
        detached=True,
    )


def pi_refused_resume(rc, stream) -> bool:
    """True when a resume attempt exited non-zero and produced no assistant output."""
    raw = stream.read_bytes() if stream.exists() else b""
    return rc != 0 and not assistant_events(raw)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    model = DEFAULT_MODEL
    tier = None
    for flag in ("--model", "--tier"):
        if flag not in argv:
            continue
        at = argv.index(flag)
        if at + 1 >= len(argv):
            print(USAGE, file=sys.stderr)
            return 2
        if flag == "--model":
            model = argv[at + 1]
        else:
            tier = argv[at + 1].strip() or None
        del argv[at : at + 2]
    if len(argv) != 4:
        print(USAGE, file=sys.stderr)
        return 2
    label, workspace, prompt_file, outdir = argv
    tier = tier or tier_of(model)
    fix = validate_label(label)
    if fix is not None:
        print(f"label refused: {fix}", file=sys.stderr)
        return 2

    set_run_label(label)
    shim_environ(os.environ)
    home = agentq_home()
    agentq_slots.write_ssh_hosts(home)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "running").mkdir(parents=True, exist_ok=True)

    prompt_text = Path(prompt_file).read_text(encoding="utf-8")
    local_prompt = (out / f"prompt-{label}.md").resolve()
    local_prompt.write_text(ensure_label_prefix(prompt_text, label), encoding="utf-8")

    stream = out / f"stream-{label}.ndjson"
    err = out / f"err-{label}.log"
    started = int(time.time())
    thinking = thinking_level(os.environ)
    write_thinking_level(out, thinking)
    run_id = f"running/{label}"
    (out / run_id).write_text(f"{os.getpid()}\t{model}", encoding="utf-8")
    requested = (os.environ.get(RESUME_ENV) or "").strip()
    resume = resume_source(requested)
    rc = 1
    with open(err, "a") as se:
        se.write(f"--- pi launch label {label} model {model} at {started}\n")
        se.flush()
        if requested and resume is None:
            se.write(f"resume: {requested} is missing or empty; starting fresh\n")
            se.flush()
        if resume is not None:
            rc = run_pi(build_resume_argv(resume, out, model, local_prompt, thinking), stream, workspace, se)
            if pi_refused_resume(rc, stream):
                se.write(f"resume: pi refused {resume} (exit {rc}, no output); starting fresh\n")
                se.flush()
                resume = None
        if resume is None:
            rc = run_pi(build_argv(label, workspace, local_prompt, out, model, thinking), stream, workspace, se)
    try:
        os.unlink(out / run_id)
    except FileNotFoundError:
        pass

    raw = stream.read_bytes() if stream.exists() else b""
    assistant = assistant_events(raw)
    quota = quota_hit(assistant)
    retries = rate_limit_retries(assistant)
    sid = session_id_of(raw) or str(uuid.uuid4())
    text = final_text(raw)

    (out / f"out-{label}.md").write_text(text if text.strip() else "", encoding="utf-8")
    manifest = out / "pi-sessions.tsv"
    with open(manifest, "a", encoding="utf-8") as fh:
        if manifest.stat().st_size == 0:
            fh.write("label\tsession_id\tstarted_epoch\tmodel\ttier\n")
        fh.write(f"{label}\t{sid}\t{started}\t{model}\t{tier}\n")

    if quota:
        (out / f"QUOTA-{label}").write_text(f"model\t{model}\nlabel\t{label}\n", encoding="utf-8")
        verdict, status = "BLOCKED", "died_early"
    else:
        verdict = verdict_of(text)
        status = "done" if rc == 0 and text.strip() else "died_early"

    print(
        f"launched pi {label} model {model} status {status} verdict {verdict}"
        f" rate_limit_retries={retries}"
        + (f" resumed_from={resume}" if resume else "")
    )
    return 0 if status == "done" and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
