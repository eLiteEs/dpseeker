#!/usr/bin/env python3
"""
DeepSeeker — DeepSeek CLI with Claude-Code-style tools.

Features:
  · Base64 write_file / append_file (whitespace-safe).
  · Read confirmation; sensitive files (.env, *.key, id_rsa…) always prompt.
  · Git tools: status, diff, log, add, commit.
  · Q&A: DeepSeek can ask with options or expect a free-form answer.
  · Startup conversation picker with history replay.
  · Spinner + elapsed + char count while waiting.
  · Optional DeepThink (R1) toggle.

Requirements:
    pip install playwright
    playwright install chromium

Usage:
    python deepseeker.py               # login if needed, then chat
    python deepseeker.py --relogin     # wipe profile, force login
    python deepseeker.py --yes         # auto-approve (sensitive reads still prompt)
    python deepseeker.py --deepthink   # enable DeepThink

In-chat commands:
    /help /cwd /rules /cache /clear /reset /exit /logout
"""

import base64
import difflib
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import colorama
    colorama.just_fix_windows_console()
except ImportError:
    pass

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PWTimeout,
    Error as PWError,
)


# ─── Config ──────────────────────────────────────────────────────────────────
USER_DATA_DIR  = Path.home() / ".deepseeker"
DEEPSEEK_URL   = "https://chat.deepseek.com/"
SESSION_PREFIX = "dpseekresponse"

REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

INPUT_SELECTORS = [
    "textarea#chat-input",
    "textarea[placeholder*='Message']",
    "textarea[placeholder*='DeepSeek']",
    "textarea",
]
RESPONSE_SELECTORS = [
    "div.ds-markdown",
    "div[class*='ds-markdown']",
    "div[class*='markdown']",
]
SIDEBAR_LINK_SELECTORS = [
    "aside a[href*='/a/chat/s/']",
    "a[href*='/a/chat/s/']",
]
DEEPTHINK_SELECTORS = [
    "button:has-text('DeepThink')",
    "button:has-text('Deepthink')",
    "button:has-text('R1')",
    "div[role='switch']",
    "[aria-label*='DeepThink']",
    "[aria-label*='think']",
]
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

STREAM_STABLE_SECONDS = 2.0
RESPONSE_TIMEOUT      = 180
MAX_FILE_READ_BYTES   = 200_000
MAX_TURN_STEPS        = 25
MAX_DIFF_LINES        = 60
MAX_TREE_ENTRIES      = 30
MAX_OUTPUT_LINES      = 12
INLINE_READ_THRESHOLD = 20_000
SPINNER_FRAMES        = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519",
    "id_ecdsa", "credentials", "secrets", "config.json",
    "*.p12", "*.pfx", ".npmrc", ".pypirc", ".netrc",
    ".git-credentials", "auth.json",
]

CWD: Path = Path.cwd().resolve()
READ_CACHE: dict[str, tuple[float, int]] = {}


# ─── Colors ─────────────────────────────────────────────────────────────────
def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") is not None: return False
    if os.environ.get("FORCE_COLOR") is not None: return True
    return sys.stdout.isatty()

if _supports_color():
    RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
    RED, GREEN, YELLOW, CYAN, MAGENTA = (
        "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[35m"
    )
else:
    RESET = BOLD = DIM = RED = GREEN = YELLOW = CYAN = MAGENTA = ""


# ─── Console helpers ────────────────────────────────────────────────────────
def message(text: str) -> None:
    lines = (text or "").split("\n")
    print(f"{DIM}⏺{RESET} {lines[0]}")
    for line in lines[1:]:
        print(f"  {line}")

def info(text: str) -> None:
    print(f"{DIM}· {text}{RESET}")

def success(text: str) -> None:
    print(f"{GREEN}✓{RESET} {text}")

def warning(text: str) -> None:
    print(f"{YELLOW}!{RESET} {text}")

def error(text: str) -> None:
    print(f"{RED}✗{RESET} {text}")


def banner(cwd: Path, deepthink: bool) -> None:
    print(f"""{CYAN}
  ██████╗ ██████╗ ███████╗███████╗███████╗██╗  ██╗███████╗██████╗
  ██╔══██╗██╔══██╗██╔════╝██╔════╝██╔════╝██║ ██╔╝██╔════╝██╔══██╗
  ██║  ██║██████╔╝███████╗█████╗  █████╗  █████╔╝ █████╗  ██████╔╝
  ██║  ██║██╔═══╝ ╚════██║██╔══╝  ██╔══╝  ██╔═██╗ ██╔══╝  ██╔══██╗
  ██████╔╝██║     ███████║███████╗███████╗██║  ██╗███████╗██║  ██║
  ╚═════╝ ╚═╝     ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝{RESET}""")
    mode = "deepthink · on" if deepthink else "deepthink · off"
    print(f"  {DIM}cwd  {cwd}{RESET}")
    print(f"  {DIM}{mode}   ·   /help for commands{RESET}")
    print()


# ─── Loading bar ────────────────────────────────────────────────────────────
class Progress:
    def __init__(self, label: str = "waiting"):
        self.label = label
        self.start = time.time()
        self.chars = 0
        self.active = False

    def tick(self, chars: int = 0):
        self.chars = chars
        elapsed = int(time.time() - self.start)
        frame = SPINNER_FRAMES[elapsed % len(SPINNER_FRAMES)]
        bar = f"{frame} {self.label}… {elapsed}s"
        if chars:
            bar += f" · {chars} chars"
        sys.stdout.write(f"\r{DIM}{bar:<60}{RESET}")
        sys.stdout.flush()
        self.active = True

    def done(self):
        if self.active:
            sys.stdout.write("\r" + " " * 62 + "\r")
            sys.stdout.flush()
            self.active = False


# ─── System rules ───────────────────────────────────────────────────────────
SYSTEM_RULES = """\
You are DeepSeek acting as the backend of a terminal coding assistant.
You talk to the user, but you also drive their machine through tools.

OUTPUT FORMAT
  1. Every response MUST begin with the exact literal prefix: dpseekresponse
  2. Immediately after the prefix, on the SAME line, emit a single JSON header.
  3. Depending on the type, the header may be followed by a delimited block.
  4. Do NOT include prose before or after the envelope.

HEADER FORMAT (single line, compact)
  {"type":"<type>", ...fields}
  Keep the header on one line. No trailing commas. Double quotes only for
  JSON keys/strings. Do NOT put file content or long text inside the header.

BLOCK FORMAT (for message, info, warning, ask — CRITICAL)
  After the JSON header line, open the block on its own line with
  <<<CONTENT>>>, write the text on the next lines, close with <<<END>>>
  on its own line.

  Example:

    dpseekresponse{"type":"message"}
    <<<CONTENT>>>
    I created the file. Should I add tests?
    <<<END>>>

  Example with ask and options:

    dpseekresponse{"type":"ask","options":["yes","no"]}
    <<<CONTENT>>>
    The config file already exists. Overwrite it?
    <<<END>>>

  Rules for the block:
    · <<<CONTENT>>> and <<<END>>> must be on their own lines.
    · Do NOT embed the block content inside the JSON.
    · Do NOT use markdown syntax inside the block (no **, no #, no `code`).
    · Do NOT use double quotes inside the block unless unavoidable; prefer 'single'.
    · Do NOT nest another block inside.

WRITE_FILE / APPEND_FILE FORMAT (base64 required — markdown-proof)
  For write_file and append_file, the JSON header MUST contain a content_b64
  field whose value is the exact bytes encoded as standard Base64.

    dpseekresponse{"type":"write_file","path":"src/main.py","content_b64":"ZGVmIGZvbygpOgogICAgcmV0dXJuIDQyCg=="}

  Rules for content_b64:
    · NEVER put raw file content in the JSON. Always Base64.
    · No whitespace, no line breaks, no markdown inside the Base64.
    · The Base64 must decode to the exact bytes that should exist on disk.
    · If content exceeds ~1500 lines or ~50 KB, SPLIT it:
        first call write_file with the first chunk,
        then subsequent calls to append_file for the rest, in order.
      Never emit one write_file whose content_b64 exceeds ~60 KB.

ALL OTHER TYPES (plain JSON, single line)
    dpseekresponse{"type":"read_file","path":"main.py"}
    dpseekresponse{"type":"tree","path":".","depth":2}
    dpseekresponse{"type":"create_folder","path":"dir"}
    dpseekresponse{"type":"run","command":"pytest","cwd":"."}
    dpseekresponse{"type":"git_status"}
    dpseekresponse{"type":"git_diff","staged":false}
    dpseekresponse{"type":"git_log","limit":20}
    dpseekresponse{"type":"git_add","paths":["a.py","b.py"]}
    dpseekresponse{"type":"git_commit","message":"fix bug"}
    dpseekresponse{"type":"git_branch"}
    dpseekresponse{"type":"done"}

RUNTIME WRAPPERS YOU WILL RECEIVE
  [TOOL RESULT] ... [/TOOL RESULT]
  [USER ANSWER] ... [/USER ANSWER]
  If a read_file result contains "unchanged": true, use what you know.
  If a read_file result contains "rejected": true, do NOT retry.

BEHAVIOUR
  · Use only the types above. Never invent new ones.
  · Use relative paths (they resolve against the project root).
  · Inspect files with read_file / tree before editing if unsure.
  · When the task is complete, emit {"type":"done"} with no block.
  · Never emit prose outside the envelope.

ACKNOWLEDGE
  Reply now with exactly:
  dpseekresponse{"type":"message"}
  <<<CONTENT>>>
  ready
  <<<END>>>
"""


# ─── Path helpers ───────────────────────────────────────────────────────────
def resolve_path(path_str: str) -> tuple[Path, bool]:
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = CWD / p
    p = p.resolve()
    try:
        p.relative_to(CWD)
        return p, False
    except ValueError:
        return p, True


def is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS)


# ─── Confirmation ───────────────────────────────────────────────────────────
def confirm_action(always_yes: list, prompt: str = "Allow?", force: bool = False) -> bool:
    if always_yes[0] and not force:
        return True
    try:
        ans = input(f"  {DIM}{prompt} (y/n/a=always){RESET} › ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if ans == "a" and not force:
        always_yes[0] = True
        return True
    return ans in ("y", "yes")


def confirm_read(path: Path, always_yes: list) -> bool:
    sensitive = is_sensitive(path)
    prompt = f"Allow DeepSeek to read {path.name}?"
    if sensitive:
        prompt = f"{RED}[sensitive]{RESET} {prompt}"
    return confirm_action(always_yes, prompt=prompt, force=sensitive)


# ─── Tool implementations ───────────────────────────────────────────────────
def tool_read_file(args: dict) -> dict:
    path_str = args.get("path", "")
    if not path_str:
        return {"ok": False, "error": "missing path"}
    p, outside = resolve_path(path_str)
    if not p.exists():
        return {"ok": False, "error": f"file not found: {path_str}"}
    if not p.is_file():
        return {"ok": False, "error": f"not a file: {path_str}"}

    st = p.stat()
    key = str(p)
    cached = READ_CACHE.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return {
            "ok": True,
            "unchanged": True,
            "path": path_str,
            "bytes": st.st_size,
            "note": "File unchanged since last read. You already have its content above.",
        }

    if st.st_size > MAX_FILE_READ_BYTES:
        return {"ok": False, "error": f"file too large ({st.st_size} bytes)"}
    try:
        content = p.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    READ_CACHE[key] = (st.st_mtime, st.st_size)
    return {
        "ok": True,
        "path": str(p) if outside else str(p.relative_to(CWD)),
        "bytes": len(content),
        "content": content,
    }


def tool_tree(args: dict) -> dict:
    path_str = args.get("path", ".")
    depth = int(args.get("depth", 2))
    p, _ = resolve_path(path_str)
    if not p.exists() or not p.is_dir():
        return {"ok": False, "error": f"not a directory: {path_str}"}

    entries: list[tuple[int, str]] = []

    def walk(cur: Path, level: int) -> None:
        if level > depth:
            return
        try:
            children = sorted(cur.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return
        for child in children:
            if child.name.startswith(".") and child.name not in (".gitignore", ".env.example"):
                continue
            rel = child.relative_to(p)
            entries.append((level, rel.name + ("/" if child.is_dir() else "")))
            if child.is_dir() and not child.is_symlink():
                walk(child, level + 1)

    walk(p, 0)
    return {"ok": True, "path": str(p), "entries": entries}


def tool_create_folder(args: dict, always_yes: list) -> dict:
    path_str = args.get("path", "")
    if not path_str:
        return {"ok": False, "error": "missing path"}
    p, outside = resolve_path(path_str)
    if p.exists():
        return {"ok": True, "path": path_str, "note": "already exists"}
    if outside:
        warning(f"target is outside project root: {p}")
    if not confirm_action(always_yes, prompt=f"Create folder {path_str}?"):
        return {"ok": False, "error": "rejected by user"}
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": path_str}


def tool_write_file(args: dict, always_yes: list) -> dict:
    path_str = args.get("path", "")
    content  = args.get("content", "")
    if not path_str:
        return {"ok": False, "error": "missing path"}

    if len(content) > 50_000:
        warning(f"large write ({len(content)} bytes) — consider append_file for splits")

    p, outside = resolve_path(path_str)
    rel = str(p) if outside else str(p.relative_to(CWD))

    if p.exists():
        if not p.is_file():
            return {"ok": False, "error": f"not a file: {path_str}"}
        try:
            old = p.read_text(errors="replace")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if old == content:
            return {"ok": True, "path": rel, "note": "no change", "bytes": len(content)}
        display_diff(old, content, rel)
    else:
        display_new_file(content, rel)

    if outside:
        warning(f"target is outside project root: {p}")
    if not confirm_action(always_yes, prompt=f"Write {rel}?"):
        return {"ok": False, "error": "rejected by user"}

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    READ_CACHE.pop(str(p), None)
    return {"ok": True, "path": rel, "bytes": len(content)}


def tool_append_file(args: dict, always_yes: list) -> dict:
    path_str = args.get("path", "")
    content  = args.get("content", "")
    if not path_str:
        return {"ok": False, "error": "missing path"}
    p, outside = resolve_path(path_str)
    rel = str(p) if outside else str(p.relative_to(CWD))
    if outside:
        warning(f"target is outside project root: {p}")
    if not confirm_action(always_yes, prompt=f"Append to {rel}?"):
        return {"ok": False, "error": "rejected by user"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    READ_CACHE.pop(str(p), None)
    return {"ok": True, "path": rel, "appended": len(content)}


def tool_run(args: dict, always_yes: list) -> dict:
    cmd = args.get("command", "")
    cwd_str = args.get("cwd", ".")
    if not cmd:
        return {"ok": False, "error": "missing command"}
    p, _ = resolve_path(cwd_str)
    if not p.is_dir():
        return {"ok": False, "error": f"cwd not a directory: {cwd_str}"}

    print(f"  {DIM}$ {cmd}{RESET}")
    if not confirm_action(always_yes, prompt="Run command?"):
        return {"ok": False, "error": "rejected by user"}
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=str(p),
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": result.returncode == 0,
        "code": result.returncode,
        "stdout": (result.stdout or "")[-4000:],
        "stderr": (result.stderr or "")[-4000:],
    }


# ─── Git tools ──────────────────────────────────────────────────────────────
def _git(args_list: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args_list,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


def tool_git_status(args: dict, always_yes: list) -> dict:
    p, _ = resolve_path(args.get("cwd", "."))
    r = _git(["status", "--short", "--branch"], p)
    return {"ok": r.returncode == 0, "code": r.returncode,
            "stdout": r.stdout, "stderr": r.stderr}


def tool_git_diff(args: dict, always_yes: list) -> dict:
    p, _ = resolve_path(args.get("cwd", "."))
    cmd = ["diff"]
    if args.get("staged"):
        cmd.append("--staged")
    r = _git(cmd, p)
    return {"ok": r.returncode == 0, "code": r.returncode,
            "stdout": r.stdout[-8000:], "stderr": r.stderr}


def tool_git_log(args: dict, always_yes: list) -> dict:
    p, _ = resolve_path(args.get("cwd", "."))
    limit = str(int(args.get("limit", 20)))
    r = _git(["log", "--oneline", "-n", limit], p)
    return {"ok": r.returncode == 0, "code": r.returncode,
            "stdout": r.stdout, "stderr": r.stderr}


def tool_git_branch(args: dict, always_yes: list) -> dict:
    p, _ = resolve_path(args.get("cwd", "."))
    r = _git(["branch", "--show-current"], p)
    return {"ok": r.returncode == 0, "code": r.returncode,
            "stdout": r.stdout.strip(), "stderr": r.stderr}


def tool_git_add(args: dict, always_yes: list) -> dict:
    p, _ = resolve_path(args.get("cwd", "."))
    paths = args.get("paths") or []
    if not paths:
        return {"ok": False, "error": "missing paths"}
    print(f"  {DIM}$ git add {' '.join(paths)}{RESET}")
    if not confirm_action(always_yes, prompt=f"Stage {len(paths)} path(s)?"):
        return {"ok": False, "error": "rejected by user"}
    r = _git(["add", "--"] + paths, p)
    return {"ok": r.returncode == 0, "code": r.returncode,
            "stdout": r.stdout, "stderr": r.stderr}


def tool_git_commit(args: dict, always_yes: list) -> dict:
    p, _ = resolve_path(args.get("cwd", "."))
    msg = args.get("message", "")
    if not msg:
        return {"ok": False, "error": "missing commit message"}
    print(f"  {DIM}$ git commit -m \"{msg}\"{RESET}")
    if not confirm_action(always_yes, prompt="Commit staged changes?"):
        return {"ok": False, "error": "rejected by user"}
    r = _git(["commit", "-m", msg], p)
    return {"ok": r.returncode == 0, "code": r.returncode,
            "stdout": r.stdout, "stderr": r.stderr}


# ─── Display helpers ────────────────────────────────────────────────────────
TOOL_LABELS = {
    "read_file":     "Read",
    "write_file":    "Write",
    "append_file":   "Append",
    "create_folder": "Mkdir",
    "tree":          "Tree",
    "run":           "Bash",
    "git_status":    "Git status",
    "git_diff":      "Git diff",
    "git_log":       "Git log",
    "git_add":       "Git add",
    "git_commit":    "Git commit",
    "git_branch":    "Git branch",
}


def display_tool_call(action: dict) -> None:
    kind = action["type"]
    label = TOOL_LABELS.get(kind, kind)
    if kind == "write_file":
        p, _ = resolve_path(action.get("path", ""))
        label = "Update" if p.exists() else "Create"
    print(f"{DIM}⏺{RESET} {YELLOW}{label}{RESET}({_tool_arg_summary(action)})")


def _tool_arg_summary(action: dict) -> str:
    kind = action["type"]
    if kind in ("read_file", "write_file", "append_file", "create_folder"):
        return action.get("path", "?")
    if kind == "tree":
        return f"{action.get('path', '.')} depth={action.get('depth', 2)}"
    if kind == "run":
        cmd = action.get("command", "")
        return cmd if len(cmd) <= 60 else cmd[:57] + "…"
    if kind == "git_commit":
        return action.get("message", "")[:60]
    if kind == "git_add":
        return ", ".join(action.get("paths", []))[:60]
    return ""


def display_tool_result(kind: str, result: dict) -> None:
    if not result.get("ok"):
        print(f"  {RED}⎿ {result.get('error', 'unknown error')}{RESET}")
        return

    if kind == "read_file":
        if result.get("unchanged"):
            print(f"  {DIM}⎿ unchanged (cached){RESET}")
            return
        content = result.get("content", "")
        lines = content.split("\n")
        for i, line in enumerate(lines[:MAX_OUTPUT_LINES], 1):
            prefix = "  ⎿ " if i == 1 else "    "
            print(f"{DIM}{prefix}{i:>3} │ {line}{RESET}")
        if len(lines) > MAX_OUTPUT_LINES:
            print(f"    {DIM}… {len(lines) - MAX_OUTPUT_LINES} more lines{RESET}")

    elif kind == "tree":
        entries = result.get("entries", [])
        if not entries:
            print(f"  {DIM}⎿ (empty){RESET}")
            return
        for i, (level, name) in enumerate(entries[:MAX_TREE_ENTRIES]):
            prefix = "  ⎿ " if i == 0 else "    "
            indent = "  " * level
            print(f"{DIM}{prefix}{indent}{name}{RESET}")
        if len(entries) > MAX_TREE_ENTRIES:
            print(f"    {DIM}… {len(entries) - MAX_TREE_ENTRIES} more entries{RESET}")

    elif kind == "write_file":
        note = result.get("note")
        if note == "no change":
            print(f"  {DIM}⎿ no change{RESET}")
        else:
            print(f"  {DIM}⎿ wrote {result.get('bytes', 0)} bytes{RESET}")

    elif kind == "append_file":
        print(f"  {DIM}⎿ appended {result.get('appended', 0)} bytes{RESET}")

    elif kind == "create_folder":
        print(f"  {DIM}⎿ {result.get('note') or 'created'}{RESET}")

    elif kind == "run":
        _display_run_result(result)

    elif kind in ("git_status", "git_diff", "git_log", "git_add", "git_commit"):
        out = (result.get("stdout") or "").strip()
        err = (result.get("stderr") or "").strip()
        first = True
        for line in (out.split("\n")[:MAX_OUTPUT_LINES] if out else []):
            print(f"{DIM}{'  ⎿ ' if first else '    '}{line}{RESET}")
            first = False
        for line in (err.split("\n")[:6] if err else []):
            print(f"{RED}{'  ⎿ ' if first else '    '}{line}{RESET}")
            first = False
        print(f"    {DIM}⎿ exit {result.get('code', -1)}{RESET}")

    elif kind == "git_branch":
        print(f"  {DIM}⎿ {result.get('stdout', '')}{RESET}")


def _display_run_result(result: dict) -> None:
    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()
    code = result.get("code", -1)
    first = True
    for line in (out.split("\n")[:MAX_OUTPUT_LINES] if out else []):
        print(f"{DIM}{'  ⎿ ' if first else '    '}{line}{RESET}")
        first = False
    for line in (err.split("\n")[:6] if err else []):
        print(f"{RED}{'  ⎿ ' if first else '    '}{line}{RESET}")
        first = False
    print(f"    {DIM}⎿ exit {code}{RESET}")


def display_diff(old: str, new: str, path: str) -> None:
    diff = list(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=2,
    ))
    shown = 0
    for raw in diff:
        if raw.startswith(("+++", "---")):
            continue
        if shown >= MAX_DIFF_LINES:
            print(f"  {DIM}… {len(diff) - shown} more diff lines{RESET}")
            return
        line = raw.rstrip("\n")
        if line.startswith("+"):
            print(f"  {GREEN}{line}{RESET}")
        elif line.startswith("-"):
            print(f"  {RED}{line}{RESET}")
        elif line.startswith("@@"):
            print(f"  {DIM}{line}{RESET}")
        else:
            print(f"  {DIM}{line}{RESET}")
        shown += 1


def display_new_file(content: str, path: str) -> None:
    lines = content.split("\n")
    for line in lines[:MAX_DIFF_LINES]:
        print(f"  {GREEN}+ {line}{RESET}")
    if len(lines) > MAX_DIFF_LINES:
        print(f"  {DIM}… {len(lines) - MAX_DIFF_LINES} more lines{RESET}")


def _repair_json(text: str) -> str:
    """Escape raw control chars inside JSON string literals."""
    out, in_string, escape = [], False, False
    for c in text:
        if escape:
            out.append(c); escape = False; continue
        if c == "\\":
            out.append(c); escape = True; continue
        if c == '"':
            in_string = not in_string; out.append(c); continue
        if in_string:
            if c == "\n": out.append("\\n"); continue
            if c == "\r": out.append("\\r"); continue
            if c == "\t": out.append("\\t"); continue
        out.append(c)
    return "".join(out)


def _extract_block(text: str) -> str | None:
    """Extract text between <<<CONTENT>>> and <<<END>>>."""
    start_marker = "<<<CONTENT>>>"
    end_marker   = "<<<END>>>"
    start = text.find(start_marker)
    if start == -1:
        return None
    start += len(start_marker)
    if start < len(text) and text[start] == "\n":
        start += 1
    end = text.rfind(end_marker)
    if end == -1 or end < start:
        return None
    content = text[start:end]
    if content.endswith("\n"):
        content = content[:-1]
    return content

def parse_response(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.lstrip()
    if not text.startswith(SESSION_PREFIX):
        return None
    payload = text[len(SESSION_PREFIX):].lstrip()

    first_line, sep, rest = payload.partition("\n")
    first_line = first_line.strip()

    header = None
    try:
        obj, end = json.JSONDecoder().raw_decode(first_line)
        if isinstance(obj, dict) and end >= len(first_line):
            header = obj
    except json.JSONDecodeError:
        pass

    if header is None:
        # Legacy single-line format (also handles literal newlines in strings)
        try:
            obj, _ = json.JSONDecoder().raw_decode(_repair_json(payload))
            if isinstance(obj, dict):
                header = obj
        except json.JSONDecodeError:
            return None

    if header is None:
        return None

    kind = header.get("type", "")

    # Block format for conversational types
    if kind in ("message", "info", "warning", "ask"):
        if "<<<CONTENT>>>" in rest:
            block = _extract_block(rest)
            if block is None:
                return None   # block started but incomplete
            if kind == "ask":
                header["question"] = block
            else:
                header["content"] = block
        # else: legacy inline "content" field — leave as is

    # Base64 for file writes
    elif kind in ("write_file", "append_file") and "content_b64" in header:
        b64 = "".join(str(header["content_b64"]).split())
        try:
            header["content"] = base64.b64decode(b64, validate=True).decode("utf-8")
        except Exception:
            return None

    return header

def is_complete_response(text: str | None) -> bool:
    if not text:
        return False
    t = text.lstrip()
    if not t.startswith(SESSION_PREFIX):
        return False
    payload = t[len(SESSION_PREFIX):].lstrip()

    first_line, sep, rest = payload.partition("\n")
    header = None
    try:
        obj, end = json.JSONDecoder().raw_decode(first_line.strip())
        if isinstance(obj, dict) and end >= len(first_line.strip()):
            header = obj
    except json.JSONDecodeError:
        pass

    if header is None:
        try:
            obj, _ = json.JSONDecoder().raw_decode(_repair_json(payload))
            if isinstance(obj, dict):
                header = obj
        except json.JSONDecodeError:
            return False

    if header is None:
        return False

    kind = header.get("type", "")

    if kind in ("message", "info", "warning", "ask"):
        if "<<<CONTENT>>>" not in rest:
            return True   # legacy inline format or no block used
        return "<<<END>>>" in rest

    if kind in ("write_file", "append_file"):
        if "content_b64" not in header:
            return False
        b64 = "".join(str(header["content_b64"]).split())
        try:
            base64.b64decode(b64, validate=True)
            return True
        except Exception:
            return False

    return True

def format_tool_result(action: dict, result: dict) -> str:
    return (
        "[TOOL RESULT]\n"
        f"tool: {action['type']}\n"
        f"result: {json.dumps(result, ensure_ascii=False)}\n"
        "[/TOOL RESULT]\n"
        "Continue with the next step."
    )


# ─── Browser helpers ────────────────────────────────────────────────────────
def find_input(page):
    for sel in INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=1500)
            return loc
        except PWTimeout:
            continue
    return None


def get_last_response_text(page) -> str | None:
    try:
        loc = page.locator(RESPONSE_SELECTORS[0])
        n = loc.count()
        if n == 0:
            return None
        el = loc.nth(n - 1)

        try:
            txt = el.inner_text()
        except PWError:
            txt = None

        # Fast path: the marker is visible, innerText is good enough
        if txt and "<<<END>>>" in txt:
            return txt

        # Slow path: try to read the raw markdown from the React fiber
        try:
            raw = el.evaluate("""(el) => {
                try {
                    const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps$'));
                    const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
                    let node = (propsKey && el[propsKey]) || (fiberKey && el[fiberKey]);
                    const queue = [node];
                    const seen = new Set();
                    let steps = 0;
                    while (queue.length && steps < 5000) {
                        steps++;
                        const cur = queue.shift();
                        if (!cur || typeof cur !== 'object' || seen.has(cur)) continue;
                        seen.add(cur);
                        for (const key of ['markdown', 'content', 'text']) {
                            const val = cur[key];
                            if (typeof val === 'string' && val.includes('dpseekresponse')) return val;
                        }
                        for (const key in cur) {
                            if (key === 'return' || key === 'child' || key === 'sibling') continue;
                            if (typeof cur[key] === 'object' && cur[key] !== null) queue.push(cur[key]);
                        }
                    }
                } catch (e) {}
                return null;
            }""")
            if raw and "dpseekresponse" in raw:
                return raw
        except (PWError, Exception):
            pass

        return txt
    except PWError:
        return None


def enable_deepthink(page) -> bool:
    for sel in DEEPTHINK_SELECTORS:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=1500)
            pressed = el.get_attribute("aria-checked") or el.get_attribute("aria-pressed")
            if pressed == "true":
                return True
            el.click()
            page.wait_for_timeout(400)
            return True
        except (PWTimeout, PWError):
            continue
    return False


def attach_file_to_chat(page, file_path: Path) -> bool:
    try:
        file_input = page.locator("input[type='file']").first
        file_input.wait_for(state="attached", timeout=5000)
        file_input.set_input_files(str(file_path))
        page.wait_for_timeout(1200)
        return True
    except (PWTimeout, PWError):
        return False


def stream_response(page, prev_text: str | None, quiet: bool = False):
    start = time.time()
    prog = Progress("waiting" if not quiet else "")
    last_text = prev_text
    seen_change = False
    stable_since = None
    grace_until = None

    while time.time() - start < RESPONSE_TIMEOUT:
        current = get_last_response_text(page)
        if current is None:
            if not quiet:
                prog.tick(0)
            time.sleep(0.25)
            continue

        if not seen_change:
            if current != prev_text:
                seen_change = True
                last_text = current
                stable_since = None
                grace_until = None
                if not quiet:
                    prog.label = "receiving"
                    prog.tick(len(current))
        else:
            if current != last_text:
                last_text = current
                stable_since = None
                grace_until = None
                if not quiet:
                    prog.tick(len(current))
            elif stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since > STREAM_STABLE_SECONDS:
                if is_complete_response(last_text):
                    if not quiet:
                        prog.done()
                    return last_text
                if grace_until is None:
                    grace_until = time.time() + STREAM_STABLE_SECONDS * 4
                    if not quiet:
                        prog.label = "completing"
                elif time.time() > grace_until:
                    if not quiet:
                        prog.done()
                    return last_text

        time.sleep(0.25)

    if not quiet:
        prog.done()
    return last_text if seen_change else None


def send(page, text: str, quiet: bool = False):
    inp = find_input(page)
    if inp is None:
        return None
    prev_text = get_last_response_text(page)
    inp.click()
    inp.fill(text)
    inp.press("Enter")
    return stream_response(page, prev_text, quiet=quiet)


def send_with_attachment(page, text: str, file_path: Path, quiet: bool = False):
    prev_text = get_last_response_text(page)
    if not attach_file_to_chat(page, file_path):
        return None
    inp = find_input(page)
    if inp is None:
        return None
    inp.click()
    inp.fill(text)
    inp.press("Enter")
    return stream_response(page, prev_text, quiet=quiet)


# ─── Conversation picker & history replay ───────────────────────────────────
def list_conversations(page) -> list[dict]:
    for sel in SIDEBAR_LINK_SELECTORS:
        try:
            links = page.locator(sel).all()
        except PWError:
            continue
        convs = []
        for link in links:
            try:
                title = link.inner_text().strip() or "(untitled)"
                href = link.get_attribute("href") or ""
                if href:
                    convs.append({"title": title, "href": href})
            except PWError:
                continue
        if convs:
            return convs
    return []


def select_conversation(page) -> str | None:
    convs = list_conversations(page)
    print()
    print(f"  {BOLD}Recent conversations{RESET}")
    print(f"  {DIM}0.{RESET} new chat")
    for i, c in enumerate(convs, 1):
        print(f"  {DIM}{i}.{RESET} {c['title']}")
    print()
    try:
        choice = input(f"  {DIM}select{RESET} › ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not choice or choice == "0":
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(convs):
            page.locator(SIDEBAR_LINK_SELECTORS[0]).nth(idx).click()
            page.wait_for_timeout(2500)
            return convs[idx]["title"]
    except (ValueError, PWError):
        pass
    return None


def extract_conversation(page) -> list[tuple[str, str]]:
    try:
        raw = page.evaluate("""() => {
            const out = [];
            const nodes = document.querySelectorAll('div.fa81, div.ds-markdown');
            nodes.forEach(el => {
                const isUser = el.classList.contains('fa81');
                const text = (el.innerText || '').trim();
                if (text) out.push({role: isUser ? 'user' : 'assistant', text});
            });
            return out;
        }""")
        return [(m["role"], m["text"]) for m in raw]
    except PWError:
        return []


def replay_history(history: list[tuple[str, str]]) -> None:
    if not history:
        return
    print()
    for role, text in history:
        if role == "user":
            first, *rest = text.split("\n")
            print(f"{BOLD}{GREEN}you›{RESET} {first}")
            for line in rest:
                print(f"      {line}")
        else:
            message(text)
    print()


# ─── Ask handling ───────────────────────────────────────────────────────────
def handle_ask(action: dict) -> str:
    q = action.get("question", "")
    opts = action.get("options", []) or []
    print(f"{DIM}⏺{RESET} {MAGENTA}Ask{RESET}")
    print(f"  {q}")
    if opts:
        for i, o in enumerate(opts, 1):
            print(f"  {DIM}{i}.{RESET} {o}")
        print(f"  {DIM}(pick a number or type a free-form answer){RESET}")
    try:
        ans = input(f"  {DIM}answer{RESET} › ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "(no answer)"
    if opts and ans.isdigit():
        i = int(ans) - 1
        if 0 <= i < len(opts):
            return opts[i]
    return ans or "(no answer)"


# ─── Turn processing ────────────────────────────────────────────────────────
TOOL_KINDS = {
    "read_file", "write_file", "append_file",
    "create_folder", "tree", "run",
    "git_status", "git_diff", "git_log", "git_add", "git_commit", "git_branch",
}


def process_turn(page, raw: str | None, always_yes: list) -> None:
    retries = 0
    for _ in range(MAX_TURN_STEPS):
        action = parse_response(raw)
        if action is None:
            warning("could not parse response")
            if raw:
                print(f"  {DIM}{raw[:300]}…{RESET}")

            if retries < 1:
                retries += 1
                info("asking DeepSeek to resend as valid JSON with block format")
                raw = send(
                    page,
                    "[SYSTEM] Your last reply could not be parsed. "
                    "Resend the SAME reply following the protocol exactly: "
                    "first line is dpseekresponse<json header>; for "
                    "message/info/warning/ask add a block starting with "
                    "<<<CONTENT>>> and ending with <<<END>>> on their own lines; "
                    "for write_file/append_file use content_b64 in the JSON. "
                    "No commentary outside the envelope.",
                    quiet=True,
                )
                if raw is None:
                    error("no response after parse-retry")
                    return
                continue
            return

        kind = action.get("type", "")

        if kind == "message":
            message(action.get("content", "")); return
        if kind == "info":
            info(action.get("content", "")); return
        if kind == "warning":
            warning(action.get("content", "")); return
        if kind == "done":
            return

        if kind == "ask":
            answer = handle_ask(action)
            raw = send(
                page,
                f"[USER ANSWER]\n{answer}\n[/USER ANSWER]\nContinue.",
                quiet=True,
            )
            if raw is None:
                error("no response after user answer")
                return
            continue

        if kind not in TOOL_KINDS:
            warning(f"unknown action type: {kind}")
            return

        display_tool_call(action)

        if kind == "read_file":
            p, outside = resolve_path(action.get("path", ""))
            if not p.is_file():
                result = {"ok": False, "error": f"not a file: {action.get('path')}"}
                display_tool_result(kind, result)
                raw = send(page, format_tool_result(action, result), quiet=True)
                if raw is None:
                    error("no response after read_file")
                    return
                continue

            if not confirm_read(p, always_yes):
                result = {"ok": False, "rejected": True,
                          "error": "read rejected by user"}
                display_tool_result(kind, result)
                raw = send(page, format_tool_result(action, result), quiet=True)
                if raw is None:
                    error("no response after read rejection")
                    return
                continue

            size = p.stat().st_size
            if size > INLINE_READ_THRESHOLD:
                result = {
                    "ok": True, "attached": True,
                    "path": str(p.relative_to(CWD)) if not outside else str(p),
                    "bytes": size,
                    "note": "The file is attached above. Read it from there. "
                            "If you already received this file earlier in this "
                            "conversation, reuse that attachment.",
                }
                print(f"  {DIM}⎿ attached ({size} bytes, {size // 1024} KB){RESET}")
                raw = send_with_attachment(
                    page, format_tool_result(action, result), p, quiet=True
                )
            else:
                result = tool_read_file(action)
                display_tool_result(kind, result)
                raw = send(page, format_tool_result(action, result), quiet=True)

            if raw is None:
                error("no response after read_file")
                return
            continue

        if kind == "tree":
            result = tool_tree(action)
        elif kind == "write_file":
            result = tool_write_file(action, always_yes)
        elif kind == "append_file":
            result = tool_append_file(action, always_yes)
        elif kind == "create_folder":
            result = tool_create_folder(action, always_yes)
        elif kind == "run":
            result = tool_run(action, always_yes)
        elif kind == "git_status":
            result = tool_git_status(action, always_yes)
        elif kind == "git_diff":
            result = tool_git_diff(action, always_yes)
        elif kind == "git_log":
            result = tool_git_log(action, always_yes)
        elif kind == "git_add":
            result = tool_git_add(action, always_yes)
        elif kind == "git_commit":
            result = tool_git_commit(action, always_yes)
        elif kind == "git_branch":
            result = tool_git_branch(action, always_yes)
        else:
            result = {"ok": False, "error": f"unhandled tool: {kind}"}

        display_tool_result(kind, result)
        raw = send(page, format_tool_result(action, result), quiet=True)
        if raw is None:
            error(f"no response after {kind}")
            return

    warning(f"turn exceeded {MAX_TURN_STEPS} tool steps")


# ─── Phases ─────────────────────────────────────────────────────────────────
def login_phase(p) -> None:
    print(f"{BOLD}DeepSeeker{RESET} {DIM}— login{RESET}")
    info(f"opening Chromium (profile: {USER_DATA_DIR})")
    print()
    print(f"  1. Log in at {DEEPSEEK_URL}")
    print(f"  2. Return here and press ENTER")
    print()
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=False,
        user_agent=REAL_UA,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        args=CHROMIUM_ARGS,
    )
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(DEEPSEEK_URL, wait_until="domcontentloaded")
        input(f"{DIM}press ENTER when logged in…{RESET} ")
    finally:
        context.close()


def chat_phase(p, auto_yes: bool, want_deepthink: bool) -> None:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=True,
        user_agent=REAL_UA,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        args=CHROMIUM_ARGS,
    )
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(DEEPSEEK_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        if find_input(page) is None:
            error("chat input not found — session may have expired")
            info(f"run with --relogin or delete {USER_DATA_DIR}")
            return

        deepthink = False
        if want_deepthink:
            deepthink = enable_deepthink(page)
            if deepthink:
                success("deepthink enabled")
            else:
                warning("could not find deepthink toggle — continuing without it")

        selected = select_conversation(page)
        if selected:
            info(f"loaded: {selected}")
            history = extract_conversation(page)
            replay_history(history)

        info("initializing session…")
        ack = send(page, SYSTEM_RULES, quiet=True)
        if ack is None:
            error("no acknowledgment from DeepSeek during init")
            return
        if parse_response(ack) is not None:
            success("rules accepted")
        else:
            warning("model did not acknowledge in expected format")
            if ack:
                print(f"  {DIM}{ack[:120]}{RESET}")

        banner(CWD, deepthink)
        always_yes = [auto_yes]

        while True:
            try:
                user_in = input(f"{BOLD}{GREEN}you›{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_in:
                continue

            cmd = user_in.lower()

            if cmd in ("/exit", "/quit", "exit", "quit"):
                break
            if cmd == "/logout":
                context.close()
                info("wiping profile…")
                shutil.rmtree(USER_DATA_DIR, ignore_errors=True)
                return
            if cmd == "/rules":
                print()
                print(SYSTEM_RULES)
                continue
            if cmd == "/help":
                print()
                print(
                    "  /help    show this help\n"
                    "  /cwd     show working directory\n"
                    "  /rules   print the active system rules\n"
                    "  /cache   show read cache size\n"
                    "  /clear   clear read cache\n"
                    "  /reset   reset auto-approve state\n"
                    "  /exit    quit, keep the session\n"
                    "  /logout  quit and wipe the session"
                )
                continue
            if cmd == "/cwd":
                info(f"cwd: {CWD}")
                continue
            if cmd == "/cache":
                info(f"read cache: {len(READ_CACHE)} entries")
                continue
            if cmd == "/clear":
                READ_CACHE.clear()
                info("read cache cleared")
                continue
            if cmd == "/reset":
                always_yes[0] = False
                info("auto-approve reset")
                continue
            if cmd.startswith("/"):
                warning(f"unknown command: {user_in}")
                continue

            raw = send(page, user_in)
            process_turn(page, raw, always_yes)
            print()

    finally:
        context.close()


# ─── Entry point ────────────────────────────────────────────────────────────
def main() -> None:
    global CWD
    CWD = Path.cwd().resolve()

    relogin = "--relogin" in sys.argv
    auto_yes = any(a in sys.argv for a in ("--yes", "-y", "--dangerously-skip-permissions"))
    deepthink = "--deepthink" in sys.argv

    if relogin and USER_DATA_DIR.exists():
        info("wiping previous profile (--relogin)")
        shutil.rmtree(USER_DATA_DIR, ignore_errors=True)

    with sync_playwright() as p:
        if relogin or not USER_DATA_DIR.exists():
            login_phase(p)
        chat_phase(p, auto_yes, deepthink)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        info("interrupted")
