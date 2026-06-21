#!/usr/bin/env python3
"""
ai-zsh backend — Gemini-powered shell autocomplete + natural-language command synthesis.

Stdlib only. Two ways to run:

  python3 backend.py daemon      Long-lived unix-socket server (warm TLS conn + cache).
  python3 backend.py oneshot     Read one request frame on stdin, write one response, exit.
                                 (Fallback used by the zsh plugin when the daemon is down.)

Debug helpers:
  python3 backend.py ghost  "<buffer>"  [pwd]
  python3 backend.py prompt "<request>" [pwd]
  python3 backend.py ping

Request frame (single line, fields separated by US = \\x1f, payloads base64):
  <mode>\\x1f<b64 text>\\x1f<b64 pwd>\\x1f<b64 history>
Response frame:
  <b64 json>\\n         (ghost: {"suggestion":...}; prompt: {command,explanation,danger,alternatives})
  pong\\n               (mode=ping)
  bye\\n                (mode=shutdown, then exit)
"""

import base64
import json
import os
import platform
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import http.client
import select
from urllib.parse import urlparse

# Ollama connections in flight, keyed by worker thread id, so the request handler
# can abort a generation when the client disconnects (you typed another key).
_ACTIVE_CONN = {}

# --------------------------------------------------------------------------- #
# Config (overridable via environment)                                         #
# --------------------------------------------------------------------------- #
API_KEY      = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_HOST  = "generativelanguage.googleapis.com"

# Local / alternative providers ------------------------------------------------
OLLAMA_URL        = os.environ.get("AIZSH_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_KEEP_ALIVE = os.environ.get("AIZSH_OLLAMA_KEEPALIVE", "30m")  # keep model warm in RAM
OPENAI_BASE       = os.environ.get("AIZSH_BASE_URL", "").rstrip("/")  # any OpenAI-compatible server
OPENAI_KEY        = os.environ.get("AIZSH_API_KEY", "")


def _truthy(v):
    return str(v).strip().lower() not in ("", "0", "false", "no", "off")


def _hostport(url, default_port=80):
    u = urlparse(url if "://" in url else "http://" + url)
    return (u.hostname or "127.0.0.1", u.port or default_port)


def _ollama_up(url=OLLAMA_URL, timeout=0.4):
    try:
        host, port = _hostport(url, 11434)
        c = http.client.HTTPConnection(host, port, timeout=timeout)
        c.request("GET", "/api/tags")
        r = c.getresponse(); r.read(); c.close()
        return r.status == 200
    except (OSError, http.client.HTTPException):
        return False


# Provider: explicit via AIZSH_PROVIDER, else auto-detect (prefer a live Ollama).
PROVIDER = os.environ.get("AIZSH_PROVIDER", "").strip().lower()
if not PROVIDER:
    PROVIDER = "ollama" if _ollama_up() else ("openai" if OPENAI_BASE else "gemini")

_DEFAULT_MODELS = {
    "ollama": ("qwen2.5-coder:1.5b", "qwen2.5-coder:3b"),
    "openai": ("local", "local"),
    "gemini": ("gemini-2.0-flash-lite", "gemini-2.0-flash"),
}
_dg, _dp = _DEFAULT_MODELS.get(PROVIDER, _DEFAULT_MODELS["gemini"])
GHOST_MODEL   = os.environ.get("AIZSH_GHOST_MODEL",  _dg)
PROMPT_MODEL  = os.environ.get("AIZSH_PROMPT_MODEL", _dp)
# next-command prediction reasons over history+context → use the instruct model
PREDICT_MODEL = os.environ.get("AIZSH_PREDICT_MODEL", PROMPT_MODEL)
# auto-fix should be FAST → reuse the always-warm ghost (coder) model by default
FIX_MODEL     = os.environ.get("AIZSH_FIX_MODEL", GHOST_MODEL)

# Small local models do better with tighter, more relevant context.
def _tiny_model(m):
    # only genuinely small models need trimmed context
    return bool(re.search(r"(?:[:\-])(?:0\.5|1\.5|1\.7|2|3|4)b\b", (m or "").lower()))


# Trim context only for tiny prompt/fix models; capable ones (e.g. 14b) get richer context.
COMPACT = (_truthy(os.environ["AIZSH_COMPACT"]) if "AIZSH_COMPACT" in os.environ
           else _tiny_model(PROMPT_MODEL))

# Thinking mode (Qwen3 etc.): big accuracy win on complex tasks; used for `prompt`
# (deliberate), kept OFF for auto-fix (must stay fast).
# Opt-in: thinking improves hard prompts but adds ~25-50s; the 9b is strong without it.
THINK_ENABLED = _truthy(os.environ.get("AIZSH_THINK", "0"))
THINK_TOKENS  = int(os.environ.get("AIZSH_THINK_TOKENS", "1500"))
THINK_TIMEOUT = float(os.environ.get("AIZSH_THINK_TIMEOUT", "120"))  # thinking can be slow

# Fill-in-middle for ghost text (Ollama only): clean token-level continuation.
FIM_ENABLED = _truthy(os.environ.get("AIZSH_FIM", "1"))
FIM_PREFIX  = os.environ.get("AIZSH_FIM_PREFIX", "<|fim_prefix|>")
FIM_SUFFIX  = os.environ.get("AIZSH_FIM_SUFFIX", "<|fim_suffix|>")
FIM_MIDDLE  = os.environ.get("AIZSH_FIM_MIDDLE", "<|fim_middle|>")
FIM_TOKENS  = int(os.environ.get("AIZSH_FIM_TOKENS", "24"))

HTTP_TIMEOUT = float(os.environ.get("AIZSH_HTTP_TIMEOUT", "20"))
SOCK_PATH    = os.environ.get("AIZSH_SOCK") or os.path.join(
    os.environ.get("TMPDIR", "/tmp"), f"ai-zsh-{os.getuid()}.sock")
DEBUG        = os.environ.get("AIZSH_DEBUG", "") not in ("", "0", "false")

CACHE_TTL    = float(os.environ.get("AIZSH_CACHE_TTL", "45"))   # ghost cache seconds
CTX_TTL      = float(os.environ.get("AIZSH_CTX_TTL", "8"))      # context cache seconds
ZOXIDE_TTL   = 30.0
RATE_MAX     = int(os.environ.get("AIZSH_RATE_MAX", "12"))      # max API calls / 60s (free tier ~15 RPM)
IDLE_EXIT    = float(os.environ.get("AIZSH_IDLE_EXIT", "10800"))  # daemon self-exit (s)

US = "\x1f"
OS_STR = f"macOS {platform.mac_ver()[0]}" if platform.mac_ver()[0] else platform.system()


def log(*a):
    if DEBUG:
        sys.stderr.write("[ai-zsh] " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()


# --------------------------------------------------------------------------- #
# Gemini HTTP (pooled keep-alive connections)                                  #
# --------------------------------------------------------------------------- #
_POOL = []
_POOL_LOCK = threading.Lock()
_SSL_CTX = ssl.create_default_context()


def _get_conn():
    with _POOL_LOCK:
        if _POOL:
            return _POOL.pop()
    return http.client.HTTPSConnection(GEMINI_HOST, timeout=HTTP_TIMEOUT, context=_SSL_CTX)


def _put_conn(c):
    with _POOL_LOCK:
        if len(_POOL) < 4:
            _POOL.append(c)
            return
    try:
        c.close()
    except Exception:
        pass


def _extract_text(j):
    try:
        parts = j["candidates"][0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    except (KeyError, IndexError, TypeError):
        return ""


def _do_request(model, body_bytes):
    """POST to generateContent. Returns (text, status, err)."""
    path = f"/v1beta/models/{model}:generateContent?key={API_KEY}"
    headers = {"Content-Type": "application/json", "Connection": "keep-alive"}
    last_err = "unknown"
    for attempt in range(2):
        c = _get_conn()
        try:
            c.request("POST", path, body=body_bytes, headers=headers)
            r = c.getresponse()
            raw = r.read()
            status = r.status
            if status == 200:
                _put_conn(c)
                try:
                    return (_extract_text(json.loads(raw)), 200, None)
                except json.JSONDecodeError as e:
                    return ("", 200, f"json: {e}")
            c.close()
            last_err = raw[:400].decode("utf-8", "replace")
            if status >= 500 and attempt == 0:
                continue
            return ("", status, last_err)
        except (http.client.HTTPException, OSError, ssl.SSLError) as e:
            try:
                c.close()
            except Exception:
                pass
            last_err = str(e)
            if attempt == 0:
                continue
    return ("", 0, last_err)


def _supports_thinking(model):
    # thinkingConfig is only valid on 2.5+/3.x; 2.0 rejects it with 400.
    return bool(re.search(r"(2\.5|gemini-3|3\.\d|3-)", model))


def _friendly(status, err):
    if status == 429:
        return "quota / rate-limit hit (Gemini free tier) — retry in a moment"
    if status == 403:
        return "this API key's project is denied access to this model"
    if status == 401 or status == 400 and "API key" in (err or ""):
        return "invalid GEMINI_API_KEY"
    return (err or "request failed")[:140]


def _gemini_generate(model, system, user, schema=None, max_tokens=64, temp=0.2, stop=None):
    """Call Gemini. thinkingBudget=0 for speed where supported; retry without it on 400."""
    if not API_KEY:
        return "", "no GEMINI_API_KEY set"
    gen = {"temperature": temp, "maxOutputTokens": max_tokens, "candidateCount": 1}
    if stop:
        gen["stopSequences"] = stop
    if schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = schema
    if _supports_thinking(model):
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": gen,
    }
    text, status, err = _do_request(model, json.dumps(body).encode("utf-8"))
    if status == 400 and "thinkingConfig" in gen:
        gen.pop("thinkingConfig", None)
        text, status, err = _do_request(model, json.dumps(body).encode("utf-8"))
    if status != 200:
        log("gemini", model, "status", status, "err", err)
        return "", _friendly(status, err)
    return text, None


def _friendly_local(status, raw):
    msg = raw[:200].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    if status == 404:
        return f"model not available — run:  ollama pull {GHOST_MODEL}  (and {PROMPT_MODEL})"
    return f"local model error (HTTP {status}): {msg}"[:200]


def _is_thinking_model(model):
    m = (model or "").lower()
    return ("qwen3" in m and "coder" not in m) or "deepseek-r1" in m or "qwq" in m


def _ollama_generate(model, system, user, schema=None, max_tokens=64, temp=0.2,
                     stop=None, think=False):
    """Call a local Ollama server (/api/generate). keep_alive holds the model in RAM."""
    capable = _is_thinking_model(model)
    thinking = think and THINK_ENABLED and capable
    opts = {"temperature": temp, "num_predict": max_tokens}
    if stop:
        opts["stop"] = stop
    if thinking:
        # Qwen3 thinking: give it room and non-greedy sampling (greedy => repetition).
        opts["num_predict"] = max(max_tokens, THINK_TOKENS)
        opts["temperature"] = 0.6
        opts["top_p"] = 0.95
        opts["top_k"] = 20
        opts.pop("stop", None)
    body = {"model": model, "system": system, "prompt": user, "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE, "options": opts}
    # Qwen3/3.5 think by DEFAULT — set explicitly so think=false truly disables it.
    if capable:
        body["think"] = thinking
    # format=json + thinking puts the JSON into the 'thinking' field (response empty),
    # so only constrain output when NOT thinking; otherwise rely on _parse_json.
    if schema is not None and not thinking:
        body["format"] = "json"
    host, port = _hostport(OLLAMA_URL, 11434)
    tid = threading.get_ident()
    try:
        c = http.client.HTTPConnection(host, port,
                                       timeout=THINK_TIMEOUT if thinking else HTTP_TIMEOUT)
        _ACTIVE_CONN[tid] = c
        c.request("POST", "/api/generate", json.dumps(body).encode("utf-8"),
                  {"Content-Type": "application/json"})
        r = c.getresponse(); raw = r.read(); st = r.status; c.close()
    except (OSError, http.client.HTTPException) as e:
        return "", f"can't reach Ollama at {OLLAMA_URL} ({e}); is `ollama serve` running?"
    finally:
        _ACTIVE_CONN.pop(tid, None)
    if st != 200:
        log("ollama", model, "status", st)
        return "", _friendly_local(st, raw)
    try:
        return json.loads(raw).get("response", "").strip(), None
    except ValueError as e:
        return "", f"bad Ollama response: {e}"


def _openai_generate(model, system, user, schema=None, max_tokens=64, temp=0.2, stop=None):
    """Call any OpenAI-compatible server (llama.cpp, LM Studio, vLLM, Ollama /v1)."""
    url = OPENAI_BASE + ("/chat/completions" if OPENAI_BASE.endswith("/v1")
                         else "/v1/chat/completions")
    u = urlparse(url)
    tls = u.scheme == "https"
    body = {"model": model, "stream": False, "temperature": temp,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if stop:
        body["stop"] = stop
    if schema is not None:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if OPENAI_KEY:
        headers["Authorization"] = "Bearer " + OPENAI_KEY
    try:
        cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
        c = cls(u.hostname, u.port or (443 if tls else 80), timeout=HTTP_TIMEOUT)
        c.request("POST", u.path, json.dumps(body).encode("utf-8"), headers)
        r = c.getresponse(); raw = r.read(); st = r.status; c.close()
    except (OSError, http.client.HTTPException) as e:
        return "", f"can't reach OpenAI-compatible server at {OPENAI_BASE} ({e})"
    if st != 200:
        return "", _friendly_local(st, raw)
    try:
        return json.loads(raw)["choices"][0]["message"]["content"].strip(), None
    except (ValueError, KeyError, IndexError) as e:
        return "", f"bad response: {e}"


def generate(model, system, user, schema=None, max_tokens=64, temp=0.2, stop=None, think=False):
    """Provider-agnostic generation. Returns (text, err). `think` only affects Ollama."""
    if PROVIDER == "ollama":
        return _ollama_generate(model, system, user, schema, max_tokens, temp, stop, think)
    if PROVIDER == "openai":
        return _openai_generate(model, system, user, schema, max_tokens, temp, stop)
    return _gemini_generate(model, system, user, schema, max_tokens, temp, stop)


# --------------------------------------------------------------------------- #
# Context gathering (cached per pwd)                                            #
# --------------------------------------------------------------------------- #
_CTX_CACHE = {}
_CTX_LOCK = threading.Lock()
_ZOX = {"t": 0.0, "v": []}


def _run(cmd, cwd=None, timeout=1.5):
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _ls(pwd):
    try:
        names = sorted(os.listdir(pwd))
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(".") and n not in (".env", ".git"):
            continue
        try:
            n = n + "/" if os.path.isdir(os.path.join(pwd, n)) else n
        except OSError:
            pass
        out.append(n[:60])
        if len(out) >= (18 if COMPACT else 40):
            break
    return out


def _git(pwd):
    if not shutil.which("git"):
        return ""
    branch = _run(["git", "-C", pwd, "rev-parse", "--abbrev-ref", "HEAD"])
    if not branch:
        return ""
    status = _run(["git", "-C", pwd, "status", "--porcelain"])
    n = len([x for x in status.splitlines() if x.strip()]) if status else 0
    return f"{branch} ({n} uncommitted)" if n else branch


def _zoxide():
    now = time.time()
    if now - _ZOX["t"] < ZOXIDE_TTL:
        return _ZOX["v"]
    v = []
    if shutil.which("zoxide"):
        out = _run(["zoxide", "query", "-l"], timeout=1.0)
        v = [line for line in out.splitlines() if line][:15]
    _ZOX["t"] = now
    _ZOX["v"] = v
    return v


_HOME = os.path.expanduser("~")


def _abbrev(p):
    return "~" + p[len(_HOME):] if p == _HOME or p.startswith(_HOME + "/") else p


def _read(path, limit=20000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _make_targets(pwd):
    out = []
    for fn in ("Makefile", "makefile", "GNUmakefile"):
        p = os.path.join(pwd, fn)
        if os.path.exists(p):
            for line in _read(p).splitlines():
                m = re.match(r"^([a-zA-Z0-9][a-zA-Z0-9_.\-]*)\s*:(?!=)", line)
                if m and not m.group(1).startswith("."):
                    out.append("make " + m.group(1))
                if len(out) >= 12:
                    break
            break
    return out


def _just_recipes(pwd):
    out = []
    for fn in ("justfile", ".justfile", "Justfile"):
        p = os.path.join(pwd, fn)
        if os.path.exists(p):
            for line in _read(p).splitlines():
                m = re.match(r"^([a-zA-Z0-9][a-zA-Z0-9_\-]*)(\s+[^:=]*)?:\s*(#.*)?$", line)
                if m and m.group(1) not in ("set", "import"):
                    out.append("just " + m.group(1))
                if len(out) >= 12:
                    break
            break
    return out


def _project(pwd):
    """Detect the folder's ecosystem(s) and the commands you can actually run here."""
    types, targets = [], []

    def has(*names):
        return any(os.path.exists(os.path.join(pwd, n)) for n in names)

    if has("package.json"):
        mgr = ("pnpm" if has("pnpm-lock.yaml") else "yarn" if has("yarn.lock")
               else "bun" if has("bun.lockb") else "npm")
        types.append(f"Node.js ({mgr})")
        try:
            pj = json.loads(_read(os.path.join(pwd, "package.json")) or "{}")
            run = "%s run" % ("npm" if mgr == "npm" else mgr)
            for s in list(pj.get("scripts", {}).keys())[:12]:
                targets.append(f"{run} {s}")
        except (ValueError, TypeError):
            pass
    if has("pyproject.toml"):
        types.append("Python (pyproject)")
    elif has("requirements.txt", "setup.py", "Pipfile"):
        types.append("Python")
    if has(".venv", "venv"):
        types.append("venv present")
    if has("Cargo.toml"):
        types.append("Rust")
        targets += ["cargo build", "cargo test", "cargo run"]
    if has("go.mod"):
        types.append("Go")
        targets += ["go build ./...", "go test ./..."]
    if has("Makefile", "makefile", "GNUmakefile"):
        types.append("Make")
        targets += _make_targets(pwd)
    if has("justfile", ".justfile", "Justfile"):
        types.append("just")
        targets += _just_recipes(pwd)
    if has("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        types.append("docker compose")
    elif has("Dockerfile"):
        types.append("Docker")
    if has("Gemfile"):
        types.append("Ruby (bundler)")
    if has("pom.xml"):
        types.append("Maven")
    elif has("build.gradle", "build.gradle.kts"):
        types.append("Gradle")

    seen, uniq = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return {"types": types, "targets": uniq[:16]}


def context(pwd):
    pwd = pwd or os.getcwd()
    now = time.time()
    with _CTX_LOCK:
        hit = _CTX_CACHE.get(pwd)
        if hit and now - hit[0] < CTX_TTL:
            return hit[1]
    ctx = {"files": _ls(pwd), "git": _git(pwd), "zoxide": _zoxide(),
           "os": OS_STR, "project": _project(pwd)}
    with _CTX_LOCK:
        _CTX_CACHE[pwd] = (now, ctx)
        if len(_CTX_CACHE) > 64:
            _CTX_CACHE.pop(next(iter(_CTX_CACHE)))
    return ctx


# --------------------------------------------------------------------------- #
# Rate limiting + ghost response cache                                         #
# --------------------------------------------------------------------------- #
_CALLS = []
_RATE_LOCK = threading.Lock()


def rate_ok():
    if RATE_MAX <= 0:        # 0 = unlimited (sensible for local providers)
        return True
    now = time.time()
    with _RATE_LOCK:
        while _CALLS and now - _CALLS[0] > 60:
            _CALLS.pop(0)
        if len(_CALLS) >= RATE_MAX:
            return False
        _CALLS.append(now)
        return True


_GCACHE = {}
_GCACHE_LOCK = threading.Lock()


def cache_get(key):
    now = time.time()
    with _GCACHE_LOCK:
        hit = _GCACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    return None


def cache_put(key, val):
    with _GCACHE_LOCK:
        _GCACHE[key] = (time.time(), val)
        if len(_GCACHE) > 256:
            _GCACHE.pop(next(iter(_GCACHE)))


def cache_prefix_get(pwd, buffer):
    """If `buffer` is the user typing *into* a line we already completed
    (`base + remainder`), return the leftover tail without calling the model —
    so typing exactly what the ghost shows never regenerates. Returns None when
    the buffer has diverged from every cached completion (then we do regenerate)."""
    now = time.time()
    best_base = -1
    best = None
    with _GCACHE_LOCK:
        for (kp, kb), (ts, suf) in _GCACHE.items():
            if kp != pwd or not suf or now - ts >= CACHE_TTL:
                continue
            full = kb + suf
            # buffer must extend the cached base and still be a prefix of its full line
            if len(kb) < len(buffer) <= len(full) and \
               buffer.startswith(kb) and full.startswith(buffer):
                if len(kb) > best_base:           # most specific base wins
                    best_base, best = len(kb), full[len(buffer):]
    return best


# --------------------------------------------------------------------------- #
# Prompts                                                                       #
# --------------------------------------------------------------------------- #
TOOL_NOTE = (
    "The user has these tools installed and you SHOULD use them when the request fits:\n"
    "  - zoxide: `z <keyword>` jumps to a frecently-used directory matching the keyword "
    "(e.g. \"go to my api project\" -> `z api`). `zi` opens an interactive zoxide+fzf picker.\n"
    "  - fzf: interactive fuzzy finder for when the user must CHOOSE something, "
    "e.g. pick a file to edit -> `nvim \"$(fzf)\"`, pick a dir to cd into -> "
    "`cd \"$(zoxide query -l | fzf)\"`, pick a branch -> "
    "`git switch \"$(git branch --format='%(refname:short)' | fzf)\"`, "
    "kill a process -> `kill \"$(ps -ax | fzf | awk '{print $1}')\"`."
)

GHOST_SYSTEM = (
    "You are a fast shell-command autocompletion engine for zsh on macOS. "
    "Given the user's partial command line and context, output ONLY the text that should be "
    "appended directly after their input — the raw continuation, no quotes, no markdown, no "
    "explanation. The continuation is concatenated to the input with NO separator added, so "
    "include a leading space yourself if one is needed. Output a single line. "
    "If you cannot confidently continue, output nothing at all. "
    "Strongly prefer completions consistent with the user's recent history and current directory. "
    "When the line starts a directory change (cd/z), prefer the listed frecent dirs. "
    "When completing a task runner (npm/yarn/pnpm/bun/make/just/cargo/go/docker), prefer the "
    "exact targets listed as runnable in this folder. "
    "CRITICAL: reply with the raw continuation text only — never wrap it in quotes or "
    "backticks, never repeat the input.\n"
    "Examples (text after '=>' is the exact continuation, trailing space included):\n"
    "  input: git che    => ckout \n"
    "  input: docker     => ps -a\n"
    "  input: cd ~/Down  => loads\n"
    "  input: z          => <frecent-dir-keyword>"
)

PROMPT_SYSTEM = (
    "You translate a natural-language request into ONE shell command for zsh on macOS. "
    "Use the provided context (current folder, its project type & runnable targets, "
    "files, git, frecent dirs, recent history). Prefer commands that fit the current "
    "folder's project type and use its listed runnable targets when relevant. "
    + TOOL_NOTE +
    "\nReturn JSON only (no markdown) matching the schema. Rules:\n"
    "  - command: a single ready-to-run command line (pipes/&&/$() allowed). No surrounding backticks.\n"
    "  - explanation: <= 12 words, plain.\n"
    "  - danger: 'none' for read-only/safe; 'caution' for writes/moves/installs/network; "
    "'dangerous' for irreversible or destructive (rm -rf, dd, mkfs, git reset --hard, "
    "force push, chmod/chown -R, disk ops, anything with sudo that modifies the system).\n"
    "  - alternatives: 0-3 other plausible one-line commands, most useful first.\n"
    "Prefer the least destructive command that satisfies the request, but if the request itself "
    "calls for a destructive action, produce it (and set danger accordingly) — never refuse or "
    "substitute a no-op."
)

FIX_SYSTEM = (
    "You are a shell troubleshooting assistant for zsh on macOS. The user's previous "
    "command FAILED. Given the failed command, its exit code, and the context, return the "
    "single corrected command they most likely intended. "
    "Exit code 127 means 'command not found' — suggest the correct command name, or how to "
    "install it (e.g. `brew install <pkg>`). "
    "Common fixes: typos in the command / subcommand / flags, a missing `sudo` for permission "
    "errors, a wrong path or filename, missing arguments, or using the right tool for the job. "
    + TOOL_NOTE +
    "\nReturn JSON {command, explanation (<=12 words), danger, alternatives (0-3)}. "
    "If the command already looks correct or you cannot improve it, return an empty command. "
    "Never return the failed command unchanged."
)

PREDICT_SYSTEM = (
    "You predict the SINGLE shell command the user is most likely to run NEXT in their zsh "
    "session on macOS, from their recent command history AND the current context (folder, git "
    "state, project type & runnable targets, files). "
    "Output ONLY that one command — no explanation, no quotes, no markdown, no backticks. "
    "Use history patterns (e.g. after `git add` → `git commit`; after editing code → run the "
    "tests/build) and context even with little history (a git repo with uncommitted changes → "
    "`git status`; a Node project → its dev script; a repo behind upstream → `git pull`; a fresh "
    "Rust project → `cargo build`). "
    "When inside a project, prefer its runnable targets (dev / test / build) or a setup step "
    "(install deps) over a generic git command. "
    "If there is no reasonably likely next command, output NOTHING (an empty response) — do not "
    "invent an unlikely command just to fill space. "
    "You MAY predict a destructive or irreversible command (e.g. `rm -rf` a build/dist dir you "
    "just built, `git reset --hard` after the user clearly abandoned changes, `git push --force` "
    "on a feature branch) ONLY when it is plainly the contextually appropriate next step — never "
    "speculatively or out of nowhere. When in doubt, output nothing."
)

PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "explanation": {"type": "string"},
        "danger": {"type": "string", "enum": ["none", "caution", "dangerous"]},
        "alternatives": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["command", "explanation", "danger"],
}

# Local safety net: upgrade danger to at least 'dangerous' if a command looks destructive,
# even if the model under-rated it.
_DANGER_RE = re.compile(
    r"(\brm\s+-[a-z]*[rf]|\brm\s+-[a-z]*f|\bdd\b|\bmkfs|\bshred\b|\bgit\s+reset\s+--hard"
    r"|\bgit\s+clean\s+-[a-z]*f|push\s+.*--force|--force-with-lease|\bchmod\s+-R|\bchown\s+-R"
    r"|\b:\s*>\s*/|>\s*/dev/(sd|disk|nvme)|diskutil\s+(erase|reformat)|\bmkswap"
    r"|\bkill(all)?\s+-9|:\(\)\s*\{|sudo\s+rm|truncate\s+-s\s*0|find\b.*-delete"
    r"|\bshutdown\b|\breboot\b|>\s*~?/\.[a-z]|\bgit\s+checkout\s+--\s+\.)",
    re.IGNORECASE,
)


def classify_danger(cmd, model_says):
    if _DANGER_RE.search(cmd or ""):
        return "dangerous"
    if model_says in ("none", "caution", "dangerous"):
        return model_says
    return "caution"


# --------------------------------------------------------------------------- #
# Handlers                                                                      #
# --------------------------------------------------------------------------- #
def _ctx_block(pwd, ctx, history):
    folder = os.path.basename(pwd.rstrip("/")) or "/"
    parts = [f"OS: {ctx['os']}",
             f"Current folder: {folder}/   (full path: {_abbrev(pwd)})"]
    if ctx["git"]:
        parts.append(f"Git branch: {ctx['git']}")
    proj = ctx.get("project") or {}
    if proj.get("types"):
        parts.append("Project type: " + ", ".join(proj["types"]))
    if proj.get("targets"):
        tg = proj["targets"][:10] if COMPACT else proj["targets"]
        parts.append("Runnable in this folder: " + ", ".join(tg))
    if ctx["files"]:
        parts.append("Files here: " + ", ".join(ctx["files"]))
    if ctx["zoxide"]:
        zz = ctx["zoxide"][:8] if COMPACT else ctx["zoxide"]
        parts.append("Frecent dirs (for `z`): " + ", ".join(zz))
    if history:
        h = "\n".join(history.splitlines()[-8:]) if COMPACT else history
        parts.append("Recent commands:\n" + h)
    return "\n".join(parts)


def clean_ghost(text, buffer):
    if not text:
        return ""
    line = text.split("\n", 1)[0]
    # strip a leading code fence/backtick if the model added one
    line = line.lstrip("`")
    if line.endswith("`"):
        line = line.rstrip("`")
    # small models often wrap the whole answer in quotes (mimicking the examples)
    if len(line) >= 2 and line[0] in "\"'" and line[-1] == line[0]:
        line = line[1:-1]
    # model sometimes echoes the whole command line; keep only the new tail
    if buffer and line.startswith(buffer):
        line = line[len(buffer):]
    # ...or echoes the whole final token; strip it only when the suggestion starts
    # with the entire typed token (safe — won't corrupt a real short continuation
    # like 'git com' + 'mit'). This path is for instruct models; Ollama uses FIM.
    elif buffer and not buffer[-1].isspace():
        partial = re.search(r"\S+$", buffer).group(0)
        if line.startswith(partial):
            line = line[len(partial):]
    line = line.rstrip("\r\n")
    # avoid a double space where buffer and continuation meet
    if buffer.endswith(" ") and line.startswith(" "):
        line = line[1:]
    # drop garbage where the model restarted the command instead of continuing it
    words = buffer.split()
    if words and len(words[0]) >= 2:
        ls = line.lstrip()
        if ls == words[0] or ls.startswith(words[0] + " "):
            return ""
    if not line.strip():
        return ""
    return line


def _ollama_fim(model, prefix, suffix):
    """Raw fill-in-middle: clean token-level continuation of `prefix`."""
    p = FIM_PREFIX + prefix + FIM_SUFFIX + suffix + FIM_MIDDLE
    body = {"model": model, "prompt": p, "raw": True, "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {"temperature": 0.1, "num_predict": FIM_TOKENS,
                        "stop": ["<|endoftext|>", "<|file_sep|>", "<|fim_pad|>",
                                 FIM_PREFIX, FIM_SUFFIX, FIM_MIDDLE, "\n"]}}
    host, port = _hostport(OLLAMA_URL, 11434)
    tid = threading.get_ident()
    try:
        c = http.client.HTTPConnection(host, port, timeout=HTTP_TIMEOUT)
        _ACTIVE_CONN[tid] = c
        c.request("POST", "/api/generate", json.dumps(body).encode("utf-8"),
                  {"Content-Type": "application/json"})
        r = c.getresponse(); raw = r.read(); st = r.status; c.close()
    except (OSError, http.client.HTTPException) as e:
        return "", str(e)        # closed by the handler on cancel → lands here
    finally:
        _ACTIVE_CONN.pop(tid, None)
    if st != 200:
        log("ollama fim status", st)
        return "", _friendly_local(st, raw)
    try:
        return json.loads(raw).get("response", ""), None
    except ValueError as e:
        return "", str(e)


def _fim_preamble(pwd, ctx, history):
    """Compact context as shell comments, prepended to the FIM prefix."""
    bits = [f"dir: {_abbrev(pwd)}"]
    if ctx.get("git"):
        bits.append(f"git {ctx['git']}")
    proj = ctx.get("project") or {}
    if proj.get("types"):
        bits.append("/".join(proj["types"][:3]))
    lines = ["# " + " | ".join(bits)]
    if proj.get("targets"):
        lines.append("# run: " + ", ".join(proj["targets"][:10]))
    if ctx.get("zoxide"):
        lines.append("# dirs: " + ", ".join(ctx["zoxide"][:8]))
    if history:
        recent = [h for h in history.splitlines() if h.strip()][-5:]
        if recent:
            lines.append("# recent: " + " ; ".join(recent))
    return "\n".join(lines)


def clean_fim(text, buffer):
    if not text:
        return ""
    line = text.split("\n", 1)[0].rstrip("\r")
    for t in ("<|endoftext|>", "<|file_sep|>", "<|fim_pad|>",
              FIM_PREFIX, FIM_SUFFIX, FIM_MIDDLE):
        line = line.replace(t, "")
    if buffer.endswith(" ") and line.startswith(" "):
        line = line[1:]
    return line if line.strip() else ""


def do_ghost(buffer, pwd, history):
    buffer = buffer or ""
    if len(buffer.strip()) < 2:
        return {"suggestion": ""}
    # don't ghost-complete a natural-language `prompt ...`/`ai ...` line
    if re.match(r"^\s*(prompt|ai|ask)\s", buffer):
        return {"suggestion": ""}
    key = (pwd, buffer)
    cached = cache_get(key)
    if cached is not None:
        return {"suggestion": cached}
    # typing further into a suggestion we already made → serve the tail, no model call
    pref = cache_prefix_get(pwd, buffer)
    if pref is not None:
        cache_put(key, pref)
        return {"suggestion": pref}
    if not rate_ok():
        return {"suggestion": ""}
    ctx = context(pwd)
    if PROVIDER == "ollama" and FIM_ENABLED:
        # qwen2.5-coder does clean token-level continuation via fill-in-middle;
        # inject context as a comment preamble so it stays folder-aware.
        prefix = _fim_preamble(pwd, ctx, history) + "\n" + buffer
        text, _ = _ollama_fim(GHOST_MODEL, prefix, "")
        suffix = clean_fim(text, buffer)
    else:
        user = (_ctx_block(pwd, ctx, history) +
                "\n\nComplete this command line (output only the continuation):\n" + buffer)
        text, _ = generate(GHOST_MODEL, GHOST_SYSTEM, user,
                           max_tokens=48, temp=0.15, stop=["\n"])
        suffix = clean_ghost(text, buffer)
    cache_put(key, suffix)
    return {"suggestion": suffix}


def _command_result(text, err):
    """Parse a model JSON reply into a normalized {command, explanation, danger, alternatives}."""
    data = _parse_json(text)

    def _as_cmd(x):
        # models sometimes return a {command, explanation} object instead of a string
        if isinstance(x, dict):
            x = x.get("command") or x.get("cmd") or ""
        return str(x).strip().strip("`").strip()

    cmd = _as_cmd(data.get("command", "")) if data else ""
    if not cmd:
        msg = "no command" + (f" ({err})" if err else "")
        return {"command": "", "explanation": msg, "danger": "none", "alternatives": []}
    alts, seen = [], {cmd}
    for a in (data.get("alternatives") or []):
        s = _as_cmd(a)
        if s and s not in seen:
            seen.add(s)
            alts.append(s)
        if len(alts) >= 3:
            break
    return {
        "command": cmd,
        "explanation": str(data.get("explanation", ""))[:120],
        "danger": classify_danger(cmd, data.get("danger", "")),
        "alternatives": alts,
    }


def do_prompt(query, pwd, history):
    query = (query or "").strip()
    if not query:
        return {"command": "", "explanation": "empty request",
                "danger": "none", "alternatives": []}
    if not rate_ok():
        return {"command": "", "explanation": "rate limited — try again",
                "danger": "none", "alternatives": []}
    ctx = context(pwd)
    user = "Request: " + query + "\n\n" + _ctx_block(pwd, ctx, history)
    # `prompt` is deliberate → allow thinking (opt-in) for better accuracy on hard tasks.
    res = _command_result(*generate(PROMPT_MODEL, PROMPT_SYSTEM, user,
                                    schema=PROMPT_SCHEMA, max_tokens=512, temp=0.2,
                                    think=True))
    # thinking can run long / get cut off → never leave the user empty-handed
    if not res["command"] and THINK_ENABLED:
        res = _command_result(*generate(PROMPT_MODEL, PROMPT_SYSTEM, user,
                                        schema=PROMPT_SCHEMA, max_tokens=512, temp=0.2,
                                        think=False))
    return res


def do_fix(text, pwd, history):
    # text = "<exit_code>\x1e<failed command>"
    ec, _, cmd = (text or "").partition("\x1e")
    cmd = cmd.strip()
    if not cmd:
        return {"command": "", "explanation": "", "danger": "none", "alternatives": []}
    if not rate_ok():
        return {"command": "", "explanation": "rate limited", "danger": "none", "alternatives": []}
    ctx = context(pwd)
    user = (f"The previous command failed (exit code {ec}).\nFailed command:\n{cmd}\n\n"
            + _ctx_block(pwd, ctx, history))
    # auto-fix fires automatically → keep it fast: the always-warm coder model,
    # no thinking. (Override with AIZSH_FIX_MODEL.)
    res = _command_result(*generate(FIX_MODEL, FIX_SYSTEM, user,
                                    schema=PROMPT_SCHEMA, max_tokens=300, temp=0.2,
                                    think=False))
    # suppress no-op "fixes" that just echo the failed command back
    if res["command"] and res["command"].strip() == cmd:
        return {"command": "", "explanation": "already correct",
                "danger": "none", "alternatives": []}
    return res


def _clean_predict(text):
    if not text:
        return ""
    lines = [l for l in text.strip().splitlines()
             if l.strip() and not l.strip().startswith("```")]
    if not lines:
        return ""
    line = lines[0].strip().strip("`").strip()
    if len(line) >= 2 and line[0] in "\"'" and line[-1] == line[0]:
        line = line[1:-1].strip()
    if line.lower() in ("", "none", "n/a", "no command", "nothing", "(none)", "null"):
        return ""
    return line if 0 < len(line) <= 200 else ""


def do_predict(text, pwd, history):
    """Predict the next command from history + context. May return empty (no guess)."""
    if not rate_ok():
        return {"suggestion": ""}
    key = ("predict", pwd, history)
    cached = cache_get(key)
    if cached is not None:
        return {"suggestion": cached}
    ctx = context(pwd)
    user = (_ctx_block(pwd, ctx, history) +
            "\n\nPredict the single next command (or output nothing):")
    out, _ = generate(PREDICT_MODEL, PREDICT_SYSTEM, user,
                      max_tokens=40, temp=0.3, think=False)
    cmd = _clean_predict(out)
    cache_put(key, cmd)
    return {"suggestion": cmd}


def _parse_json(text):
    if not text:
        return None
    # drop any reasoning block a thinking model may have emitted inline
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Frame protocol                                                                #
# --------------------------------------------------------------------------- #
def _b64d(s):
    if not s:
        return ""
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:
        return ""


def handle_frame(raw):
    """raw: bytes without trailing newline. Returns response bytes (incl. newline)."""
    try:
        line = raw.decode("utf-8", "replace")
    except Exception:
        return b"\n"
    fields = line.split(US)
    mode = fields[0].strip()
    if mode == "ping":
        return b"pong\n"
    if mode == "shutdown":
        return b"bye\n"
    text = _b64d(fields[1]) if len(fields) > 1 else ""
    pwd = _b64d(fields[2]) if len(fields) > 2 else ""
    history = _b64d(fields[3]) if len(fields) > 3 else ""
    try:
        if mode == "ghost":
            result = do_ghost(text, pwd, history)
        elif mode == "prompt":
            result = do_prompt(text, pwd, history)
        elif mode == "fix":
            result = do_fix(text, pwd, history)
        elif mode == "predict":
            result = do_predict(text, pwd, history)
        else:
            result = {"error": f"unknown mode {mode}"}
    except Exception as e:  # never crash the daemon on a single bad request
        log("handler error:", repr(e))
        result = {"suggestion": "", "command": "", "explanation": f"error: {e}",
                  "danger": "none", "alternatives": []}
    payload = base64.b64encode(json.dumps(result).encode("utf-8"))
    return payload + b"\n"


# --------------------------------------------------------------------------- #
# Daemon                                                                        #
# --------------------------------------------------------------------------- #
_LAST = [time.time()]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        _LAST[0] = time.time()
        raw = line.rstrip(b"\n")
        mode = raw.split(US.encode(), 1)[0]
        if mode == b"ping":
            self._send(b"pong\n"); return
        if mode == b"shutdown":
            self._send(b"bye\n"); os._exit(0)
        # Run the work in a thread so we can ABORT it if the client disconnects —
        # e.g. you typed another key and autosuggestions killed the worker. We then
        # close the in-flight Ollama connection so it stops generating (no backlog).
        holder = {}
        t = threading.Thread(target=lambda: holder.__setitem__("r", handle_frame(raw)),
                             daemon=True)
        t.start()
        sock = self.connection
        while t.is_alive():
            try:
                ready, _, _ = select.select([sock], [], [], 0.1)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    closed = not sock.recv(1, socket.MSG_PEEK)
                except OSError:
                    closed = True
                if closed:                                  # client gone → cancel
                    conn = _ACTIVE_CONN.get(t.ident)
                    if conn:
                        try: conn.close()
                        except Exception: pass
                    return
        if "r" in holder:
            self._send(holder["r"])

    def _send(self, data):
        try:
            self.wfile.write(data); self.wfile.flush()
        except Exception:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _another_daemon_alive():
    if not os.path.exists(SOCK_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCK_PATH)
        s.sendall(b"ping\n")
        ok = s.recv(16).startswith(b"pong")
        s.close()
        return ok
    except OSError:
        return False


def _watchdog():
    while True:
        time.sleep(60)
        if time.time() - _LAST[0] > IDLE_EXIT:
            log("idle exit")
            os._exit(0)


def serve():
    if _another_daemon_alive():
        log("daemon already running")
        return
    if os.path.exists(SOCK_PATH):
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass
    old = os.umask(0o077)  # socket readable only by this user
    try:
        server = Server(SOCK_PATH, Handler)
    finally:
        os.umask(old)
    threading.Thread(target=_watchdog, daemon=True).start()
    log("listening on", SOCK_PATH, "ghost=", GHOST_MODEL, "prompt=", PROMPT_MODEL)
    try:
        server.serve_forever()
    finally:
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Entry points                                                                  #
# --------------------------------------------------------------------------- #
def _frame(mode, text, pwd="", history=""):
    enc = lambda s: base64.b64encode((s or "").encode("utf-8")).decode("ascii")
    return US.join([mode, enc(text), enc(pwd), enc(history)])


def doctor():
    print("ai-zsh doctor")
    print("  provider     :", PROVIDER)
    print("  ghost model  :", GHOST_MODEL)
    print("  prompt model :", PROMPT_MODEL)
    print("  compact ctx  :", COMPACT)
    print("  socket       :", SOCK_PATH,
          "(daemon running)" if _another_daemon_alive() else "(no daemon)")

    if PROVIDER == "ollama":
        if not _ollama_up():
            print(f"  ollama       : NOT reachable at {OLLAMA_URL}")
            print("  -> start it:  brew services start ollama   (or: ollama serve &)")
            return
        print(f"  ollama       : reachable at {OLLAMA_URL}")
        try:
            host, port = _hostport(OLLAMA_URL, 11434)
            c = http.client.HTTPConnection(host, port, timeout=3)
            c.request("GET", "/api/tags")
            tags = json.loads(c.getresponse().read()); c.close()
            have = {m["name"] for m in tags.get("models", [])}
            have |= {n.split(":")[0] for n in have}
            for m in (GHOST_MODEL, PROMPT_MODEL):
                ok = m in have or m.split(":")[0] in have
                print(f"  model        : {m} " +
                      ("pulled" if ok else "NOT pulled -> ollama pull " + m))
        except (OSError, ValueError) as e:
            print("  models       : couldn't list:", e)
    elif PROVIDER == "gemini":
        print("  API key      :", "set (%d chars)" % len(API_KEY) if API_KEY else "MISSING")
        if not API_KEY:
            print("  -> set GEMINI_API_KEY and retry."); return
    else:
        print("  base url     :", OPENAI_BASE or "(unset — set AIZSH_BASE_URL)")
        if not OPENAI_BASE:
            return

    print("  generation   : probing", GHOST_MODEL, "…")
    text, err = generate(GHOST_MODEL, "Reply with the single word: ok.", "ping",
                         max_tokens=8, temp=0)
    if text:
        print("  generation   : OK ->", text.strip()[:60])
    else:
        print("  generation   : FAILED ->", err)
        if PROVIDER == "ollama":
            print(f"  Fix: is the model pulled?  ollama pull {GHOST_MODEL}")
        elif PROVIDER == "gemini":
            print("  Fix: enable billing / use a key with quota ('limit: 0' = no free tier).")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    if cmd == "daemon":
        serve()
    elif cmd == "doctor":
        doctor()
    elif cmd == "oneshot":
        raw = sys.stdin.buffer.readline().rstrip(b"\n")
        sys.stdout.buffer.write(handle_frame(raw))
        sys.stdout.buffer.flush()
    elif cmd in ("ghost", "prompt"):
        text = sys.argv[2] if len(sys.argv) > 2 else ""
        pwd = sys.argv[3] if len(sys.argv) > 3 else os.getcwd()
        raw = _frame(cmd, text, pwd).encode("utf-8")
        resp = handle_frame(raw).rstrip(b"\n")
        print(json.dumps(json.loads(base64.b64decode(resp)), indent=2))
    elif cmd == "fix":
        command = sys.argv[2] if len(sys.argv) > 2 else ""
        ec = sys.argv[3] if len(sys.argv) > 3 else "1"
        pwd = sys.argv[4] if len(sys.argv) > 4 else os.getcwd()
        raw = _frame("fix", f"{ec}\x1e{command}", pwd).encode("utf-8")
        resp = handle_frame(raw).rstrip(b"\n")
        print(json.dumps(json.loads(base64.b64decode(resp)), indent=2))
    elif cmd == "predict":
        pwd = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        history = sys.argv[3] if len(sys.argv) > 3 else ""
        raw = _frame("predict", "", pwd, history).encode("utf-8")
        resp = handle_frame(raw).rstrip(b"\n")
        print(json.dumps(json.loads(base64.b64decode(resp)), indent=2))
    elif cmd == "ping":
        print("pong" if _another_daemon_alive() else "no daemon")
    elif cmd == "stop":
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(SOCK_PATH)
            s.sendall(b"shutdown\n")
            s.recv(16)
            s.close()
            print("stopped")
        except OSError:
            print("not running")
    else:
        sys.stderr.write(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
