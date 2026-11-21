#!/usr/bin/env python3
import argparse
import atexit
import base64
import difflib
import html
import json
import logging
import os
import platform
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

HOME = Path.home()
ROOT = HOME / ".vezcli"
SESSIONS = ROOT / "sessions"
CONFIG_PATH = ROOT / "config.json"
KNOWLEDGE_PATH = ROOT / "knowledge.md"

MODELS = [
    ("glm-5.3-flash", "GLM 5.3 Flash", "250 tok/s"),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash", "496 tok/s"),
    ("glm-5.2", "GLM 5.2", "185 tok/s"),
    ("haiku-5.5", "Claude Haiku 5.5", "201 tok/s"),
]

KNOWN = {m[0]: (m[1], m[2]) for m in MODELS}

DEFAULT_CONFIG = {
    "provider": "coffeevps",
    "model": "glm-5.3-flash",
    "providers": {
        "coffeevps": {
            "base_url": "https://ai.coffeevps.com/v1",
            "api_key": "fasfas-83d64dbb241049d5af090cb6798e9282",
            "models": [m[0] for m in MODELS],
        }
    },
    "image_model": "mai-image-2.6-flash",
    "max_steps": 20,
    "max_tokens": 8192,
    "shell_timeout": 60,
    "permissions": "auto",
    "retry_wait": 120,
    "retry_max": 20,
    "goal_rounds": 40,
    "sub_steps": 15,
    "sub_parallel": 3,
    "compact_at": 48000,
    "mcp": {},
}

PRESETS = [
    ("OpenAI", "https://api.openai.com/v1"),
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("Groq", "https://api.groq.com/openai/v1"),
    ("DeepSeek", "https://api.deepseek.com/v1"),
    ("Together", "https://api.together.xyz/v1"),
    ("Mistral", "https://api.mistral.ai/v1"),
    ("xAI", "https://api.x.ai/v1"),
    ("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("Ollama", "http://127.0.0.1:11434/v1"),
]

COMMANDS = [
    ("models", "switch model or provider", False),
    ("connect", "add an API provider", True),
    ("new", "start a fresh session", False),
    ("sessions", "browse saved sessions", False),
    ("rename", "rename this session", True),
    ("cd", "change directory", True),
    ("shell", "run a shell command", True),
    ("image", "generate an image", True),
    ("retry", "redo the last message", False),
    ("goal", "work autonomously until it is done", True),
    ("skills", "list available skills", False),
    ("plan", "plan mode: read-only until you approve", False),
    ("undo", "revert the last file changes", False),
    ("compact", "summarize history to save tokens", False),
    ("mcp", "MCP servers: add, remove, reload", True),
    ("notify", "browser notifications on or off", False),
    ("perm", "tool permissions: auto or ask", True),
    ("theme", "accent: orange green blue pink violet white", True),
    ("font", "pixel or clean", True),
    ("export", "save chat as markdown", False),
    ("copy", "copy last reply", False),
    ("help", "show commands", False),
    ("exit", "quit", False),
]

WEB_ONLY = {"retry", "perm", "theme", "font", "goal", "skills", "plan", "undo", "compact", "mcp", "notify"}
THEMES = ["orange", "green", "blue", "pink", "violet", "white"]
GUARDED = ("run_shell", "write_file", "edit_file")
PLAN_ALLOWED = {"read_file", "list_dir", "grep_files", "search_web", "fetch_url", "use_skill", "todo_write", "spawn_agent", "ask_user", "exit_plan", "goal_done"}
VERSION = "1.1"

ALIASES = {
    "model": "models",
    "m": "models",
    "providers": "connect",
    "session": "new",
    "clear": "new",
    "history": "sessions",
    "quit": "exit",
    "q": "exit",
    "sh": "shell",
    "img": "image",
    "themes": "theme",
}

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MAX_PREVIEW = 6

SYSTEM_BASE = """You are vez, a fast terminal agent running inside the vezcli app on the user's phone.
You finish small dev and system tasks by calling tools, not by describing what to do.

Rules:
- Act first. Chain tool calls until the task is done, then reply with a one or two line summary and stop.
- When the task is done, stop. Do not run extra commands to double check unless something failed.
- Never repeat a tool call that already succeeded.
- Read a file before editing it. Use edit_file for small changes and write_file for new files or full rewrites. Always write complete files, never partial snippets.
- Do not put comments in code you write unless asked.
- Keep replies short and plain. No filler, no unrequested explanations.
- Commands must work in Termux: no sudo, pkg for system packages, pip for python, paths under $HOME.
- If something is truly ambiguous or blocked, ask one short question. Otherwise decide and go.
- Prefer paths relative to the working directory.
- Match the user's tone. Swearing from the user is normal, ignore it."""

WEB_RULES = """UI rules (web):
- Replies render as markdown. Use real markdown tables for comparisons and anything with columns. Never put tables in code blocks or fake them with plain text.
- Use ask_user whenever you need the user to choose or decide. Put all questions in one call, one entry per question, as many questions and options as you need, each with a short title and clear options. Set multi to true when several options can be picked. Never ask a choice in plain text.
- After ask_user returns, continue with the answers and do not ask again.
- Use > for short callouts and - [ ] task lists for checklists."""

AGENT_RULES = """Agent tools:
- Delegate independent subtasks with spawn_agent. Give each a complete, self-contained task because it has no memory of this chat. Call several in one message to run them in parallel. Use it for research, big searches or parallel file work, not for trivial steps.
- For web research use search_web, then fetch_url on the best results, and say which sources you used.
- Load a skill with use_skill when its description matches the task.
- For work with 3 or more steps keep a checklist with todo_write. Send the full list each time and update statuses as you go."""

SUB_RULES = """

You are a sub-agent working for a parent agent. Do only the task you were given, then reply with a concise final report of what you found or did. You cannot ask the user questions."""

GOAL_PROMPT = """GOAL: {goal}

Work on this autonomously until it is completely finished. Plan, use tools, delegate independent parts with spawn_agent, and verify your work. Do not stop to ask for confirmation. When everything is done and verified, call goal_done with a short summary."""

GOAL_NUDGE = "Continue working toward the goal. If everything is finished and verified, call goal_done with a short summary. If you are genuinely blocked, ask the user with ask_user."

STARTER_SKILLS = [
    ("termux", "Rules for running commands and installing things on Android Termux", "- No sudo and no apt. Use pkg install for system packages and pip install for python packages.\n- Home is /data/data/com.termux/files/home. Shared storage needs termux-setup-storage first.\n- Prefer python scripts over desktop-only tools. Never suggest GPU or desktop workflows.\n- Long running servers should be started in the background with nohup and logged to a file."),
    ("webdesign", "Build premium dark websites with HTML, Tailwind CDN and vanilla JS", "- Single index.html with Tailwind via CDN and a little vanilla JS. Mobile first, then scale up.\n- Black base, big confident type, generous spacing, thin 1px borders, glass headers with blur, subtle motion.\n- No lorem ipsum. Write real copy. Every section has one clear job.\n- Hover and focus states on everything clickable. Respect prefers-reduced-motion."),
    ("research", "Research a topic on the web and write a sourced summary", "1. Run search_web with 2 or 3 differently worded queries.\n2. Open the best 3 to 5 results with fetch_url. Spawn sub-agents for independent subtopics.\n3. Cross-check claims between sources and note disagreements.\n4. Answer with a short summary, then a bullet list of sources with urls."),
]

PLAN_RULES = """

PLAN MODE is on. You may only read, search and think. Do not modify files or run commands. Investigate with the read-only tools, ask the user with ask_user if something is unclear, then call exit_plan with the complete step-by-step plan in markdown. You can only build after the user approves."""

SUMMARY_PROMPT = "Summarize this conversation so work can continue from the summary alone. Keep goals, decisions, file paths touched, commands run and their outcomes, open problems and user preferences. Be dense. No preamble."

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
UA = "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"


class Cancelled(Exception):
    pass


class ApiError(RuntimeError):
    def __init__(self, msg, wait=False):
        super().__init__(msg)
        self.wait = wait


def make_diff(path, old, new):
    base = os.path.basename(path)
    lines = list(
        difflib.unified_diff(
            (old or "").splitlines(),
            new.splitlines(),
            "/dev/null" if old is None else "a/" + base,
            "b/" + base,
            lineterm="",
            n=2,
        )
    )
    return clip("\n".join(lines[:240]), 6000)


def grep_files(pattern, path):
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"bad regex: {e}", False
    root = Path(path or ".").expanduser()
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    files = [root] if root.is_file() else (x for x in root.rglob("*") if x.is_file() and not (set(x.parts) & skip))
    hits = []
    for f in files:
        try:
            if f.stat().st_size > 1_000_000:
                continue
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}:{line.strip()[:200]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n…(truncated)", True
        except Exception:
            continue
    return "\n".join(hits) or "no matches", True


def expand_mentions(text):
    files, parts = [], []
    for m in re.finditer(r"(?:^|\s)@([^\s@]+)", text or ""):
        raw = m.group(1).rstrip(",.;:!?)")
        if raw in files:
            continue
        p = Path(raw).expanduser()
        try:
            if p.is_file():
                data = p.read_text(errors="replace")
                if "\x00" in data[:2000]:
                    continue
                parts.append(f"[file: {raw}]\n```\n{clip(data, 30000)}\n```")
            elif p.is_dir():
                names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())[:200]
                parts.append(f"[dir: {raw}]\n" + "\n".join(names))
            else:
                continue
        except Exception:
            continue
        files.append(raw)
    if not parts:
        return text, []
    return text + "\n\n" + "\n\n".join(parts), files


def list_files(q, limit=30):
    base = Path(os.getcwd())
    ql = (q or "").lower()
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".npm"}
    out = []
    for root, dirs, files in os.walk(base):
        rel_root = Path(root).relative_to(base)
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))[:200]
        if len(rel_root.parts) >= 3:
            dirs[:] = []
        for name in dirs + sorted(files):
            rel = str(rel_root / name) if rel_root.parts else name
            if ql in rel.lower():
                out.append(rel + ("/" if (Path(root) / name).is_dir() else ""))
        if len(out) > 400:
            break
    out.sort(key=lambda x: (not x.lower().startswith(ql), x.count("/"), len(x)))
    return out[:limit]


def mcp_fn(server, tool_name):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool_name}")[:64]


class McpServer:
    def __init__(self, name, conf, client):
        self.name, self.conf, self.client = name, conf, client
        self.tools, self.err, self.ready = [], None, False
        self.proc, self.q, self.sid, self.n = None, queue.Queue(), None, 0
        self.lock = threading.Lock()

    def reader(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.q.put(json.loads(line))
                except Exception:
                    pass
        finally:
            self.q.put({"closed": True})

    def start(self):
        try:
            if not self.conf.get("url"):
                cmd = [self.conf["command"]] + [str(a) for a in self.conf.get("args", [])]
                env = dict(os.environ)
                env.update(self.conf.get("env", {}))
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env, start_new_session=True)
                threading.Thread(target=self.reader, daemon=True).start()
            self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "vezcli", "version": VERSION}}, 60)
            self.notify("notifications/initialized")
            self.tools = (self.rpc("tools/list", {}, 30) or {}).get("tools", [])
            self.ready = True
        except Exception as e:
            self.err = str(e)[:200] or e.__class__.__name__
            self.stop()

    def notify(self, method):
        msg = {"jsonrpc": "2.0", "method": method}
        if self.conf.get("url"):
            self.http(msg, None, 20)
        else:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def http(self, msg, mid, timeout):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        h.update(self.conf.get("headers", {}))
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        r = self.client.post(self.conf["url"], json=msg, headers=h, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:150]}")
        self.sid = r.headers.get("mcp-session-id") or self.sid
        if mid is None:
            return None
        datas = []
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        datas.append(json.loads(line[5:].strip()))
                    except Exception:
                        pass
        else:
            datas.append(r.json())
        for d in datas:
            if "method" not in d and d.get("id") == mid:
                if "error" in d:
                    raise RuntimeError(str(d["error"].get("message", d["error"]))[:200])
                return d.get("result")
        raise RuntimeError("no response from server")

    def rpc(self, method, params, timeout=120):
        with self.lock:
            self.n += 1
            mid = self.n
            msg = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
            if self.conf.get("url"):
                return self.http(msg, mid, timeout)
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            end = time.monotonic() + timeout
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{method} timed out")
                try:
                    d = self.q.get(timeout=left)
                except queue.Empty:
                    raise TimeoutError(f"{method} timed out")
                if d.get("closed"):
                    raise RuntimeError("server exited")
                if "method" not in d and d.get("id") == mid:
                    if "error" in d:
                        raise RuntimeError(str(d["error"].get("message", d["error"]))[:200])
                    return d.get("result")

    def call(self, tool_name, args):
        res = self.rpc("tools/call", {"name": tool_name, "arguments": args}, 120) or {}
        parts = [c.get("text", "") if c.get("type") == "text" else f"[{c.get('type')}]" for c in res.get("content", [])]
        return "\n".join(parts) or "(no output)", not res.get("isError")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except Exception:
                self.proc.terminate()


class McpHub:
    def __init__(self, conf, client):
        self.conf, self.client, self.servers = conf, client, {}
        atexit.register(self.stop)

    def start(self):
        for name, c in list(self.conf.items()):
            self.add(name, c)

    def add(self, name, c):
        old = self.servers.pop(name, None)
        if old:
            old.stop()
        srv = McpServer(name, c, self.client)
        self.servers[name] = srv
        threading.Thread(target=srv.start, daemon=True).start()

    def remove(self, name):
        old = self.servers.pop(name, None)
        if old:
            old.stop()

    def wait(self, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end and any(not x.ready and not x.err for x in self.servers.values()):
            time.sleep(0.2)

    def specs(self):
        out = []
        for srv in list(self.servers.values()):
            if not srv.ready:
                continue
            for t in srv.tools:
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                out.append({"type": "function", "function": {"name": mcp_fn(srv.name, t.get("name", "")), "description": (t.get("description") or "")[:900], "parameters": schema}})
        return out

    def call(self, fn, args):
        for srv in list(self.servers.values()):
            for t in srv.tools:
                if mcp_fn(srv.name, t.get("name", "")) == fn:
                    return srv.call(t["name"], args)
        return f"unknown mcp tool {fn}", False

    def status(self):
        return [
            {"name": n, "transport": "http" if x.conf.get("url") else "stdio", "ready": x.ready, "err": x.err, "tools": [t.get("name", "") for t in x.tools]}
            for n, x in self.servers.items()
        ]

    def stop(self):
        for x in list(self.servers.values()):
            x.stop()


def seed_skills():
    d = ROOT / "skills"
    if d.exists():
        return
    for name, desc, body in STARTER_SKILLS:
        sd = d / name
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")


def parse_skill(path):
    text = path.read_text(errors="replace")
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
        body = m.group(2)
    name = meta.get("name") or (path.parent.name if path.name.upper() == "SKILL.MD" else path.stem)
    return {"name": name, "description": meta.get("description", ""), "path": str(path), "body": body.strip()}


def load_skills():
    out = {}
    for d in (ROOT / "skills", Path(os.getcwd()) / ".vezcli" / "skills"):
        if not d.is_dir():
            continue
        for p in sorted(list(d.glob("*/SKILL.md")) + list(d.glob("*.md"))):
            try:
                sk = parse_skill(p)
            except Exception:
                continue
            out[sk["name"]] = sk
    return list(out.values())


def use_skill(name):
    for sk in load_skills():
        if sk["name"].lower() == str(name).strip().lower():
            return clip(sk["body"], 12000), True
    return f"no skill named {name}", False


def save_cfg(cfg):
    data = {k: v for k, v in cfg.items() if k not in ("base_url", "api_key")}
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def apply_provider(cfg):
    provs = cfg["providers"]
    if cfg.get("provider") not in provs:
        cfg["provider"] = next(iter(provs))
    p = provs[cfg["provider"]]
    cfg["base_url"] = p["base_url"].rstrip("/")
    cfg["api_key"] = p.get("api_key", "")


def load_config():
    ROOT.mkdir(parents=True, exist_ok=True)
    SESSIONS.mkdir(parents=True, exist_ok=True)
    seed_skills()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text())
        except Exception:
            raw = {}
    if raw and "providers" not in raw:
        d = DEFAULT_CONFIG["providers"]["coffeevps"]
        raw["providers"] = {
            "coffeevps": {
                "base_url": raw.get("base_url", d["base_url"]),
                "api_key": raw.get("api_key", d["api_key"]),
                "models": [m[0] for m in MODELS],
            }
        }
        raw["provider"] = "coffeevps"
    cfg.update(raw)
    if not cfg.get("providers"):
        cfg["providers"] = json.loads(json.dumps(DEFAULT_CONFIG["providers"]))
    apply_provider(cfg)
    save_cfg(cfg)
    return cfg


def fetch_models(client, base, key):
    h = {"Authorization": f"Bearer {key}"} if key else {}
    r = client.get(f"{base.rstrip('/')}/models", headers=h, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:150]}")
    d = r.json()
    items = (d.get("data") or d.get("models") or []) if isinstance(d, dict) else d
    ids = {str(x.get("id") or x.get("name") if isinstance(x, dict) else x) for x in items if x}
    return sorted(i for i in ids if i and i != "None")


def add_provider(cfg, client, name, base, key):
    name = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(name).strip()).strip("-").lower()[:24]
    base = str(base).strip()
    if not name or not base.startswith("http"):
        raise ValueError("need a name and an http(s) base url")
    models, err = [], None
    try:
        models = fetch_models(client, base, key)
    except Exception as e:
        err = str(e)[:160]
    cfg["providers"][name] = {"base_url": base.rstrip("/"), "api_key": key, "models": models}
    save_cfg(cfg)
    return name, len(models), err


def refresh_provider(cfg, client, name):
    p = cfg["providers"].get(name)
    if not p:
        raise ValueError(f"unknown provider {name}")
    p["models"] = fetch_models(client, p["base_url"], p.get("api_key", ""))
    save_cfg(cfg)
    return len(p["models"])


def remove_provider(cfg, name):
    if name not in cfg["providers"]:
        raise ValueError(f"unknown provider {name}")
    if len(cfg["providers"]) < 2:
        raise ValueError("can't remove the only provider")
    del cfg["providers"][name]
    if cfg["provider"] == name:
        cfg["provider"] = next(iter(cfg["providers"]))
        ms = cfg["providers"][cfg["provider"]].get("models") or []
        cfg["model"] = ms[0] if ms else cfg["model"]
    apply_provider(cfg)
    save_cfg(cfg)


def select_model(cfg, provider, model):
    if provider not in cfg["providers"]:
        raise ValueError(f"unknown provider {provider}")
    if not str(model).strip():
        raise ValueError("missing model")
    cfg["provider"] = provider
    cfg["model"] = str(model).strip()
    apply_provider(cfg)
    save_cfg(cfg)


def resolve_model(cfg, arg):
    arg = arg.strip()
    if ":" in arg:
        head, tail = arg.split(":", 1)
        if head in cfg["providers"]:
            return head, tail
    cur = cfg["provider"]
    if arg in cfg["providers"][cur].get("models", []):
        return cur, arg
    for name, p in cfg["providers"].items():
        if arg in p.get("models", []):
            return name, arg
    return cur, arg


def parse_connect(arg):
    p = str(arg).split()
    if not p:
        return None
    pre = {n.lower(): u for n, u in PRESETS}
    if p[0].lower() in pre and (len(p) < 2 or not p[1].startswith("http")):
        return p[0].lower(), pre[p[0].lower()], p[1] if len(p) > 1 else ""
    if len(p) >= 2 and p[1].startswith("http"):
        return p[0], p[1], p[2] if len(p) > 2 else ""
    return None


def restore_model(cfg, sess):
    prov, mid = sess.get("provider"), sess.get("model")
    if not mid:
        return False
    if prov not in cfg["providers"]:
        prov = next((n for n, p in cfg["providers"].items() if mid in p.get("models", [])), None)
    if not prov:
        return False
    cfg["provider"], cfg["model"] = prov, mid
    apply_provider(cfg)
    return True


def model_label(cfg, mid):
    return mid if len(cfg["providers"]) < 2 else f"{cfg['provider']} · {mid}"


def export_md(sess, events):
    out = [f"# {sess.get('title', 'session')}", ""]
    for e in events:
        k = e.get("t")
        if k == "user":
            out += ["## You", "", e["text"], ""]
        elif k == "ai":
            out += ["## Assistant", "", e["text"], ""]
        elif k == "tool":
            out += [f"> `{e['name']}` {e['summary']}", ""]
    return "\n".join(out)


def export_file(sess, events):
    if not events:
        return "nothing to export"
    slug = re.sub(r"[^a-z0-9]+", "-", str(sess.get("title", "session")).lower()).strip("-")[:40] or "session"
    path = Path(os.getcwd()) / f"{slug}-{sess['id']}.md"
    path.write_text(export_md(sess, events))
    return f"exported → {path}"


def clean(s):
    s = ANSI.sub("", s or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return CTRL.sub("", s)


def clip(s, n):
    return s if len(s) <= n else s[:n] + f"\n…[{len(s) - n} chars cut]"


def fmt_tok(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def fmt_time(s):
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def load_knowledge():
    parts = []
    for p in (KNOWLEDGE_PATH, Path(os.getcwd()) / "AGENTS.md"):
        try:
            if p.is_file():
                t = p.read_text(errors="replace").strip()
                if t:
                    parts.append(f"## {p.name}\n{t[:8000]}")
        except Exception:
            pass
    return "\n\n".join(parts)


def build_system(web=False, agents=False):
    termux = "com.termux" in str(HOME) or bool(os.environ.get("TERMUX_VERSION"))
    env = [
        f"cwd: {os.getcwd()}",
        f"home: {HOME}",
        f"date: {datetime.now():%Y-%m-%d %H:%M}",
        f"platform: {'Android/Termux' if termux else platform.system()}",
    ]
    out = SYSTEM_BASE + "\n\nEnvironment:\n" + "\n".join(env)
    if web:
        out += "\n\n" + WEB_RULES
    if agents:
        out += "\n\n" + AGENT_RULES
    k = load_knowledge()
    if k:
        out += "\n\nKnowledge:\n" + k
    sk = load_skills()
    if sk:
        out += "\n\nSkills (load one with use_skill before relevant work):\n" + "\n".join(f"- {x['name']}: {x['description'][:160]}" for x in sk)
        out += f"\nTo create a skill write {ROOT / 'skills'}/<name>/SKILL.md with name and description frontmatter."
    return out


def tool(name, desc, props, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


S = {"type": "string"}
TOOLS = [
    tool("run_shell", "Run a shell command in the working directory and return combined output", {"command": S, "cwd": S}, ["command"]),
    tool("read_file", "Read a text file", {"path": S}, ["path"]),
    tool("write_file", "Create or overwrite a file with its full content", {"path": S, "content": S}, ["path", "content"]),
    tool("edit_file", "Replace exactly one occurrence of old_text with new_text in a file", {"path": S, "old_text": S, "new_text": S}, ["path", "old_text", "new_text"]),
    tool("list_dir", "List a directory", {"path": S}, []),
    tool("search_web", "Search the web and return top results", {"query": S}, ["query"]),
    tool("generate_image", "Generate an image from a prompt and save it to a file", {"prompt": S, "path": S}, ["prompt"]),
    tool("fetch_url", "Fetch a web page and return its readable text", {"url": S}, ["url"]),
    tool("use_skill", "Load the full instructions of a named skill", {"name": S}, ["name"]),
    tool("grep_files", "Search file contents with a regex under a path", {"pattern": S, "path": S}, ["pattern"]),
]


ASK_TOOL = tool(
    "ask_user",
    "Ask the user one or more multiple-choice questions as an interactive poll and wait for the answers. No limit on questions or options.",
    {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": S,
                    "options": {"type": "array", "items": S},
                    "multi": {"type": "boolean"},
                    "other": {"type": "boolean"},
                },
                "required": ["title", "options"],
            },
        }
    },
    ["questions"],
)


SPAWN_TOOL = tool(
    "spawn_agent",
    "Delegate a self-contained task to a sub-agent with its own context and tools. Returns its final report. Call several in one message to run them in parallel.",
    {"task": S, "name": S},
    ["task"],
)

GOAL_TOOL = tool("goal_done", "Call when the active goal is completely finished and verified", {"summary": S}, ["summary"])

TODO_TOOL = tool(
    "todo_write",
    "Create or update the visible checklist for the current work. Always send the full list.",
    {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"content": S, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
                "required": ["content", "status"],
            },
        }
    },
    ["todos"],
)

EXIT_PLAN_TOOL = tool("exit_plan", "Present the finished plan to the user for approval and leave plan mode when approved", {"plan": S}, ["plan"])


def run_shell(command, cwd, timeout, cancel=None):
    try:
        p = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd or os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except Exception as e:
        return str(e), False
    end = time.monotonic() + timeout
    while True:
        try:
            out, _ = p.communicate(timeout=0.3)
            break
        except subprocess.TimeoutExpired:
            stopped = bool(cancel and cancel.is_set())
            if stopped or time.monotonic() > end:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()
                out, _ = p.communicate()
                reason = "stopped" if stopped else f"timed out after {timeout}s"
                return (out or "").rstrip() + f"\n({reason})", False
    out = (out or "").rstrip() or "(no output)"
    if p.returncode != 0:
        out += f"\n(exit {p.returncode})"
    return out, p.returncode == 0


def read_file(path):
    p = Path(path).expanduser()
    if not p.is_file():
        return f"not a file: {path}", False
    data = p.read_text(errors="replace")
    if len(data) > 60000:
        data = data[:60000] + "\n…truncated"
    return data, True


def write_file(path, content):
    if not path:
        return "missing path", False
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    n = content.count("\n") + 1
    return f"wrote {path} ({n} lines)", True


def edit_file(path, old, new):
    p = Path(path).expanduser()
    if not p.is_file():
        return f"not a file: {path}", False
    data = p.read_text(errors="replace")
    n = data.count(old) if old else 0
    if n == 0:
        return "old_text not found", False
    if n > 1:
        return f"old_text matches {n} places, add more context", False
    p.write_text(data.replace(old, new, 1))
    return f"edited {path}", True


def list_dir(path):
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"not a directory: {path}", False
    items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = [x.name + ("/" if x.is_dir() else "") for x in items[:300]]
    if len(items) > 300:
        lines.append(f"… +{len(items) - 300} more")
    return "\n".join(lines) or "(empty)", True


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def unwrap(href):
    href = html.unescape(href)
    q = parse_qs(urlparse(href).query)
    return unquote(q["uddg"][0]) if "uddg" in q else href


def ddg_parse(text):
    rows = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', text, re.S)
    return [(h, strip_tags(t), strip_tags(sn)) for h, t, sn in rows]


def ddg_lite_parse(text):
    links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>', text, re.S)
    snips = re.findall(r'class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>', text, re.S)
    return [(h, strip_tags(t), strip_tags(snips[i]) if i < len(snips) else "") for i, (h, t) in enumerate(links)]


def wiki_search(client, query):
    r = client.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 5},
        headers={"User-Agent": UA},
        timeout=15,
    )
    items = r.json().get("query", {}).get("search", [])
    return [(f"https://en.wikipedia.org/wiki/{quote(i['title'].replace(' ', '_'))}", i["title"], strip_tags(i.get("snippet", ""))) for i in items]


def search_web(client, query):
    if not query:
        return "missing query", False
    hdr = {"User-Agent": UA}
    attempts = (
        lambda: ddg_parse(client.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=hdr, follow_redirects=True, timeout=20).text),
        lambda: ddg_lite_parse(client.post("https://lite.duckduckgo.com/lite/", data={"q": query}, headers=hdr, follow_redirects=True, timeout=20).text),
        lambda: wiki_search(client, query),
    )
    for fn in attempts:
        try:
            rows = fn()
        except Exception:
            continue
        if rows:
            return "\n\n".join(f"{t}\n{unwrap(h)}\n{sn}" for h, t, sn in rows[:8]), True
    return "no results", False


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article|pre)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def fetch_url(client, url):
    if not re.match(r"https?://", url or ""):
        return "need an http(s) url", False
    try:
        r = client.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=25)
    except Exception as e:
        return f"fetch failed: {e}", False
    text = r.text
    if "html" in r.headers.get("content-type", "") or text[:300].lstrip().lower().startswith(("<!doctype", "<html")):
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        text = (strip_tags(title.group(1)) + "\n\n" if title else "") + html_to_text(text)
    return clip(text, 12000), r.status_code < 400


def generate_image(client, cfg, prompt, path):
    if not prompt:
        return "missing prompt", False
    try:
        r = client.post(
            f"{cfg['base_url']}/images/generations",
            headers={"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {},
            json={"model": cfg["image_model"], "prompt": prompt, "n": 1},
            timeout=180,
        )
        if r.status_code >= 400:
            return f"{r.status_code} {r.text[:200]}", False
        d = (r.json().get("data") or [{}])[0]
        if d.get("b64_json"):
            data = base64.b64decode(d["b64_json"])
        elif d.get("url"):
            data = client.get(d["url"], timeout=120, follow_redirects=True).content
        else:
            return "no image in response", False
        out = Path(path or f"image-{int(time.time())}.png").expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return f"saved {out}", True
    except Exception as e:
        return f"image failed: {e}", False


def norm_calls(tcs):
    out = []
    for tc in tcs:
        fn = tc.get("function") or {}
        args, err = parse_args(fn.get("arguments"))
        out.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", "name": fn.get("name", ""), "args": args, "err": err})
    return out


def parse_args(raw):
    if isinstance(raw, dict):
        return raw, None
    if not raw:
        return {}, None
    try:
        v = json.loads(raw)
        return (v if isinstance(v, dict) else {}), None
    except Exception as e:
        return {}, f"invalid JSON arguments: {e}"


def describe(name, a):
    if name in ("run_shell", "shell"):
        return "$ " + " ".join(str(a.get("command", "")).split())
    if name in ("read_file", "list_dir", "edit_file"):
        return str(a.get("path") or ".")
    if name == "write_file":
        n = str(a.get("content", "")).count("\n") + 1
        return f"{a.get('path', '')} ({n} lines)"
    if name == "search_web":
        return str(a.get("query", ""))
    if name == "fetch_url":
        return str(a.get("url", ""))
    if name == "use_skill":
        return str(a.get("name", ""))
    if name == "spawn_agent":
        return str(a.get("name") or a.get("task", ""))[:60]
    if name == "goal_done":
        return str(a.get("summary", ""))[:80]
    if name == "grep_files":
        return str(a.get("pattern", ""))
    if name == "todo_write":
        return f"{len(a.get('todos') or [])} items"
    if name == "exit_plan":
        return "plan"
    if name in ("generate_image", "image"):
        return str(a.get("prompt", ""))[:80]
    return json.dumps(a)[:80]


def tool_text(name, summary, state, result, dur):
    t = Text(no_wrap=True, overflow="ellipsis")
    icon, color = {"run": ("●", "#8a8a8a"), "ok": ("✓", "#6fbf73"), "err": ("✗", "#e06c6c")}[state]
    t.append(f"{icon} ", style=color)
    t.append(name, style="bold #ffffff")
    t.append("  " + summary, style="#8a8a8a")
    if dur is not None:
        t.append(f"  {fmt_time(dur)}", style="#555555")
    if result:
        lines = result.strip("\n").splitlines() or [""]
        for ln in lines[:MAX_PREVIEW]:
            t.append("\n")
            t.append(ln.replace("\t", "  ")[:160], style="#777777")
        if len(lines) > MAX_PREVIEW:
            t.append(f"\n… +{len(lines) - MAX_PREVIEW} lines", style="#555555")
    return t


def meta_text(m):
    mark = "~" if m.get("est") else ""
    parts = [
        m.get("model", ""),
        fmt_time(m["time"]),
        f"{mark}{fmt_tok(m['ct'])} tok",
        f"{m['tps']:.0f} tok/s",
        f"{mark}{fmt_tok(m['pt'])} in",
    ]
    if m.get("turn"):
        parts.append(f"turn {fmt_time(m['turn'])} / {m['calls']} calls")
    t = Text()
    for i, p in enumerate(parts):
        if i:
            t.append(" · ", style="#333333")
        t.append(p, style="#5c5c5c")
    return t


def new_sess(model):
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": uuid.uuid4().hex[:8],
        "title": "new session",
        "created": now,
        "updated": now,
        "model": model,
        "cwd": os.getcwd(),
        "tokens": 0,
        "messages": [],
        "events": [],
    }


def list_sessions():
    out = []
    for p in SESSIONS.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            out.append(
                {
                    "id": d["id"],
                    "title": d.get("title", "untitled"),
                    "updated": d.get("updated", ""),
                    "count": sum(1 for m in d.get("messages", []) if m.get("role") == "user"),
                }
            )
        except Exception:
            continue
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out[:50]


CSS = """
Screen { background: #000000; }
#header { height: 1; padding: 0 1; background: #0b0b0b; }
#chat { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; scrollbar-color: #2a2a2a; scrollbar-background: #000000; }
.user { margin: 1 0 0 0; padding: 0 1; background: #141414; border-left: heavy #ffffff; color: #ffffff; }
.ai { margin: 1 0 0 0; padding: 0 1; color: #d6d6d6; }
.meta { padding: 0 1; color: #555555; }
.tool { margin: 1 0 0 0; padding: 0 1; background: #0d0d0d; border-left: heavy #333333; color: #8a8a8a; }
.note { margin: 1 0 0 0; padding: 0 1; color: #7a7a7a; }
.err { margin: 1 0 0 0; padding: 0 1; border-left: heavy #e06c6c; color: #e06c6c; }
#menu { display: none; height: auto; max-height: 8; margin: 0 1; padding: 0 1; border: none; background: #101010; }
#box { height: auto; margin: 0 1; padding: 0 1; border: round #3a3a3a; background: #0a0a0a; border-title-color: #6a6a6a; border-subtitle-color: #a0a0a0; }
#box:focus-within { border: round #8a8a8a; }
#prompt { height: 1; padding: 0; border: none; background: #0a0a0a; color: #ffffff; }
#prompt:focus { border: none; background: #0a0a0a; }
#prompt > .input--placeholder { color: #555555; }
OptionList > .option-list--option-highlighted { background: #2a2a2a; color: #ffffff; text-style: bold; }
OptionList:focus > .option-list--option-highlighted { background: #2a2a2a; color: #ffffff; text-style: bold; }
ModelPicker, SessionPicker { align: center middle; background: #000000 70%; }
#picker { width: 92%; max-width: 64; height: auto; max-height: 80%; padding: 0 1; background: #0c0c0c; border: round #4a4a4a; border-title-color: #ffffff; border-subtitle-color: #666666; }
#plist { height: auto; max-height: 18; padding: 0; border: none; background: #0c0c0c; }
"""


class Chat(VerticalScroll, can_focus=False):
    pass


class SlashMenu(OptionList, can_focus=False):
    pass


class ToolCard(Static):
    def __init__(self, name, summary, state="run", result="", dur=None):
        super().__init__(tool_text(name, summary, state, result, dur), classes="tool")
        self.tool_name = name
        self.tool_summary = summary

    def finish(self, result, ok, dur):
        stick = self.app.stick()
        self.update(tool_text(self.tool_name, self.tool_summary, "ok" if ok else "err", result, dur))
        if stick:
            self.app.call_after_refresh(self.app.scroll_chat)


class ModelPicker(ModalScreen[tuple | None]):
    BINDINGS = [Binding("escape", "close", show=False)]

    def __init__(self, entries, current):
        super().__init__()
        self.entries = entries
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield OptionList(id="plist")

    def on_mount(self):
        self.query_one("#picker").border_title = "models"
        lst = self.query_one("#plist", OptionList)
        for prov, mid in self.entries:
            mark = "● " if (prov, mid) == self.current else "  "
            label, speed = KNOWN.get(mid, (mid, ""))
            lst.add_option(
                Option(Text.assemble((mark, "#ffffff"), (label, "bold #ffffff"), "  ", (prov + (f" · {speed}" if speed else ""), "#666666")))
            )
        lst.highlighted = next((i for i, e in enumerate(self.entries) if e == self.current), 0)
        lst.focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event):
        event.stop()
        self.dismiss(self.entries[event.option_index])

    def action_close(self):
        self.dismiss(None)


class SessionPicker(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("d", "remove", show=False),
    ]

    def __init__(self, items):
        super().__init__()
        self.items = items

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield OptionList(id="plist")

    def fill(self):
        lst = self.query_one("#plist", OptionList)
        lst.clear_options()
        if not self.items:
            lst.add_option(Option(Text("no saved sessions", style="#666666"), disabled=True))
            return
        for s in self.items:
            try:
                when = datetime.fromisoformat(s["updated"]).strftime("%b %d %H:%M")
            except Exception:
                when = ""
            lst.add_option(
                Option(
                    Text.assemble(
                        (s["title"][:34], "bold #ffffff"),
                        "  ",
                        (f"{when} · {s['count']} msg", "#666666"),
                    )
                )
            )

    def on_mount(self):
        box = self.query_one("#picker")
        box.border_title = "sessions"
        box.border_subtitle = "enter open · d delete"
        self.fill()
        lst = self.query_one("#plist", OptionList)
        if self.items:
            lst.highlighted = 0
        lst.focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event):
        event.stop()
        if self.items:
            self.dismiss(self.items[event.option_index]["id"])

    def action_remove(self):
        lst = self.query_one("#plist", OptionList)
        i = lst.highlighted
        if i is None or not self.items:
            return
        try:
            (SESSIONS / f"{self.items[i]['id']}.json").unlink()
        except Exception:
            pass
        self.items.pop(i)
        self.fill()
        lst.highlighted = min(i, len(self.items) - 1) if self.items else None

    def action_close(self):
        self.dismiss(None)


class VezCLI(App):
    CSS = CSS
    TITLE = "vezcli"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+c", "interrupt", show=False, priority=True),
        Binding("ctrl+d", "quit", show=False),
        Binding("escape", "stop", show=False),
        Binding("up", "menu_up", show=False),
        Binding("down", "menu_down", show=False),
        Binding("tab", "complete", show=False, priority=True),
        Binding("pageup", "chat_up", show=False),
        Binding("pagedown", "chat_down", show=False),
    ]

    def __init__(self, cfg, resume=False):
        super().__init__()
        self.cfg = cfg
        self.cur_model = cfg["model"]
        self.client = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self.is_busy = False
        self.cancel_evt = threading.Event()
        self.busy_label = ""
        self.busy_since = 0.0
        self.frame = 0
        self.convo = []
        self.events = []
        self.sess = new_sess(self.cur_model)
        self.cmd_hist = []
        self.hist_pos = None
        self.resume = resume

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield Chat(id="chat")
        yield SlashMenu(id="menu")
        with Vertical(id="box"):
            yield Input(placeholder="message, /command or !shell", id="prompt")

    def on_mount(self):
        self.header_w = self.query_one("#header", Static)
        self.chat_w = self.query_one("#chat", Chat)
        self.menu_w = self.query_one("#menu", SlashMenu)
        self.box_w = self.query_one("#box", Vertical)
        self.prompt_w = self.query_one("#prompt", Input)
        self.box_w.border_subtitle = model_label(self.cfg, self.cur_model)
        self.box_w.border_title = self.short_cwd()
        self.refresh_header()
        self.prompt_w.focus()
        self.set_interval(0.1, self.spin)
        if self.resume:
            self.call_later(self.resume_last)
        else:
            self.welcome()

    def on_resize(self, event):
        if hasattr(self, "header_w"):
            self.refresh_header()

    def short_cwd(self):
        p = os.getcwd()
        h = str(HOME)
        if p == h:
            return "~"
        if p.startswith(h + os.sep):
            p = "~" + p[len(h):]
        parts = p.split(os.sep)
        if len(parts) > 3:
            p = "…/" + "/".join(parts[-2:])
        return p

    def refresh_header(self):
        w = max(20, self.size.width - 2)
        right = f"{fmt_tok(self.sess['tokens'])} tok"
        room = w - 6 - len(right) - 4
        title = self.sess["title"]
        if len(title) > room:
            title = title[: max(0, room - 1)] + "…"
        gap = max(1, w - 6 - 2 - len(title) - len(right))
        line = Text()
        line.append("vezcli", style="bold #ffffff")
        line.append("  " + title, style="#777777")
        line.append(" " * gap)
        line.append(right, style="#555555")
        self.header_w.update(line)

    def spin(self):
        if self.is_busy:
            self.frame = (self.frame + 1) % len(SPIN)
            el = time.perf_counter() - self.busy_since
            self.box_w.border_title = f"{SPIN[self.frame]} {self.busy_label} {fmt_time(el)} · esc to stop"
        else:
            t = self.short_cwd()
            if self.box_w.border_title != t:
                self.box_w.border_title = t

    def stick(self):
        c = self.chat_w
        return c.max_scroll_y - c.scroll_y <= 3

    def scroll_chat(self):
        self.chat_w.scroll_end(animate=False)

    def add(self, *widgets, force=False):
        stick = force or self.stick()
        self.chat_w.mount(*widgets)
        if stick:
            self.call_after_refresh(self.scroll_chat)

    def add_user(self, text):
        self.add(Static(Text(text), classes="user"), force=True)

    def add_ai(self, text, meta):
        self.add(Static(Markdown(text), classes="ai"), Static(meta_text(meta), classes="meta"))

    def add_tool(self, name, summary):
        card = ToolCard(name, summary)
        self.add(card)
        return card

    def add_note(self, text, err=False):
        self.add(Static(Text(text), classes="err" if err else "note"))

    def welcome(self):
        t = Text()
        t.append("vezcli", style="bold #ffffff")
        t.append("  terminal agent\n", style="#777777")
        t.append(f"{self.cur_model} · {self.short_cwd()}\n", style="#555555")
        t.append("/ commands   ! shell   esc stop   ↑ history", style="#555555")
        self.add(Static(t, classes="note"))

    def show_menu(self, prefix):
        items = [c for c in COMMANDS if c[0].startswith(prefix) and c[0] not in WEB_ONLY]
        if not items:
            self.menu_w.display = False
            return
        self.menu_w.clear_options()
        self.menu_w.add_options(
            [Option(Text.assemble((f"/{n}", "bold #ffffff"), "  ", (d, "#777777")), id=n) for n, d, _ in items]
        )
        self.menu_w.highlighted = 0
        self.menu_w.display = True

    def hide_menu(self):
        self.menu_w.display = False

    def highlighted_cmd(self):
        m = self.menu_w
        if m.display and m.highlighted is not None:
            return m.get_option_at_index(m.highlighted).id
        return None

    @on(Input.Changed, "#prompt")
    def changed(self, event):
        v = event.value
        if v.startswith("/") and " " not in v:
            self.show_menu(v[1:].lower())
        else:
            self.hide_menu()

    @on(OptionList.OptionSelected, "#menu")
    async def menu_picked(self, event):
        await self.pick_command(event.option.id)

    async def pick_command(self, name):
        takes = {n: a for n, _, a in COMMANDS}[name]
        self.hide_menu()
        if takes:
            self.prompt_w.value = f"/{name} "
            self.prompt_w.cursor_position = len(self.prompt_w.value)
            self.prompt_w.focus()
        else:
            self.prompt_w.value = ""
            self.prompt_w.focus()
            await self.run_command(f"/{name}")

    @on(Input.Submitted, "#prompt")
    async def submitted(self, event):
        text = event.value.strip()
        if not text:
            return
        hl = self.highlighted_cmd()
        if text.startswith("/"):
            event.input.value = ""
            self.hide_menu()
            self.cmd_hist.append(text)
            self.hist_pos = None
            await self.run_command(text, hl)
            return
        if text.startswith("!"):
            event.input.value = ""
            self.cmd_hist.append(text)
            self.hist_pos = None
            await self.cmd_shell(text[1:].strip())
            return
        if self.is_busy:
            self.add_note("busy, press esc to stop the current task", True)
            return
        event.input.value = ""
        self.cmd_hist.append(text)
        self.hist_pos = None
        self.start_turn(text)

    def action_menu_up(self):
        if self.menu_w.display:
            self.menu_w.action_cursor_up()
        else:
            self.history_move(-1)

    def action_menu_down(self):
        if self.menu_w.display:
            self.menu_w.action_cursor_down()
        else:
            self.history_move(1)

    def history_move(self, d):
        if not self.cmd_hist:
            return
        if self.hist_pos is None:
            if d > 0:
                return
            self.hist_pos = len(self.cmd_hist)
        self.hist_pos = max(0, min(len(self.cmd_hist), self.hist_pos + d))
        v = "" if self.hist_pos == len(self.cmd_hist) else self.cmd_hist[self.hist_pos]
        self.prompt_w.value = v
        self.prompt_w.cursor_position = len(v)

    def action_complete(self):
        if isinstance(self.screen, ModalScreen):
            return
        name = self.highlighted_cmd()
        if name:
            self.prompt_w.value = f"/{name} "
            self.prompt_w.cursor_position = len(self.prompt_w.value)

    def action_stop(self):
        if self.menu_w.display:
            self.hide_menu()
        elif self.is_busy:
            self.cancel_evt.set()
            self.busy_label = "stopping"

    def action_chat_up(self):
        self.chat_w.scroll_page_up(animate=False)

    def action_chat_down(self):
        self.chat_w.scroll_page_down(animate=False)

    async def action_interrupt(self):
        if self.is_busy:
            self.cancel_evt.set()
            self.busy_label = "stopping"
        else:
            await self.action_quit()

    async def action_quit(self):
        self.cancel_evt.set()
        try:
            self.save_session()
        except Exception:
            pass
        self.exit()

    async def run_command(self, text, hl=None):
        parts = text[1:].split(None, 1)
        raw = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        name = ALIASES.get(raw, raw)
        names = [c[0] for c in COMMANDS if c[0] not in WEB_ONLY]
        if name not in names:
            cands = [n for n in names if n.startswith(raw)]
            if hl in cands:
                name = hl
            elif cands:
                name = cands[0]
            else:
                self.add_note(f"/{raw} is web only" if raw in WEB_ONLY else f"unknown command /{raw}", True)
                return
        await getattr(self, f"cmd_{name}")(arg)

    async def cmd_models(self, arg):
        if arg:
            prov, mid = resolve_model(self.cfg, arg)
            self.set_model(prov, mid)
            return
        entries = [(p, m) for p, d in self.cfg["providers"].items() for m in d.get("models", [])]
        if not entries:
            self.add_note("no models known, use /models <id> or /connect", True)
            return
        self.push_screen(ModelPicker(entries, (self.cfg["provider"], self.cur_model)), self.after_model)

    def after_model(self, res):
        if res:
            self.set_model(*res)

    def set_model(self, prov, mid):
        try:
            select_model(self.cfg, prov, mid)
        except Exception as e:
            self.add_note(str(e), True)
            return
        self.cur_model = mid
        self.box_w.border_subtitle = model_label(self.cfg, mid)
        self.add_note(f"model → {model_label(self.cfg, mid)}")

    async def cmd_new(self, arg):
        if self.is_busy:
            self.add_note("busy, press esc first", True)
            return
        self.save_session()
        self.sess = new_sess(self.cur_model)
        self.convo = []
        self.events = []
        await self.chat_w.remove_children()
        self.refresh_header()
        self.welcome()

    async def cmd_sessions(self, arg):
        if self.is_busy:
            self.add_note("busy, press esc first", True)
            return
        self.push_screen(SessionPicker(list_sessions()), self.after_session)

    def after_session(self, sid):
        if sid:
            self.call_later(self.load_session, sid)

    async def cmd_rename(self, arg):
        if not arg:
            self.add_note("usage: /rename <name>")
            return
        self.sess["title"] = arg[:60]
        self.save_session()
        self.refresh_header()

    async def cmd_cd(self, arg):
        if not arg:
            self.add_note(os.getcwd())
            return
        try:
            os.chdir(Path(arg).expanduser())
        except Exception as e:
            self.add_note(str(e), True)
            return
        self.box_w.border_title = self.short_cwd()
        self.add_note(f"→ {os.getcwd()}")

    async def cmd_shell(self, arg):
        if not arg:
            self.add_note("usage: /shell <command>  or  !<command>")
            return
        card = ToolCard("shell", describe("shell", {"command": arg}))
        self.add(card, force=True)
        self.run_aux(card, "shell", arg)

    async def cmd_image(self, arg):
        if not arg:
            self.add_note("usage: /image <prompt>")
            return
        card = ToolCard("image", describe("image", {"prompt": arg}))
        self.add(card, force=True)
        self.run_aux(card, "image", arg)

    async def cmd_copy(self, arg):
        text = next((e["text"] for e in reversed(self.events) if e.get("t") == "ai"), "")
        if not text:
            self.add_note("nothing to copy")
            return
        ok = False
        try:
            subprocess.run(["termux-clipboard-set"], input=text, text=True, timeout=5, check=True)
            ok = True
        except Exception:
            try:
                self.copy_to_clipboard(text)
                ok = True
            except Exception:
                pass
        self.add_note("copied last reply" if ok else "copy failed, install termux-api", not ok)

    async def cmd_help(self, arg):
        t = Text()
        for n, d, _ in [c for c in COMMANDS if c[0] not in WEB_ONLY]:
            t.append(f"/{n}".ljust(11), style="bold #ffffff")
            t.append(d + "\n", style="#777777")
        t.append("!cmd".ljust(11), style="bold #ffffff")
        t.append("run shell directly\n", style="#777777")
        t.append("esc".ljust(11), style="bold #ffffff")
        t.append("stop task / close menu\n", style="#777777")
        t.append("↑ ↓".ljust(11), style="bold #ffffff")
        t.append("history / menu", style="#777777")
        self.add(Static(t, classes="note"))

    async def cmd_connect(self, arg):
        parsed = parse_connect(arg)
        if not parsed:
            self.add_note("usage: /connect <name> <base_url> [key]  or  /connect <preset> <key>\npresets: " + " ".join(p[0].lower() for p in PRESETS), True)
            return
        self.add_note(f"connecting {parsed[0]}…")
        self.run_connect(*parsed)

    @work(thread=True, group="aux")
    def run_connect(self, name, url, key):
        try:
            n, c, err = add_provider(self.cfg, self.client, name, url, key)
            self.call_from_thread(self.add_note, f"connected {n} · {c} models" + (f" ({err})" if err else ""), bool(err) and not c)
        except Exception as e:
            self.call_from_thread(self.add_note, str(e), True)

    async def cmd_export(self, arg):
        self.add_note(export_file(self.sess, self.events))

    async def cmd_exit(self, arg):
        await self.action_quit()

    def save_session(self):
        if not self.convo:
            return
        self.sess["messages"] = self.convo
        self.sess["events"] = self.events
        self.sess["updated"] = datetime.now().isoformat(timespec="seconds")
        self.sess["model"] = self.cur_model
        self.sess["provider"] = self.cfg["provider"]
        self.sess["cwd"] = os.getcwd()
        path = SESSIONS / f"{self.sess['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sess))
        tmp.replace(path)

    async def resume_last(self):
        items = list_sessions()
        if items:
            await self.load_session(items[0]["id"])
        else:
            self.welcome()

    async def load_session(self, sid):
        if self.is_busy:
            return
        try:
            data = json.loads((SESSIONS / f"{sid}.json").read_text())
        except Exception as e:
            self.add_note(f"failed to load: {e}", True)
            return
        self.save_session()
        data.setdefault("tokens", 0)
        data.setdefault("title", "untitled")
        self.sess = data
        self.convo = data.get("messages", [])
        self.events = data.get("events", [])
        try:
            os.chdir(data.get("cwd") or os.getcwd())
        except Exception:
            pass
        if restore_model(self.cfg, data):
            self.cur_model = self.cfg["model"]
            self.box_w.border_subtitle = model_label(self.cfg, self.cur_model)
        await self.chat_w.remove_children()
        self.replay()
        self.refresh_header()
        self.box_w.border_title = self.short_cwd()

    def replay(self):
        widgets = []
        if len(self.events) > 120:
            widgets.append(Static(Text(f"… {len(self.events) - 120} earlier events hidden"), classes="note"))
        for e in self.events[-120:]:
            try:
                k = e.get("t")
                if k == "user":
                    widgets.append(Static(Text(e["text"]), classes="user"))
                elif k == "ai":
                    widgets.append(Static(Markdown(e["text"]), classes="ai"))
                    widgets.append(Static(meta_text(e["meta"]), classes="meta"))
                elif k == "tool":
                    widgets.append(ToolCard(e["name"], e["summary"], "ok" if e["ok"] else "err", e["result"], e["dur"]))
            except Exception:
                continue
        if widgets:
            self.chat_w.mount(*widgets)
            self.call_after_refresh(self.scroll_chat)

    def start_turn(self, text):
        self.is_busy = True
        self.cancel_evt.clear()
        self.busy_label = "thinking"
        self.busy_since = time.perf_counter()
        self.add_user(text)
        if self.sess["title"] == "new session":
            self.sess["title"] = (text.splitlines() or [text])[0][:50]
            self.refresh_header()
        self.convo.append({"role": "user", "content": text})
        self.events.append({"t": "user", "text": text})
        self.run_turn()

    def end_turn(self, note):
        if note:
            self.add_note(note, note != "stopped")
        self.refresh_header()
        self.prompt_w.focus()

    def post_chat(self, msgs, tools=None):
        payload = {
            "model": self.cur_model,
            "messages": msgs,
            "tools": tools if tools is not None else getattr(self, "tools", TOOLS),
            "tool_choice": "auto",
            "max_tokens": int(self.cfg["max_tokens"]),
        }
        if not payload["tools"]:
            payload.pop("tools")
            payload.pop("tool_choice")
        headers = {"Content-Type": "application/json"}
        if self.cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self.cfg['api_key']}"
        last = None
        for attempt in range(3):
            try:
                r = self.client.post(f"{self.cfg['base_url']}/chat/completions", headers=headers, json=payload)
            except httpx.HTTPError as e:
                last = str(e) or e.__class__.__name__
                time.sleep(1 + attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{r.status_code} {r.text[:200]}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise ApiError(f"{r.status_code} {r.text[:300]}", r.status_code in (402, 403, 408, 429))
            return r.json()
        raise ApiError(last or "request failed", True)

    def api_chat(self, msgs, tools=None):
        box = {}

        def go():
            try:
                box["r"] = self.post_chat(msgs, tools)
            except Exception as e:
                box["e"] = e

        th = threading.Thread(target=go, daemon=True)
        th.start()
        while th.is_alive():
            th.join(0.2)
            if self.cancel_evt.is_set():
                raise Cancelled()
        if "e" in box:
            raise box["e"]
        return box["r"]

    def dispatch(self, name, a):
        try:
            if name == "run_shell":
                if not a.get("command"):
                    return "missing command", False
                return run_shell(str(a["command"]), a.get("cwd"), int(self.cfg["shell_timeout"]), self.cancel_evt)
            if name == "read_file":
                return read_file(str(a.get("path", "")))
            if name == "write_file":
                return write_file(str(a.get("path", "")), str(a.get("content", "")))
            if name == "edit_file":
                return edit_file(str(a.get("path", "")), str(a.get("old_text", "")), str(a.get("new_text", "")))
            if name == "list_dir":
                return list_dir(str(a.get("path") or "."))
            if name == "search_web":
                return search_web(self.client, str(a.get("query", "")))
            if name == "generate_image":
                return generate_image(self.client, self.cfg, str(a.get("prompt", "")), a.get("path"))
            if name == "fetch_url":
                return fetch_url(self.client, str(a.get("url", "")))
            if name == "use_skill":
                return use_skill(str(a.get("name", "")))
            if name == "grep_files":
                return grep_files(str(a.get("pattern", "")), str(a.get("path") or "."))
        except Exception as e:
            return f"{name} failed: {e}", False
        return f"unknown tool {name}", False

    @work(thread=True, group="aux")
    def run_aux(self, card, kind, arg):
        s = time.perf_counter()
        try:
            if kind == "shell":
                out, ok = run_shell(arg, None, int(self.cfg["shell_timeout"]))
            else:
                out, ok = generate_image(self.client, self.cfg, arg, None)
        except Exception as e:
            out, ok = str(e), False
        try:
            self.call_from_thread(card.finish, clean(out), ok, time.perf_counter() - s)
        except Exception:
            pass

    @work(thread=True, exclusive=True, group="turn")
    def run_turn(self):
        t0 = time.perf_counter()
        calls = 0
        seen = {}
        note = None
        try:
            for _ in range(int(self.cfg["max_steps"])):
                if self.cancel_evt.is_set():
                    note = "stopped"
                    break
                self.busy_label = "thinking"
                msgs = [{"role": "system", "content": build_system()}] + self.convo
                s = time.perf_counter()
                resp = self.api_chat(msgs)
                dt = time.perf_counter() - s
                calls += 1
                msg = (resp.get("choices") or [{}])[0].get("message") or {}
                text = (msg.get("content") or "").strip()
                tcs = msg.get("tool_calls") or []
                usage = resp.get("usage") or {}
                ct = usage.get("completion_tokens")
                pt = usage.get("prompt_tokens")
                est = ct is None or pt is None
                if ct is None:
                    arg_len = sum(len(str((tc.get("function") or {}).get("arguments", ""))) for tc in tcs)
                    ct = max(1, (len(text) + arg_len) // 4)
                if pt is None:
                    pt = max(1, len(json.dumps(msgs)) // 4)
                ct, pt = int(ct), int(pt)
                self.sess["tokens"] += ct + pt
                norm = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    args, err = parse_args(fn.get("arguments"))
                    norm.append(
                        {
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            "name": fn.get("name", ""),
                            "args": args,
                            "err": err,
                        }
                    )
                entry = {"role": "assistant", "content": text}
                if norm:
                    entry["tool_calls"] = [
                        {
                            "id": n["id"],
                            "type": "function",
                            "function": {"name": n["name"], "arguments": json.dumps(n["args"])},
                        }
                        for n in norm
                    ]
                self.convo.append(entry)
                meta = {
                    "model": self.cur_model,
                    "time": dt,
                    "ct": ct,
                    "pt": pt,
                    "tps": ct / dt if dt > 0 else 0,
                    "est": est,
                    "turn": None,
                    "calls": None,
                }
                if not norm and calls > 1:
                    meta["turn"] = time.perf_counter() - t0
                    meta["calls"] = calls
                if text:
                    self.events.append({"t": "ai", "text": text, "meta": meta})
                    self.call_from_thread(self.add_ai, text, meta)
                elif not norm:
                    self.call_from_thread(self.add_note, "empty reply from model", True)
                self.call_from_thread(self.refresh_header)
                if not norm:
                    break
                for i, n in enumerate(norm):
                    if self.cancel_evt.is_set():
                        for rest in norm[i:]:
                            self.convo.append({"role": "tool", "tool_call_id": rest["id"], "content": "cancelled by user"})
                        note = "stopped"
                        break
                    summary = describe(n["name"], n["args"])
                    card = self.call_from_thread(self.add_tool, n["name"], summary)
                    self.busy_label = f"running {n['name']}"
                    key = n["name"] + json.dumps(n["args"], sort_keys=True)
                    seen[key] = seen.get(key, 0) + 1
                    ts = time.perf_counter()
                    if n["err"]:
                        result, ok = n["err"], False
                    elif seen[key] > 2:
                        result, ok = "duplicate call blocked, this exact call already ran. move on or finish.", False
                    else:
                        result, ok = self.dispatch(n["name"], n["args"])
                    dur = time.perf_counter() - ts
                    result = clean(result)
                    self.call_from_thread(card.finish, result, ok, dur)
                    self.events.append(
                        {"t": "tool", "name": n["name"], "summary": summary, "result": result[:1500], "ok": ok, "dur": dur}
                    )
                    self.convo.append({"role": "tool", "tool_call_id": n["id"], "content": clip(result, 12000)})
                if note:
                    break
            else:
                note = "step limit reached"
        except Cancelled:
            note = "stopped"
        except Exception as e:
            try:
                self.call_from_thread(self.add_note, str(e), True)
            except Exception:
                pass
        finally:
            self.is_busy = False
            try:
                self.save_session()
                self.call_from_thread(self.end_turn, note)
            except Exception:
                pass


WEB_HTML = r"""<!doctype html>
<html lang="en" data-theme="orange" data-font="clean">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content">
<meta name="theme-color" content="#000000">
<title>vezcli</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&family=Pixelify+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--ac:#ff7a1a;--ac-rgb:255,122,26;--bg:#000;--s1:#0a0a0a;--s2:#111;--s3:#171717;--line:#1d1d1d;--line2:#2b2b2b;--tx:#ededed;--mu:#808080;--di:#515151;--ok:#5fd08a;--er:#ff5d5d;--r:6px;--mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;--sans:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;--px:'Pixelify Sans','JetBrains Mono',monospace;--body:var(--sans)}
html[data-theme=green]{--ac:#3dff8a;--ac-rgb:61,255,138}
html[data-theme=blue]{--ac:#4da3ff;--ac-rgb:77,163,255}
html[data-theme=pink]{--ac:#ff5fa8;--ac-rgb:255,95,168}
html[data-theme=violet]{--ac:#a384ff;--ac-rgb:163,132,255}
html[data-theme=white]{--ac:#f2f2f2;--ac-rgb:242,242,242}
html[data-font=pixel]{--body:var(--px)}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:15px;line-height:1.65;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;background-image:radial-gradient(ellipse 70% 45% at 50% -8%,rgba(var(--ac-rgb),.2),transparent 70%),linear-gradient(rgba(255,255,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.05) 1px,transparent 1px);background-size:100% 100%,24px 24px,24px 24px;-webkit-mask-image:radial-gradient(ellipse 120% 90% at 50% 20%,#000 40%,transparent 100%);mask-image:radial-gradient(ellipse 120% 90% at 50% 20%,#000 40%,transparent 100%)}
#bg{position:fixed;inset:0;z-index:0;pointer-events:none}
button{font:inherit;color:inherit}
.pxl{font-family:var(--px);letter-spacing:.06em;text-transform:uppercase}
#app{height:100dvh;display:flex;flex-direction:column;max-width:900px;margin:0 auto;position:relative;z-index:1}
header{display:flex;align-items:center;gap:12px;padding:calc(10px + env(safe-area-inset-top)) 14px 10px;border-bottom:1px solid var(--line);background:rgba(0,0,0,.7);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);z-index:5}
.icon{width:36px;height:36px;border-radius:var(--r);border:1px solid var(--line2);background:var(--s1);display:grid;place-items:center;cursor:pointer;transition:.15s;flex:none;box-shadow:2px 2px 0 rgba(var(--ac-rgb),.25)}
.icon:hover{border-color:var(--ac)}
.icon:active{transform:translate(2px,2px);box-shadow:0 0 0 rgba(var(--ac-rgb),.25)}
.brand{font:700 21px var(--px);letter-spacing:.02em;color:#fff}
.brand i{display:inline-block;width:9px;height:17px;background:var(--ac);margin-left:4px;vertical-align:-2px;animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:0}}
#sessTitle{flex:1;min-width:0;color:var(--mu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:13px var(--mono)}
#tokTotal{font:11.5px var(--mono);color:var(--ac);flex:none;border:1px solid rgba(var(--ac-rgb),.35);padding:2px 8px;border-radius:var(--r)}
main{flex:1;overflow-y:auto;padding:22px 16px 6px;overscroll-behavior:contain}
main::-webkit-scrollbar{width:6px}
main::-webkit-scrollbar-thumb{background:#262626}
#hero{text-align:center;padding:11vh 0 0}
.logo{width:64px;height:56px;margin:0 auto 26px;position:relative}
.logo i{position:absolute;left:0;top:0;width:8px;height:8px;background:var(--ac);box-shadow:8px 8px var(--ac),16px 16px var(--ac),24px 24px var(--ac),16px 32px var(--ac),8px 40px var(--ac),0 48px var(--ac),40px 48px var(--ac),48px 48px var(--ac),56px 48px var(--ac);filter:drop-shadow(0 0 10px rgba(var(--ac-rgb),.6))}
#hero .wm{font:700 46px var(--px);letter-spacing:.04em;color:#fff;text-shadow:3px 3px 0 rgba(var(--ac-rgb),.45)}
#hero p{color:var(--mu);margin-top:10px;font-size:14px}
.chips{display:flex;gap:8px;justify-content:center;margin-top:22px;flex-wrap:wrap}
.chips span{font:12px var(--px);color:var(--mu);border:1px solid var(--line2);padding:4px 12px;background:rgba(10,10,10,.8);letter-spacing:.04em}
.chips b{color:var(--ac);font-weight:600}
.msg{margin:0 0 20px}
.fresh{animation:in .26s steps(6) both}
@keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.user .bub{background:rgba(17,17,17,.9);border:1px solid var(--line2);border-left:3px solid var(--ac);padding:9px 14px;border-radius:var(--r);white-space:pre-wrap;word-break:break-word;font-family:var(--body)}
.user .bub::before{content:'> ';color:var(--ac);font:700 15px var(--px)}
.body{word-break:break-word;color:#dedede;font-family:var(--body)}
html[data-font=pixel] .body,html[data-font=pixel] .user .bub{font-size:16px;line-height:1.55}
.body p{margin:0 0 10px}
.body p:last-child{margin-bottom:0}
.body h2,.body h3,.body h4,.body h5{font-family:var(--px);font-weight:600;letter-spacing:.02em;margin:18px 0 6px;color:#fff}
.body h2{font-size:22px;color:var(--ac)}.body h3{font-size:19px}.body h4,.body h5{font-size:16px}
.body ul,.body ol{margin:0 0 10px 22px}
.body li{margin:2px 0}
.body li::marker{color:var(--ac)}
.body a{color:var(--ac);text-decoration:underline;text-underline-offset:3px}
.body code{background:var(--s3);border:1px solid var(--line2);padding:1px 6px;border-radius:4px;font:.86em var(--mono);color:#ffd9bd}
.code{border:1px solid var(--line2);border-radius:var(--r);background:rgba(5,5,5,.92);margin:10px 0;overflow:hidden}
.ch{display:flex;justify-content:space-between;align-items:center;padding:6px 12px;border-bottom:1px solid var(--line);font:12px var(--px);color:var(--ac);letter-spacing:.06em;text-transform:uppercase}
.cp,.cpm{background:none;border:1px solid var(--line2);border-radius:4px;padding:1px 9px;cursor:pointer;font:11px var(--mono);color:var(--mu);transition:.15s}
.cp:hover,.cpm:hover{color:var(--ac);border-color:var(--ac)}
.code pre{padding:12px 14px;overflow-x:auto;font:12.5px/1.6 var(--mono);color:#cfcfcf}
.code code{background:none;border:0;padding:0;font:inherit;color:inherit}
.meta{display:flex;align-items:center;gap:10px;color:var(--di);font:11px var(--mono);margin-top:8px}
.meta span{flex:1;min-width:0}
.body blockquote{border-left:3px solid var(--ac);padding:2px 0 2px 14px;margin:10px 0;color:var(--mu)}
.body hr{border:0;border-top:1px dashed var(--line2);margin:16px 0}
.body del{color:var(--di)}
.cb{display:inline-block;width:13px;height:13px;border:1.5px solid #555;margin-right:8px;vertical-align:-2px}
.cb.on{background:var(--ac);border-color:var(--ac)}
.tbl{margin:12px 0;border:1px solid var(--line2);border-radius:var(--r);overflow:auto;background:rgba(10,10,10,.92)}
.tbl table{border-collapse:collapse;width:100%;font-size:13.5px}
.tbl th{font:500 12px var(--px);letter-spacing:.08em;text-transform:uppercase;color:var(--ac);padding:10px 14px;background:var(--s2);border-bottom:1px solid var(--line2);white-space:nowrap}
.tbl td{padding:10px 14px;border-bottom:1px solid var(--line);white-space:nowrap;color:#cfcfcf}
.tbl tr:last-child td{border-bottom:0}
.tbl tbody tr:hover td{background:var(--s2)}
.tbl td:first-child{color:#fff;font-weight:500}
details.tool{border:1px solid var(--line2);background:rgba(10,10,10,.92);border-radius:var(--r);margin:0 0 12px;overflow:hidden}
details.tool[open]{border-color:#3a3a3a}
details.tool summary{display:flex;align-items:center;gap:10px;padding:9px 13px;cursor:pointer;list-style:none}
details.tool summary::-webkit-details-marker{display:none}
.dot{width:8px;height:8px;background:var(--ok);flex:none}
.tool.err .dot{background:var(--er)}
.tool.run .dot{background:var(--ac);animation:blink .8s steps(2) infinite}
.tn{font:600 13px var(--px);letter-spacing:.04em;color:var(--ac)}
.ts{flex:1;min-width:0;color:var(--mu);font:12px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.td{color:var(--di);font:11px var(--mono);flex:none}
details.tool pre{padding:11px 14px;border-top:1px solid var(--line);font:12px/1.6 var(--mono);color:#a3a3a3;max-height:280px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.note{color:var(--mu);font:12.5px var(--mono);text-align:center;margin:0 0 16px}
.note.err{color:var(--er)}
.helpc{border:1px solid var(--line2);background:rgba(10,10,10,.92);border-radius:var(--r);padding:14px 16px}
.helpc .pxl{color:var(--ac);font-size:13px;margin-bottom:6px}
.helpc .sp{margin-top:14px}
.hr{display:flex;gap:14px;padding:3px 0;font-size:13px}
.hr b{font:500 12.5px var(--mono);min-width:92px;color:#fff}
.hr span{color:var(--mu)}
#think{display:none;align-items:center;gap:5px;color:var(--mu);font:12px var(--mono);margin:0 0 18px}
#think i{width:6px;height:6px;background:var(--ac);animation:blink .9s steps(2) infinite}
#think i:nth-child(2){animation-delay:.15s}
#think i:nth-child(3){animation-delay:.3s}
#think span{margin-left:8px}
.dock{padding:8px 12px calc(12px + env(safe-area-inset-bottom));position:relative}
.composer{border:1px solid var(--line2);background:rgba(8,8,8,.94);border-radius:var(--r);padding:12px 13px 9px;transition:.2s;box-shadow:4px 4px 0 rgba(var(--ac-rgb),.18)}
.composer:focus-within{border-color:var(--ac);box-shadow:4px 4px 0 rgba(var(--ac-rgb),.35)}
.row{display:flex;gap:10px;align-items:flex-start}
.pm{font:700 19px var(--px);color:var(--ac);line-height:1.45;user-select:none}
textarea{flex:1;min-width:0;background:none;border:0;outline:0;color:var(--tx);font:15px/1.5 var(--mono);resize:none;max-height:160px;display:block}
textarea::placeholder{color:#555}
.bar{display:flex;align-items:center;gap:8px;margin-top:9px}
#cwd{flex:1;min-width:0;font:11.5px var(--mono);color:var(--di);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#modelBtn{font:12px var(--mono);color:var(--mu);background:none;border:1px solid var(--line2);border-radius:var(--r);padding:5px 10px;cursor:pointer;transition:.15s;max-width:52vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#modelBtn i{color:var(--ac);font-style:normal}
#modelBtn:hover{color:#fff;border-color:var(--ac)}
#send{width:34px;height:34px;border-radius:var(--r);border:0;background:var(--ac);color:#000;display:grid;place-items:center;cursor:pointer;transition:.12s;flex:none;box-shadow:3px 3px 0 rgba(var(--ac-rgb),.35)}
#send:active{transform:translate(2px,2px);box-shadow:1px 1px 0 rgba(var(--ac-rgb),.35)}
#send.stop{background:var(--s3);color:var(--ac);border:1px solid var(--ac)}
.menu{position:absolute;left:12px;right:12px;bottom:calc(100% - 2px);background:rgba(8,8,8,.98);border:1px solid var(--line2);border-radius:var(--r);overflow:hidden;box-shadow:0 -14px 50px rgba(0,0,0,.9);max-height:46vh;overflow-y:auto;z-index:8}
.hidden{display:none!important}
.mi{display:flex;gap:14px;align-items:baseline;padding:9px 15px;cursor:pointer}
.mi b{font:500 13px var(--mono);min-width:92px;color:var(--ac)}
.mi span{color:var(--mu);font-size:13px}
.mi.on{background:var(--s3)}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.7);opacity:0;pointer-events:none;transition:.25s;z-index:15}
#scrim.on{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;bottom:0;left:0;width:min(86vw,340px);background:#050505;border-right:1px solid var(--line2);transform:translateX(-102%);transition:transform .3s cubic-bezier(.2,.8,.2,1);z-index:20;display:flex;flex-direction:column;padding:calc(14px + env(safe-area-inset-top)) 12px 12px}
#drawer.open{transform:none}
#drawer h3{font:500 13px var(--px);color:var(--ac);letter-spacing:.1em;text-transform:uppercase;margin:16px 6px 8px}
#newBtn{display:flex;align-items:center;gap:10px;width:100%;padding:11px 14px;border-radius:var(--r);border:1px solid var(--line2);background:var(--s2);cursor:pointer;transition:.15s;font:600 14px var(--px);letter-spacing:.05em;text-transform:uppercase;box-shadow:3px 3px 0 rgba(var(--ac-rgb),.25)}
#newBtn:hover{border-color:var(--ac)}
#sessList{flex:1;overflow-y:auto}
.si{display:flex;align-items:center;gap:8px;padding:10px 12px;border-radius:var(--r);cursor:pointer;transition:.12s;border:1px solid transparent}
.si:hover{background:var(--s2)}
.si.cur{border-color:rgba(var(--ac-rgb),.5)}
.si .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
.si small{color:var(--di);font:10.5px var(--mono);flex:none}
.si .x{background:none;border:0;color:var(--di);cursor:pointer;padding:2px 4px}
.si .x:hover{color:var(--er)}
.sheet{position:fixed;left:50%;bottom:0;transform:translate(-50%,105%);width:min(100%,620px);max-height:86dvh;background:#070707;border:1px solid var(--line2);border-bottom:0;border-radius:12px 12px 0 0;z-index:30;display:flex;flex-direction:column;padding:16px 14px calc(14px + env(safe-area-inset-bottom));transition:transform .3s cubic-bezier(.2,.8,.2,1);box-shadow:0 -6px 0 rgba(var(--ac-rgb),.2)}
.sheet.open{transform:translate(-50%,0)}
.sh{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.sh .pxl{font-size:18px;font-weight:700;color:#fff}
.ghost{background:none;border:1px solid var(--line2);border-radius:var(--r);padding:5px 12px;cursor:pointer;font:12px var(--px);letter-spacing:.06em;text-transform:uppercase;color:var(--ac);transition:.15s}
.ghost:hover{border-color:var(--ac)}
.srch{width:100%;background:var(--s2);border:1px solid var(--line2);border-radius:var(--r);padding:10px 12px;color:var(--tx);font:14px var(--mono);outline:0;margin-bottom:10px;transition:.15s}
.srch:focus{border-color:var(--ac)}
#mlist{overflow-y:auto;flex:1;min-height:80px}
.pg{margin-bottom:14px}
.pgh{display:flex;align-items:center;gap:10px;padding:4px 4px 8px;font:600 14px var(--px);color:var(--ac);letter-spacing:.08em;text-transform:uppercase}
.pgh small{color:var(--di);font:11px var(--mono);text-transform:none;letter-spacing:0;flex:1}
.pga button{background:none;border:0;color:var(--mu);cursor:pointer;padding:2px 7px;font-size:14px}
.pga button:hover{color:var(--ac)}
.mi2{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:var(--r);cursor:pointer;border:1px solid transparent}
.mi2:hover{background:var(--s2)}
.mi2.on{border-color:rgba(var(--ac-rgb),.6);background:rgba(var(--ac-rgb),.07)}
.rd{width:8px;height:8px;background:var(--line2);flex:none}
.mi2.on .rd{background:var(--ac)}
.mi2 b{font-weight:500;font-size:14px;word-break:break-all}
.mi2 span{margin-left:auto;color:var(--di);font:11px var(--mono);flex:none}
.empty{color:var(--di);font:12px var(--mono);padding:6px 12px}
.sheet label{display:block;font:12px var(--px);color:var(--mu);margin:12px 0 5px;letter-spacing:.08em;text-transform:uppercase}
.presets{display:flex;flex-wrap:wrap;gap:6px}
.presets button{background:var(--s2);border:1px solid var(--line2);border-radius:var(--r);padding:5px 11px;cursor:pointer;font:12px var(--mono);color:var(--tx);transition:.15s}
.presets button:hover{border-color:var(--ac);color:var(--ac)}
.go{margin-top:16px;width:100%;padding:12px;border:0;border-radius:var(--r);background:var(--ac);color:#000;font:700 15px var(--px);letter-spacing:.08em;text-transform:uppercase;cursor:pointer;box-shadow:3px 3px 0 rgba(var(--ac-rgb),.35);transition:.12s}
.go:active{transform:translate(2px,2px);box-shadow:1px 1px 0 rgba(var(--ac-rgb),.35)}
.go:disabled{opacity:.5}
#cMsg{margin-top:10px;font:12px var(--mono);color:var(--mu);min-height:16px}
.poll{border:1px solid var(--line2);background:rgba(10,10,10,.94);border-radius:var(--r);padding:16px;transition:.2s;box-shadow:4px 4px 0 rgba(var(--ac-rgb),.18)}
.poll.sending{opacity:.45;pointer-events:none}
.ph{display:flex;align-items:center;gap:12px;font:12px var(--px);color:var(--ac);margin-bottom:14px;letter-spacing:.06em}
.trk{flex:1;height:4px;background:var(--line2);overflow:hidden}
.trk i{display:block;height:100%;background:var(--ac);transition:width .3s steps(8)}
.pt{font:600 18px var(--px);letter-spacing:.02em;color:#fff;margin-bottom:12px}
.po{display:flex;flex-direction:column;gap:8px}
.op{display:flex;align-items:flex-start;gap:12px;width:100%;text-align:left;padding:11px 14px;border:1px solid var(--line2);background:var(--s2);border-radius:var(--r);cursor:pointer;transition:.12s}
.op:hover{border-color:#666}
.op.on{border-color:var(--ac);background:rgba(var(--ac-rgb),.08)}
.ck{width:16px;height:16px;border:1.5px solid #555;flex:none;margin-top:3px;position:relative}
.op.on .ck{border-color:var(--ac)}
.op.on .ck::after{content:'';position:absolute;inset:3px;background:var(--ac)}
.ol b{display:block;font-weight:500;color:var(--tx)}
.ol small{display:block;color:var(--mu);font-size:12.5px;margin-top:1px}
.oi{width:100%;margin-top:8px;background:var(--s2);border:1px dashed var(--line2);border-radius:var(--r);padding:10px 14px;color:var(--tx);font:14px var(--mono);outline:0}
.oi:focus{border:1px solid var(--ac)}
.pf{display:flex;align-items:center;gap:8px;margin-top:14px}
.pf span{flex:1}
.pb{padding:7px 16px;border-radius:var(--r);border:1px solid var(--line2);background:none;cursor:pointer;font:600 13px var(--px);letter-spacing:.06em;text-transform:uppercase;color:var(--mu);transition:.12s}
.pb:hover{color:#fff;border-color:#666}
.pb.go2{background:var(--ac);color:#000;border-color:var(--ac)}
.poll.done{padding:12px 16px;box-shadow:none}
.poll.done .pt{font:12px var(--px);color:var(--ac);letter-spacing:.08em;text-transform:uppercase;font-weight:500;margin-bottom:6px}
.pr{display:flex;justify-content:space-between;gap:16px;padding:7px 0;border-top:1px solid var(--line);font-size:13.5px}
.pr span{color:var(--mu)}
.pr b{font-weight:500;text-align:right}
details.agent{border-color:rgba(var(--ac-rgb),.4)}
.ag{border-top:1px solid var(--line);padding:10px 14px}
.agt{color:var(--mu);font:12px var(--mono);margin-bottom:8px;white-space:pre-wrap;word-break:break-word}
.ags{display:flex;gap:10px;font:12px var(--mono);padding:2px 0;color:var(--mu)}
.ags b{color:var(--ac);font-weight:500;min-width:86px;flex:none}
.ags span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ags.bad b{color:var(--er)}
.agr{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line2);font-size:14px}
#goalChip{font:12px var(--px);letter-spacing:.06em;text-transform:uppercase;color:#000;background:var(--ac);padding:2px 9px;border-radius:var(--r);cursor:pointer;flex:none;animation:glow 1.6s ease-in-out infinite}
@keyframes glow{50%{opacity:.55}}
#atts{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.at{position:relative;width:48px;height:48px;border:1px solid var(--line2);border-radius:var(--r);overflow:hidden}
.at img{width:100%;height:100%;object-fit:cover;display:block}
.at button{position:absolute;top:0;right:0;background:rgba(0,0,0,.75);border:0;color:#fff;font-size:10px;padding:1px 4px;cursor:pointer}
#attach{background:none;border:1px solid var(--line2);border-radius:var(--r);width:30px;height:30px;display:grid;place-items:center;color:var(--mu);cursor:pointer;flex:none;transition:.15s}
#attach:hover{color:var(--ac);border-color:var(--ac)}
.chs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.fc{font:11.5px var(--mono);color:var(--ac);border:1px solid rgba(var(--ac-rgb),.4);padding:1px 8px;border-radius:var(--r)}
.th{width:48px;height:48px;object-fit:cover;border:1px solid var(--line2);border-radius:var(--r)}
.mi .fb{font:500 13px var(--mono);color:#fff;min-width:0;word-break:break-all}
.todo{border:1px solid var(--line2);background:rgba(10,10,10,.94);border-radius:var(--r);padding:12px 14px}
.th2{display:flex;align-items:center;gap:12px;margin-bottom:8px;font:12px var(--px);color:var(--ac);letter-spacing:.06em;text-transform:uppercase}
.ti{display:flex;gap:10px;padding:3px 0;font-size:14px}
.ti .cb2{width:13px;height:13px;border:1.5px solid #555;flex:none;margin-top:6px}
.ti.done .cb2{background:var(--ac);border-color:var(--ac)}
.ti.in_progress .cb2{border-color:var(--ac);animation:blink 1s steps(2) infinite}
.ti.done span{color:var(--di);text-decoration:line-through}
.plancard{border:1px solid rgba(var(--ac-rgb),.5);background:rgba(10,10,10,.94);border-radius:var(--r);padding:14px 16px;box-shadow:4px 4px 0 rgba(var(--ac-rgb),.18)}
.ph2{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font:13px var(--px);color:var(--ac);letter-spacing:.08em;text-transform:uppercase}
.ptag{font:11px var(--mono);color:var(--mu);border:1px solid var(--line2);padding:1px 8px;border-radius:var(--r);text-transform:none;letter-spacing:0}
.ptag.approved{color:#000;background:var(--ok);border-color:var(--ok)}
details.tool pre.diff{white-space:pre;word-break:normal}
.diff span{display:block}
.diff .a{color:#7ee0a1;background:rgba(95,208,138,.09)}
.diff .d{color:#ff8a8a;background:rgba(255,93,93,.09)}
.diff .h{color:var(--ac)}
.diff .m{color:var(--di)}
#planChip{font:12px var(--px);letter-spacing:.06em;text-transform:uppercase;color:var(--ac);border:1px solid var(--ac);padding:2px 9px;border-radius:var(--r);cursor:pointer;flex:none}
</style>
</head>
<body>
<canvas id="bg"></canvas>
<div id="app">
<header>
<button class="icon" id="menuBtn" aria-label="sessions"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="square"><path d="M4 7h16M4 12h16M4 17h10"/></svg></button>
<span class="brand">vezcli<i></i></span>
<span id="sessTitle"></span>
<span id="goalChip" class="hidden"></span>
<span id="planChip" class="hidden">plan</span>
<span id="tokTotal">0 tok</span>
</header>
<main id="chat">
<div id="hero"><div class="logo"><i></i></div><div class="wm">VEZCLI</div><p>Describe a task. It runs tools on your device.</p><div class="chips"><span><b>/</b> commands</span><span><b>!</b> shell</span><span><b>esc</b> stop</span><span><b>ctrl+k</b> models</span></div></div>
<div id="msgs"></div>
<div id="think"><i></i><i></i><i></i><span id="thinkTxt"></span></div>
</main>
<div class="dock">
<div class="menu hidden" id="slash"></div>
<div class="composer">
<div id="atts" class="hidden"></div>
<div class="row"><span class="pm">&gt;</span><textarea id="input" rows="1" placeholder="message, /command or !shell" autocomplete="off" autocapitalize="off" spellcheck="false"></textarea></div>
<div class="bar">
<span id="cwd"></span>
<button id="attach" aria-label="attach image"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="square"><path d="M21 12.5l-8.5 8.5a5.5 5.5 0 01-7.8-7.8l9-9a3.7 3.7 0 015.2 5.2l-9 9a1.8 1.8 0 01-2.6-2.6l8-8"/></svg></button><input type="file" id="file" accept="image/*" multiple hidden>
<button id="modelBtn"></button>
<button id="send" aria-label="send"></button>
</div>
</div>
</div>
</div>
<div id="scrim"></div>
<aside id="drawer">
<button id="newBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="square"><path d="M12 5v14M5 12h14"/></svg>New session</button>
<h3>Sessions</h3>
<div id="sessList"></div>
</aside>
<section class="sheet" id="modelSheet">
<div class="sh"><span class="pxl">Models</span><button class="ghost" id="addProv">+ connect</button></div>
<input class="srch" id="msearch" placeholder="search models…" autocomplete="off" spellcheck="false">
<div id="mlist"></div>
<div class="empty" id="mmsg"></div>
</section>
<section class="sheet" id="connSheet">
<div class="sh"><span class="pxl">Connect</span><button class="ghost" id="connBack">back</button></div>
<div class="presets" id="presets"></div>
<label>name</label><input class="srch" id="cName" placeholder="openrouter" autocomplete="off" spellcheck="false" autocapitalize="off">
<label>base url</label><input class="srch" id="cUrl" placeholder="https://api.example.com/v1" autocomplete="off" spellcheck="false" autocapitalize="off">
<label>api key</label><input class="srch" id="cKey" type="password" placeholder="sk-…" autocomplete="off" spellcheck="false">
<button class="go" id="cGo">Connect &amp; fetch models</button>
<div id="cMsg"></div>
</section>
<script>
const BOOT=__BOOT__;
const TOKEN="__TOKEN__";
const $=s=>document.querySelector(s);
const root=document.documentElement;
const chat=$('#chat'),msgs=$('#msgs'),input=$('#input'),sendBtn=$('#send'),think=$('#think'),slash=$('#slash'),scrim=$('#scrim');
const UP='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="square"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
const STOP='<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16"/></svg>';
const coarse=matchMedia('(pointer:coarse)').matches;
const OVS=['#drawer','#modelSheet','#connSheet'];
let sid=null,rendered=0,busy=false,pollT=null,hl=0,slashItems=[],busyLabel='',busyAt=0,lastAI='',pin=true;
let menuMode='cmd',pending=[],atTok=null,atT=null,notifyOn=false,swReg=null;
let PV={provider:'',model:'',providers:[],presets:[]};
const running=new Set(),nodes=[],sigs=[];
const ls=(k,v)=>{try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}};
function applyLook(){root.dataset.theme=ls('vez.theme')||'orange';root.dataset.font=ls('vez.font')||'clean'}
applyLook();
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtTok=n=>n>=1000?(n/1000).toFixed(1)+'k':String(Math.round(n));
const fmtTime=s=>s<60?s.toFixed(1)+'s':Math.floor(s/60)+'m'+String(Math.floor(s%60)).padStart(2,'0')+'s';
const when=i=>{const d=new Date(i);return isNaN(d)?'':d.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})};
const el=h=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstElementChild};
async function api(path,body){
  const o={headers:{'X-Vez':TOKEN}};
  if(body!==undefined){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}
  const r=await fetch(path,o);
  if(!r.ok)throw new Error(r.status);
  return r.json();
}
async function copyText(t){
  try{await navigator.clipboard.writeText(t)}catch(e){const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove()}
}
function md(src){
  const blocks=[];
  src=src.replace(/```([\w+-]*)[^\S\n]*\n?([\s\S]*?)```/g,(m,l,c)=>{blocks.push({l:l,c:c.replace(/\n$/,'')});return '\u0000'+(blocks.length-1)+'\u0000'});
  const lines=esc(src).split('\n');
  const inline=s=>s.replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\*)/g,'$1<em>$2</em>').replace(/~~([^~]+)~~/g,'<del>$1</del>').replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  const cells=l=>{l=l.trim();if(l[0]==='|')l=l.slice(1);if(l.endsWith('|'))l=l.slice(0,-1);return l.split('|').map(c=>c.trim())};
  const isSep=l=>l.includes('-')&&/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  let out='',para=[],qt=[],list=null;
  const fp=()=>{if(para.length){out+='<p>'+inline(para.join('<br>'))+'</p>';para=[]}};
  const fq=()=>{if(qt.length){out+='<blockquote>'+inline(qt.join('<br>'))+'</blockquote>';qt=[]}};
  const flush=()=>{fp();fq()};
  const close=()=>{if(list){out+='</'+list+'>';list=null}};
  for(let x=0;x<lines.length;x++){
    const ln=lines[x];let m;
    if(ln.includes('|')&&x+1<lines.length&&isSep(lines[x+1])){
      flush();close();
      const head=cells(ln),al=cells(lines[x+1]).map(c=>c[0]===':'&&c.endsWith(':')?'center':c.endsWith(':')?'right':'left');
      let t='<div class="tbl"><table><thead><tr>'+head.map((c,j)=>'<th style="text-align:'+al[j]+'">'+inline(c)+'</th>').join('')+'</tr></thead><tbody>';
      x+=2;
      while(x<lines.length&&lines[x].trim()&&lines[x].includes('|')){const r=cells(lines[x]);t+='<tr>'+head.map((_,j)=>'<td style="text-align:'+al[j]+'">'+inline(r[j]||'')+'</td>').join('')+'</tr>';x++}
      x--;out+=t+'</tbody></table></div>';continue;
    }
    if((m=ln.match(/^\u0000(\d+)\u0000$/))){flush();close();const b=blocks[+m[1]];out+='<div class="code"><div class="ch"><span>'+esc(b.l||'code')+'</span><button class="cp">copy</button></div><pre><code>'+esc(b.c)+'</code></pre></div>'}
    else if(/^\s*([-*_])\1{2,}\s*$/.test(ln)){flush();close();out+='<hr>'}
    else if((m=ln.match(/^(#{1,4})\s+(.*)$/))){flush();close();const h=m[1].length+1;out+='<h'+h+'>'+inline(m[2])+'</h'+h+'>'}
    else if((m=ln.match(/^&gt;\s?(.*)$/))){fp();close();qt.push(m[1])}
    else if((m=ln.match(/^\s*[-*]\s+(.*)$/))){flush();if(list!=='ul'){close();out+='<ul>';list='ul'}out+='<li>'+inline(m[1].replace(/^\[( |x)\]\s+/i,(a,c)=>'<span class="cb'+(c.trim()?' on':'')+'"></span>'))+'</li>'}
    else if((m=ln.match(/^\s*\d+\.\s+(.*)$/))){flush();if(list!=='ol'){close();out+='<ol>';list='ol'}out+='<li>'+inline(m[1])+'</li>'}
    else if(!ln.trim()){flush();close()}
    else{fq();close();para.push(ln)}
  }
  flush();close();
  return out;
}
function renderPoll(e,i){
  const n=document.createElement('div');n.className='msg';n.dataset.i=i;
  if(e.status!=='pending'){
    n.innerHTML='<div class="poll done"><div class="pt">'+(e.status==='skipped'?'Skipped':'Answered')+' · '+e.questions.length+(e.questions.length>1?' questions':' question')+'</div>'+e.questions.map((q,k)=>'<div class="pr"><span>'+esc(q.title)+'</span><b>'+esc(((e.answers||[])[k]||[]).join(', ')||'—')+'</b></div>').join('')+'</div>';
    return n;
  }
  const qs=e.questions,sel=qs.map(()=>[]),oth=qs.map(()=>'');
  let k=0,sent=false;
  const card=document.createElement('div');card.className='poll';n.appendChild(card);
  const send=async skip=>{
    if(sent)return;sent=true;card.classList.add('sending');
    const answers=skip?null:qs.map((q,x)=>sel[x].concat(oth[x].trim()?[oth[x].trim()]:[]));
    try{await api('/api/answer',{id:e.id,answers:answers})}catch(err){sent=false;card.classList.remove('sending');return}
    poll();
  };
  const draw=()=>{
    const q=qs[k];
    card.innerHTML='<div class="ph"><span>'+(k+1)+'/'+qs.length+'</span><div class="trk"><i style="width:'+((k+1)/qs.length*100)+'%"></i></div></div>'
      +'<div class="pt">'+esc(q.title)+'</div>'
      +'<div class="po">'+q.options.map((o,j)=>'<button class="op'+(sel[k].includes(o.label)?' on':'')+'" data-j="'+j+'"><span class="ck"></span><span class="ol"><b>'+esc(o.label)+'</b>'+(o.desc?'<small>'+esc(o.desc)+'</small>':'')+'</span></button>').join('')+'</div>'
      +(q.other?'<input class="oi" placeholder="Other…" value="'+esc(oth[k])+'">':'')
      +'<div class="pf"><button class="pb" data-a="skip">Skip</button><span></span>'+(k>0?'<button class="pb" data-a="back">Back</button>':'')+'<button class="pb go2" data-a="next">'+(k===qs.length-1?'Submit':'Next')+'</button></div>';
  };
  card.addEventListener('click',ev=>{
    const op=ev.target.closest('.op');
    if(op){
      const q=qs[k],o=q.options[+op.dataset.j].label;
      if(q.multi){const p=sel[k].indexOf(o);if(p>=0)sel[k].splice(p,1);else sel[k].push(o);draw()}
      else{sel[k]=[o];oth[k]='';draw();if(k<qs.length-1)setTimeout(()=>{k++;draw()},220)}
      return;
    }
    const b=ev.target.closest('[data-a]');
    if(!b)return;
    const a=b.dataset.a;
    if(a==='skip')send(true);
    else if(a==='back'){k--;draw()}
    else if(k===qs.length-1)send(false);
    else{k++;draw()}
  });
  card.addEventListener('input',ev=>{
    if(!ev.target.classList.contains('oi'))return;
    oth[k]=ev.target.value;
    if(!qs[k].multi&&oth[k]){sel[k]=[];card.querySelectorAll('.op').forEach(x=>x.classList.remove('on'))}
  });
  draw();
  return n;
}
function renderEv(e,i){
  let n=null;
  if(e.t==='user'){
    const ch=(e.files||[]).map(f=>'<span class="fc">@'+esc(f)+'</span>').join('')+(e.thumbs||[]).map(t=>'<img class="th" src="'+esc(t)+'">').join('');
    n=el('<div class="msg user"><div class="bub">'+esc(e.text)+'</div>'+(ch?'<div class="chs">'+ch+'</div>':'')+'</div>');
  }
  else if(e.t==='ai'){
    const m=e.meta||{},mk=m.est?'~':'';
    const bits=[m.model||'',fmtTime(m.time||0),mk+fmtTok(m.ct||0)+' tok',Math.round(m.tps||0)+' tok/s',mk+fmtTok(m.pt||0)+' in'];
    if(m.turn)bits.push('turn '+fmtTime(m.turn)+' / '+m.calls+' calls');
    n=el('<div class="msg"><div class="body">'+md(e.text)+'</div><div class="meta"><span>'+bits.map(esc).join(' · ')+'</span><button class="cpm">copy</button></div></div>');
    n.querySelector('.cpm').onclick=ev=>{copyText(e.text);ev.target.textContent='copied';setTimeout(()=>ev.target.textContent='copy',1200)};
    lastAI=e.text;
  }else if(e.t==='tool'){
    const st=e.run?'run':(e.ok?'ok':'err');
    let dif=null;
    if(e.diff){
      let a=0,d=0;
      const h=e.diff.split('\n').map(l=>{let c='';if(l.startsWith('+++')||l.startsWith('---'))c='m';else if(l[0]==='+'){c='a';a++}else if(l[0]==='-'){c='d';d++}else if(l.startsWith('@@'))c='h';return '<span class="'+c+'">'+esc(l)+'</span>'}).join('');
      dif={h:h,a:a,d:d};
    }
    n=el('<details class="tool '+st+'"'+(st==='err'?' open':'')+'><summary><i class="dot"></i><span class="tn">'+esc(e.name)+'</span><span class="ts">'+esc(e.summary)+(dif?'  +'+dif.a+' −'+dif.d:'')+'</span><span class="td">'+(e.run?'':fmtTime(e.dur||0))+'</span></summary>'+(dif?'<pre class="diff">'+dif.h+'</pre>':(e.result?'<pre>'+esc(e.result)+'</pre>':''))+'</details>');
  }else if(e.t==='agent'){
    const st=e.run?'run':(e.status==='done'?'ok':'err');
    n=el('<details class="tool agent '+st+'"'+(e.run?' open':'')+'><summary><i class="dot"></i><span class="tn">agent</span><span class="ts">'+esc(e.name)+' · '+e.steps.length+' steps</span><span class="td">'+(e.run?'':fmtTime(e.dur||0))+'</span></summary><div class="ag"><div class="agt">'+esc(e.task)+'</div>'+e.steps.map(x=>'<div class="ags'+(x.ok?'':' bad')+'"><b>'+esc(x.name)+'</b><span>'+esc(x.summary)+'</span></div>').join('')+(e.result?'<div class="body agr">'+md(e.result)+'</div>':'')+'</div></details>');
  }else if(e.t==='todo'){
    const it=e.items||[],dn=it.filter(x=>x.status==='done').length;
    n=el('<div class="msg"><div class="todo"><div class="th2"><span>Todo</span><span>'+dn+'/'+it.length+'</span><div class="trk"><i style="width:'+(it.length?dn/it.length*100:0)+'%"></i></div></div>'+it.map(x=>'<div class="ti '+x.status+'"><i class="cb2"></i><span>'+esc(x.text)+'</span></div>').join('')+'</div></div>');
  }else if(e.t==='plan'){
    n=el('<div class="msg"><div class="plancard"><div class="ph2"><span>Plan</span><span class="ptag '+e.status+'">'+esc(e.status==='pending'?'awaiting approval':e.status)+'</span></div><div class="body">'+md(e.text)+'</div></div></div>');
  }else if(e.t==='poll')return renderPoll(e,i);
  else if(e.t==='note')n=el('<div class="note'+(e.err?' err':'')+'">'+esc(e.text)+'</div>');
  if(n)n.dataset.i=i;
  return n;
}
function since(){let m=rendered;running.forEach(i=>{if(i<m)m=i});return m}
function setSend(){sendBtn.classList.toggle('stop',busy);sendBtn.innerHTML=busy?STOP:UP}
function paintModel(){$('#modelBtn').innerHTML=(PV.providers.length>1?'<i>'+esc(PV.provider)+' ·</i> ':'')+esc(PV.model)}
function head(s){
  $('#sessTitle').textContent=s.title;
  $('#tokTotal').textContent=fmtTok(s.tokens)+' tok';
  $('#cwd').textContent=s.cwd+(s.perm==='ask'?'  · ask':'');
  if(s.model!==PV.model||s.provider!==PV.provider){PV.model=s.model;PV.provider=s.provider}
  paintModel();
  const gc=$('#goalChip');
  if(s.goal){gc.textContent='goal '+s.goal.round+'/'+s.goal.max;gc.classList.remove('hidden')}else gc.classList.add('hidden');
  $('#planChip').classList.toggle('hidden',!s.plan);
}
function apply(s){
  if(sid!==s.sid){
    sid=s.sid;msgs.innerHTML='';nodes.length=0;sigs.length=0;running.clear();rendered=0;
    if(s.base!==0){poll();return}
  }
  const stick=pin||chat.scrollHeight-chat.scrollTop-chat.clientHeight<140;
  const fresh=s.base>0||rendered>0;
  s.events.forEach((e,k)=>{
    const i=s.base+k,sg=JSON.stringify(e);
    if((e.t==='tool'||e.t==='poll'||e.t==='agent'||e.t==='todo'||e.t==='plan')&&e.run)running.add(i);else running.delete(i);
    if(sigs[i]===sg)return;
    if(e.t==='poll'&&e.status==='pending')notify('vezcli · needs you',e.questions[0].title);
    const n=renderEv(e,i);
    if(n&&!nodes[i]&&fresh)n.classList.add('fresh');
    if(nodes[i]){if(n)nodes[i].replaceWith(n);else nodes[i].remove()}
    else if(n)msgs.appendChild(n);
    nodes[i]=n;sigs[i]=sg;
  });
  rendered=Math.max(rendered,s.base+s.events.length);
  const was=busy;busy=s.busy;busyLabel=s.label;busyAt=performance.now()-s.elapsed*1000;
  if(was&&!busy)notify('vezcli · done',(lastAI||'task finished').replace(/\s+/g,' ').slice(0,140));
  head(s);setSend();
  $('#hero').classList.toggle('hidden',msgs.children.length>0);
  if(stick){chat.scrollTop=chat.scrollHeight;pin=false}
}
async function poll(){
  clearTimeout(pollT);
  let s;
  try{s=await api('/api/poll?since='+since())}catch(e){pollT=setTimeout(poll,1500);return}
  apply(s);
  if(busy||running.size)pollT=setTimeout(poll,350);
}
function local(t,err){
  const n=el('<div class="note'+(err?' err':'')+'"></div>');n.textContent=t;
  msgs.appendChild(n);$('#hero').classList.add('hidden');chat.scrollTop=chat.scrollHeight;
}
function helpCard(){
  const rows=BOOT.commands.map(c=>'<div class="hr"><b>/'+esc(c[0])+'</b><span>'+esc(c[1])+'</span></div>').join('');
  const keys=[['!cmd','run a shell command'],['esc','stop task / close'],['/','focus the input'],['ctrl+k','models'],['up down','menu']].map(k=>'<div class="hr"><b>'+k[0]+'</b><span>'+k[1]+'</span></div>').join('');
  msgs.appendChild(el('<div class="msg"><div class="helpc"><div class="pxl">Commands</div>'+rows+'<div class="pxl sp">Keys</div>'+keys+'</div></div>'));
  $('#hero').classList.add('hidden');chat.scrollTop=chat.scrollHeight;
}
async function skillsCard(){
  let list=[];
  try{list=await api('/api/skills')}catch(e){}
  const rows=list.map(x=>'<div class="hr"><b>'+esc(x.name)+'</b><span>'+esc(x.description)+'</span></div>').join('')||'<div class="hr"><span>no skills yet</span></div>';
  msgs.appendChild(el('<div class="msg"><div class="helpc"><div class="pxl">Skills</div>'+rows+'<div class="empty" style="padding:10px 0 0">'+esc(BOOT.skillDir)+'/&lt;name&gt;/SKILL.md · ask the agent to create one</div></div></div>'));
  $('#hero').classList.add('hidden');chat.scrollTop=chat.scrollHeight;
}
function notify(title,body,force){
  if(!notifyOn||(!force&&!document.hidden)||!('Notification' in window)||Notification.permission!=='granted')return;
  try{
    if(swReg&&swReg.showNotification)swReg.showNotification(title,{body:body,tag:'vezcli',renotify:true});
    else new Notification(title,{body:body});
  }catch(e){}
}
async function setNotify(a){
  const w=(a||'').toLowerCase();
  if(w==='test'){notify('vezcli','notifications work',true);return}
  const on=w?w==='on':!notifyOn;
  if(on){
    if(!('Notification' in window)){local('notifications are not supported in this browser',true);return}
    const p=await Notification.requestPermission();
    if(p!=='granted'){local('notification permission: '+p,true);return}
  }
  notifyOn=on;ls('vez.notify',on?'1':'0');
  local('notifications '+(on?'on, you get a ping when a task finishes or needs you while this tab is in the background':'off'));
  if(on)notify('vezcli','notifications on',true);
}
async function mcpCard(){
  let list=[];
  try{list=await api('/api/mcp')}catch(e){}
  const rows=list.map(x=>'<div class="hr"><b>'+esc(x.name)+'</b><span>'+(x.ready?x.tools.length+' tools':(x.err?'error: '+esc(x.err):'connecting…'))+'</span></div>'+(x.ready&&x.tools.length?'<div class="hr"><b></b><span style="font:11px var(--mono);color:var(--di)">'+esc(x.tools.slice(0,12).join(' · '))+(x.tools.length>12?' …':'')+'</span></div>':'')).join('')||'<div class="hr"><span>no MCP servers yet</span></div>';
  msgs.appendChild(el('<div class="msg"><div class="helpc"><div class="pxl">MCP</div>'+rows+'<div class="empty" style="padding:10px 0 0">/mcp add &lt;name&gt; &lt;command or url&gt; · /mcp remove &lt;name&gt; · /mcp reload</div></div></div>'));
  $('#hero').classList.add('hidden');chat.scrollTop=chat.scrollHeight;
}
function readImg(file){
  return new Promise(res=>{
    const fr=new FileReader();
    fr.onload=()=>{
      const im=new Image();
      im.onload=()=>{
        const mk=m=>{const sc=Math.min(1,m/Math.max(im.width,im.height)),c=document.createElement('canvas');c.width=Math.max(1,Math.round(im.width*sc));c.height=Math.max(1,Math.round(im.height*sc));c.getContext('2d').drawImage(im,0,0,c.width,c.height);return c};
        res({img:mk(1280).toDataURL('image/jpeg',.82),thumb:mk(96).toDataURL('image/jpeg',.6)});
      };
      im.onerror=()=>res(null);
      im.src=fr.result;
    };
    fr.readAsDataURL(file);
  });
}
async function addImgs(files){
  for(const f of files){
    if(pending.length>=4){local('max 4 images',true);break}
    const r=await readImg(f);if(r)pending.push(r);
  }
  renderAtts();
}
function renderAtts(){
  $('#atts').innerHTML=pending.map((p,i)=>'<span class="at"><img src="'+p.thumb+'"><button data-x="'+i+'">✕</button></span>').join('');
  $('#atts').classList.toggle('hidden',!pending.length);
}
function setTheme(a){
  const i=BOOT.themes.indexOf(root.dataset.theme),t=a?a.toLowerCase():BOOT.themes[(i+1)%BOOT.themes.length];
  if(!BOOT.themes.includes(t)){local('themes: '+BOOT.themes.join(' · '),true);return}
  ls('vez.theme',t);applyLook();local('theme → '+t);
}
function setFont(a){
  const f=a?a.toLowerCase():(root.dataset.font==='pixel'?'clean':'pixel');
  if(f!=='pixel'&&f!=='clean'){local('fonts: pixel · clean',true);return}
  ls('vez.font',f);applyLook();local('font → '+f);
}
function openOv(id){OVS.forEach(o=>$(o).classList.toggle('open',o===id));scrim.classList.add('on')}
function closeOv(){OVS.forEach(o=>$(o).classList.remove('open'));scrim.classList.remove('on')}
async function loadProviders(){try{PV=await api('/api/providers')}catch(e){}paintModel()}
function renderModels(v){
  const q=(v||'').trim().toLowerCase();
  let h='';
  for(const p of PV.providers){
    const ms=p.models.filter(m=>!q||(m.id+' '+m.label+' '+p.name).toLowerCase().includes(q));
    if(q&&!ms.length)continue;
    h+='<div class="pg"><div class="pgh"><span>'+esc(p.name)+'</span><small>'+p.models.length+' models'+(p.key?'':' · no key')+'</small><span class="pga"><button data-ref="'+esc(p.name)+'" title="refresh models">⟳</button>'+(PV.providers.length>1?'<button data-rm="'+esc(p.name)+'" title="remove">✕</button>':'')+'</span></div>';
    h+=ms.map(m=>'<div class="mi2'+(p.name===PV.provider&&m.id===PV.model?' on':'')+'" data-p="'+esc(p.name)+'" data-m="'+esc(m.id)+'"><i class="rd"></i><b>'+esc(m.label)+'</b><span>'+esc(m.speed||(m.label!==m.id?m.id:''))+'</span></div>').join('')||'<div class="empty">no models yet · tap ⟳ to fetch</div>';
    h+='</div>';
  }
  if(q)h+='<div class="mi2" data-p="'+esc(PV.provider)+'" data-m="'+esc(v.trim())+'"><i class="rd"></i><b>use “'+esc(v.trim())+'”</b><span>custom id</span></div>';
  $('#mlist').innerHTML=h;
}
async function openModels(){
  await loadProviders();
  $('#msearch').value='';$('#mmsg').textContent='';renderModels('');
  openOv('#modelSheet');
  if(!coarse)$('#msearch').focus();
}
function openConnect(){
  $('#presets').innerHTML=PV.presets.map(p=>'<button data-n="'+esc(p[0])+'" data-u="'+esc(p[1])+'">'+esc(p[0])+'</button>').join('');
  $('#cMsg').textContent='';
  openOv('#connSheet');
}
function hideSlash(){slash.classList.add('hidden')}
function renderSlash(){
  if(!slashItems.length){hideSlash();return}
  slash.innerHTML=slashItems.map((c,i)=>'<div class="mi'+(i===hl?' on':'')+'" data-i="'+i+'">'+(menuMode==='file'?'<b class="fb">'+esc(c[0])+'</b>':'<b>/'+esc(c[0])+'</b><span>'+esc(c[1])+'</span>')+'</div>').join('');
  slash.classList.remove('hidden');
  const on=slash.querySelector('.on');if(on)on.scrollIntoView({block:'nearest'});
}
function updateSlash(){
  const v=input.value,pre=v.slice(0,input.selectionStart);
  const m=pre.match(/(^|\s)@([^\s]*)$/);
  clearTimeout(atT);
  if(m){
    atT=setTimeout(async()=>{
      let list=[];
      try{list=await api('/api/files?q='+encodeURIComponent(m[2]))}catch(e){}
      if(input.value.slice(0,input.selectionStart)!==pre)return;
      if(!list.length){hideSlash();return}
      slashItems=list.map(p=>[p]);hl=0;menuMode='file';atTok={start:pre.length-m[2].length-1,end:pre.length};renderSlash();
    },120);
    return;
  }
  if(v.startsWith('/')&&!/\s/.test(v)){
    const p=v.slice(1).toLowerCase();
    menuMode='cmd';slashItems=BOOT.commands.filter(c=>c[0].startsWith(p));hl=0;renderSlash();
  }else hideSlash();
}
function autosize(){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,160)+'px'}
function pickSlash(i){
  const c=slashItems[i];if(!c)return;
  if(menuMode==='file'){
    const p=c[0],v=input.value,ins='@'+p+(p.endsWith('/')?'':' ');
    input.value=v.slice(0,atTok.start)+ins+v.slice(atTok.end);
    const pos=atTok.start+ins.length;input.setSelectionRange(pos,pos);input.focus();autosize();
    hideSlash();if(p.endsWith('/'))updateSlash();
    return;
  }
  hideSlash();
  if(c[2]&&!BOOT.noarg.includes(c[0])){input.value='/'+c[0]+' ';autosize();input.focus()}
  else{input.value='';autosize();runCmd('/'+c[0])}
}
async function runCmd(raw,hlName){
  const m=raw.slice(1).trim().match(/^(\S*)\s*([\s\S]*)$/);
  const c=(m[1]||'').toLowerCase(),arg=m[2].trim();
  let name=BOOT.aliases[c]||c;
  if(!BOOT.commands.some(x=>x[0]===name)){
    const cand=BOOT.commands.map(x=>x[0]).filter(n=>n.startsWith(c));
    name=cand.includes(hlName)?hlName:cand[0];
  }
  if(!name){local('unknown command /'+c,true);return}
  if(name==='sessions'){openDrawer();return}
  if(name==='help'){helpCard();return}
  if(name==='skills'){skillsCard();return}
  if(name==='notify'){setNotify(arg);return}
  if(name==='mcp'&&!arg){mcpCard();return}
  if(name==='models'&&!arg){openModels();return}
  if(name==='connect'&&!arg){await loadProviders();openConnect();return}
  if(name==='copy'){if(lastAI){copyText(lastAI);local('copied last reply')}else local('nothing to copy');return}
  if(name==='theme'){setTheme(arg);return}
  if(name==='font'){setFont(arg);return}
  try{
    if(name==='connect'){await api('/api/connect',{arg:arg});await loadProviders()}
    else await api('/api/cmd',{name:name,arg:arg})
  }catch(e){local('request failed',true);return}
  poll();
}
async function submit(){
  const raw=input.value.trim();
  if(!raw&&!pending.length)return;
  const hlName=menuMode==='cmd'&&!slash.classList.contains('hidden')&&slashItems[hl]?slashItems[hl][0]:null;
  if(raw.startsWith('/')){input.value='';autosize();hideSlash();runCmd(raw,hlName);return}
  if(raw.startsWith('!')){input.value='';autosize();runCmd('/shell '+raw.slice(1).trim());return}
  if(busy)return;
  const imgs=pending.slice();
  const text=raw||'Look at the attached image(s).';
  input.value='';autosize();hideSlash();pending=[];renderAtts();
  let r;
  try{r=await api('/api/send',{text:text,images:imgs.map(x=>x.img),thumbs:imgs.map(x=>x.thumb)})}catch(e){local('request failed',true);input.value=raw;pending=imgs;renderAtts();return}
  if(!r.ok){input.value=raw;pending=imgs;renderAtts();local('busy, stop the current task first',true);return}
  busy=true;pin=true;setSend();poll();
}
async function stop(){try{await api('/api/stop',{})}catch(e){}}
async function openDrawer(){
  let list=[];
  try{list=await api('/api/sessions')}catch(e){}
  const base=String(sid||'').split('.')[0];
  $('#sessList').innerHTML=list.map(s=>'<div class="si'+(s.id===base?' cur':'')+'" data-id="'+s.id+'"><span class="t">'+esc(s.title)+'</span><small>'+esc(when(s.updated))+'</small><button class="x" data-del="'+s.id+'">✕</button></div>').join('')||'<div class="note">no saved sessions</div>';
  openOv('#drawer');
}
input.addEventListener('input',()=>{autosize();updateSlash()});
input.addEventListener('keydown',e=>{
  const open=!slash.classList.contains('hidden');
  if(open&&(e.key==='ArrowDown'||e.key==='ArrowUp')){e.preventDefault();hl=(hl+(e.key==='ArrowDown'?1:-1)+slashItems.length)%slashItems.length;renderSlash();return}
  if(open&&e.key==='Tab'){e.preventDefault();if(menuMode==='file')pickSlash(hl);else{const c=slashItems[hl];if(c){input.value='/'+c[0]+' ';autosize();hideSlash()}}return}
  if(open&&menuMode==='file'&&e.key==='Enter'&&!e.shiftKey){e.preventDefault();pickSlash(hl);return}
  if(e.key==='Enter'&&!e.shiftKey&&!coarse){e.preventDefault();submit()}
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if(!slash.classList.contains('hidden'))hideSlash();
    else if(scrim.classList.contains('on'))closeOv();
    else if(busy)stop();
  }else if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openModels()}
  else if(e.key==='/'&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();input.focus();input.value='/';updateSlash()}
});
slash.addEventListener('mousedown',e=>{e.preventDefault();const d=e.target.closest('.mi');if(d)pickSlash(+d.dataset.i)});
slash.addEventListener('touchstart',e=>{const d=e.target.closest('.mi');if(d){e.preventDefault();pickSlash(+d.dataset.i)}},{passive:false});
$('#mlist').addEventListener('click',async e=>{
  const rf=e.target.closest('[data-ref]'),rm=e.target.closest('[data-rm]'),it=e.target.closest('.mi2');
  if(rf){
    rf.textContent='…';
    let r={};
    try{r=await api('/api/refresh',{name:rf.dataset.ref})}catch(x){r={ok:false,err:'request failed'}}
    $('#mmsg').textContent=r.ok?'':r.err;
    await loadProviders();renderModels($('#msearch').value);return;
  }
  if(rm){
    if(!confirm('Remove provider '+rm.dataset.rm+'?'))return;
    try{await api('/api/disconnect',{name:rm.dataset.rm})}catch(x){}
    await loadProviders();renderModels($('#msearch').value);poll();return;
  }
  if(it){
    closeOv();
    try{await api('/api/model',{provider:it.dataset.p,model:it.dataset.m})}catch(x){}
    await loadProviders();poll();
  }
});
$('#msearch').addEventListener('input',e=>renderModels(e.target.value));
$('#addProv').onclick=openConnect;
$('#connBack').onclick=openModels;
$('#presets').addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  $('#cName').value=b.dataset.n.toLowerCase();$('#cUrl').value=b.dataset.u;$('#cKey').focus();
});
$('#cGo').onclick=async()=>{
  const g=$('#cGo'),msg=$('#cMsg');
  g.disabled=true;g.textContent='Connecting…';msg.style.color='';msg.textContent='';
  try{
    const r=await api('/api/connect',{name:$('#cName').value,url:$('#cUrl').value,key:$('#cKey').value,quiet:true});
    if(r.ok){$('#cKey').value='';await loadProviders();poll();await openModels()}
    else{msg.style.color='var(--er)';msg.textContent=r.err}
  }catch(e){msg.style.color='var(--er)';msg.textContent='request failed'}
  g.disabled=false;g.textContent='Connect & fetch models';
};
document.addEventListener('click',e=>{
  const cp=e.target.closest('.cp');
  if(cp){
    copyText(cp.closest('.code').querySelector('code').innerText);
    cp.textContent='copied';setTimeout(()=>cp.textContent='copy',1200);
  }
});
$('#modelBtn').onclick=openModels;
sendBtn.onclick=()=>busy?stop():submit();
$('#menuBtn').onclick=openDrawer;
scrim.onclick=closeOv;
$('#newBtn').onclick=async()=>{closeOv();try{await api('/api/cmd',{name:'new',arg:''})}catch(e){}poll()};
$('#sessList').addEventListener('click',async e=>{
  const x=e.target.closest('[data-del]');
  if(x){e.stopPropagation();try{await api('/api/delete',{id:x.dataset.del})}catch(err){}openDrawer();return}
  const s=e.target.closest('.si');
  if(s){closeOv();try{await api('/api/load',{id:s.dataset.id})}catch(err){}poll()}
});
$('#planChip').onclick=()=>runCmd('/plan');
$('#attach').onclick=()=>$('#file').click();
$('#file').onchange=e=>{addImgs([...e.target.files]);e.target.value=''};
$('#atts').addEventListener('click',e=>{const x=e.target.closest('[data-x]');if(x){pending.splice(+x.dataset.x,1);renderAtts()}});
input.addEventListener('paste',e=>{const f=[...((e.clipboardData&&e.clipboardData.files)||[])].filter(x=>x.type.startsWith('image/'));if(f.length){e.preventDefault();addImgs(f)}});
const comp=document.querySelector('.composer');
comp.addEventListener('dragover',e=>e.preventDefault());
comp.addEventListener('drop',e=>{const f=[...e.dataTransfer.files].filter(x=>x.type.startsWith('image/'));if(f.length){e.preventDefault();addImgs(f)}});
notifyOn=ls('vez.notify')==='1';
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').then(r=>{swReg=r}).catch(()=>{});
$('#goalChip').onclick=()=>runCmd('/goal stop');
setInterval(()=>{
  if(busy){think.style.display='flex';$('#thinkTxt').textContent=busyLabel+' '+fmtTime((performance.now()-busyAt)/1000)+' · esc to stop'}
  else think.style.display='none';
},120);
(function(){
  const c=$('#bg'),x=c.getContext('2d'),G=24;
  if(matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  let W=0,H=0,cells=[],last=0;
  const size=()=>{const d=devicePixelRatio||1;W=innerWidth;H=innerHeight;c.width=W*d;c.height=H*d;x.setTransform(d,0,0,d,0,0)};
  size();addEventListener('resize',size);
  function frame(ts){
    if(!document.hidden&&ts-last>90){
      last=ts;x.clearRect(0,0,W,H);
      if(Math.random()<.45&&cells.length<14)cells.push({x:Math.floor(Math.random()*W/G),y:Math.floor(Math.random()*H/G),t:0,l:50+Math.random()*90});
      const col=getComputedStyle(root).getPropertyValue('--ac-rgb').trim();
      cells=cells.filter(q=>{
        q.t++;if(q.t>=q.l)return false;
        x.fillStyle='rgba('+col+','+(Math.sin(Math.PI*q.t/q.l)*.55)+')';
        x.fillRect(q.x*G+1,q.y*G+1,G-1,G-1);return true;
      });
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
setSend();loadProviders();poll();
</script>
</body>
</html>
"""


def valid_sid(s):
    return bool(re.fullmatch(r"[0-9a-f]{8}", str(s or "")))


class WebCore:
    post_chat = VezCLI.post_chat
    api_chat = VezCLI.api_chat
    dispatch = VezCLI.dispatch
    save_session = VezCLI.save_session
    short_cwd = VezCLI.short_cwd

    def __init__(self, cfg):
        self.cfg = cfg
        self.cur_model = cfg["model"]
        self.client = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self.cancel_evt = threading.Event()
        self.lock = threading.Lock()
        self.busy = False
        self.label = ""
        self.since = 0.0
        self.polls = {}
        self.goal = None
        self.sub_sem = threading.Semaphore(int(cfg.get("sub_parallel", 3)))
        self.undo = []
        self.turn_id = 0
        self.last_pt = 0
        self.plan = False
        self.mcp = McpHub(cfg.setdefault("mcp", {}), self.client)
        self.mcp.start()
        self.tools = TOOLS + [ASK_TOOL, SPAWN_TOOL, TODO_TOOL]
        self.web = True
        self.perm = cfg.get("permissions", "auto")
        self.always = set()
        self.epoch = 0
        self.reset()

    @property
    def tools(self):
        base = self.base_tools + ([GOAL_TOOL] if self.goal else [])
        if self.plan:
            return [t for t in base if t["function"]["name"] in PLAN_ALLOWED] + [EXIT_PLAN_TOOL]
        return base + self.mcp.specs()

    @tools.setter
    def tools(self, v):
        self.base_tools = v

    def reset(self):
        self.sess = new_sess(self.cur_model)
        self.convo = []
        self.events = []
        self.undo = []
        self.last_pt = 0

    def note(self, text, err=False):
        self.events.append({"t": "note", "text": text, "err": err})

    def state(self, since):
        since = max(0, min(since, len(self.events)))
        return {
            "sid": f"{self.sess['id']}.{self.epoch}",
            "base": since,
            "events": self.events[since:],
            "busy": self.busy,
            "label": self.label,
            "elapsed": (time.perf_counter() - self.since) if self.busy else 0,
            "title": self.sess["title"],
            "tokens": self.sess["tokens"],
            "model": self.cur_model,
            "provider": self.cfg["provider"],
            "perm": self.perm,
            "plan": self.plan,
            "goal": ({"text": self.goal["text"], "round": self.goal["round"], "max": int(self.cfg.get("goal_rounds", 40))} if self.goal else None),
            "cwd": self.short_cwd(),
        }

    def send(self, text, display=None, images=None, thumbs=None, content=None, files=None):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        self.cancel_evt.clear()
        self.label = "thinking"
        self.since = time.perf_counter()
        self.turn_id += 1
        shown = display or text or ""
        if content is None:
            body, files = expand_mentions(text)
            content = ([{"type": "text", "text": body}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]) if images else body
        if self.sess["title"] == "new session":
            self.sess["title"] = (shown.splitlines() or [shown])[0][:50] or "new session"
        self.convo.append({"role": "user", "content": content})
        self.events.append({"t": "user", "text": shown, "files": files or [], "thumbs": thumbs or []})
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def stop(self):
        if self.busy:
            self.cancel_evt.set()
            self.label = "stopping"

    def load(self, sid):
        if self.busy or not valid_sid(sid):
            return False
        try:
            data = json.loads((SESSIONS / f"{sid}.json").read_text())
        except Exception:
            return False
        self.save_session()
        data.setdefault("tokens", 0)
        data.setdefault("title", "untitled")
        self.sess = data
        self.convo = data.get("messages", [])
        self.events = data.get("events", [])
        try:
            os.chdir(data.get("cwd") or os.getcwd())
        except Exception:
            pass
        if restore_model(self.cfg, data):
            self.cur_model = self.cfg["model"]
        for e in self.events:
            if e.get("t") == "poll" and e.get("run"):
                e.update(run=False, status="skipped", answers=[])
            elif e.get("t") in ("todo", "plan", "agent") and e.get("run"):
                e["run"] = False
        self.undo = []
        self.last_pt = 0
        return True

    def delete(self, sid):
        if valid_sid(sid):
            try:
                (SESSIONS / f"{sid}.json").unlink()
            except Exception:
                pass

    def providers(self):
        out = []
        for name, p in self.cfg["providers"].items():
            ms = []
            for m in p.get("models", []):
                label, speed = KNOWN.get(m, (m, ""))
                ms.append({"id": m, "label": label, "speed": speed})
            out.append({"name": name, "url": p["base_url"], "key": bool(p.get("api_key")), "models": ms})
        return {"provider": self.cfg["provider"], "model": self.cur_model, "providers": out, "presets": PRESETS}

    def select(self, prov, mid):
        try:
            select_model(self.cfg, prov, mid)
        except Exception as e:
            self.note(str(e), True)
            return False
        self.cur_model = mid
        self.note(f"model → {model_label(self.cfg, mid)}")
        return True

    def connect(self, d):
        if d.get("arg"):
            parsed = parse_connect(str(d["arg"]))
            if not parsed:
                err = "usage: /connect <name> <base_url> [key]  or  /connect <preset> <key>"
                self.note(err, True)
                return {"ok": False, "err": err}
            name, url, key = parsed
        else:
            name, url, key = d.get("name", ""), str(d.get("url", "")).strip(), str(d.get("key", "")).strip()
        try:
            n, c, err = add_provider(self.cfg, self.client, name, url, key)
        except Exception as e:
            if not d.get("quiet"):
                self.note(str(e), True)
            return {"ok": False, "err": str(e)}
        self.note(f"connected {n} · {c} models" + (f" ({err})" if err else ""), bool(err) and not c)
        return {"ok": True, "name": n, "count": c, "err": err}

    def refresh(self, name):
        try:
            return {"ok": True, "count": refresh_provider(self.cfg, self.client, str(name))}
        except Exception as e:
            return {"ok": False, "err": str(e)[:200]}

    def remove(self, name):
        try:
            remove_provider(self.cfg, str(name))
        except Exception as e:
            return {"ok": False, "err": str(e)}
        self.cur_model = self.cfg["model"]
        return {"ok": True}

    def retry(self):
        if self.busy:
            self.note("busy, stop the current task first", True)
            return
        skip = (GOAL_NUDGE, "continue")
        ui = [i for i, m in enumerate(self.convo) if m.get("role") == "user" and m.get("content") not in skip and not (isinstance(m.get("content"), str) and m["content"].startswith("["))]
        eu = [i for i, e in enumerate(self.events) if e.get("t") == "user"]
        if not ui or not eu:
            self.note("nothing to retry", True)
            return
        content = self.convo[ui[-1]]["content"]
        ev = self.events[eu[-1]]
        shown, files, thumbs = ev.get("text", ""), ev.get("files") or [], ev.get("thumbs") or []
        del self.convo[ui[-1]:]
        del self.events[eu[-1]:]
        self.epoch += 1
        self.send(None, display=shown, content=content, files=files, thumbs=thumbs)

    def exec_tool(self, name, args):
        if self.plan and name not in PLAN_ALLOWED:
            return "blocked: plan mode is on. Only read-only tools are allowed. Call exit_plan when the plan is ready.", False, ""
        if name.startswith("mcp__"):
            try:
                out, ok = self.mcp.call(name, args)
            except Exception as e:
                out, ok = f"mcp call failed: {e}", False
            return out, ok, ""
        snap = None
        if name in ("write_file", "edit_file"):
            try:
                pth = Path(str(args.get("path", ""))).expanduser()
                snap = (str(pth), pth.read_text(errors="replace") if pth.is_file() else None)
            except Exception:
                snap = None
        result, ok = self.dispatch(name, args)
        diff = ""
        if snap and ok:
            try:
                diff = make_diff(snap[0], snap[1], Path(snap[0]).read_text(errors="replace"))
                self.undo.append({"path": snap[0], "old": snap[1], "turn": self.turn_id})
            except Exception:
                pass
        return result, ok, diff

    def undo_last(self):
        if self.busy:
            self.note("busy, stop the current task first", True)
            return
        if not self.undo:
            self.note("nothing to undo")
            return
        tid = self.undo[-1]["turn"]
        done = []
        while self.undo and self.undo[-1]["turn"] == tid:
            u = self.undo.pop()
            try:
                f = Path(u["path"])
                if u["old"] is None:
                    f.unlink()
                else:
                    f.write_text(u["old"])
                done.append(u["path"])
            except Exception as e:
                self.note(f"undo failed for {u['path']}: {e}", True)
        if done:
            self.note("↶ reverted " + ", ".join(os.path.basename(x) for x in done))
            self.convo.append({"role": "user", "content": "[note] The user reverted your last file changes: " + ", ".join(done)})

    def todo(self, args):
        raw = args.get("todos")
        if not isinstance(raw, list):
            return "todo_write needs a todos list", False
        items = []
        for t in raw:
            if isinstance(t, str):
                items.append({"text": t, "status": "pending"})
            elif isinstance(t, dict):
                text = str(t.get("content") or t.get("text") or "").strip()
                st = str(t.get("status") or "pending")
                st = "done" if st in ("done", "completed") else st if st in ("pending", "in_progress") else "pending"
                if text:
                    items.append({"text": text, "status": st})
        last = next((e for e in reversed(self.events) if e.get("t") == "todo"), None)
        run = any(i["status"] != "done" for i in items)
        if last is not None and last.get("run"):
            last.update(items=items, run=run)
        else:
            self.events.append({"t": "todo", "items": items, "run": run})
        done = sum(i["status"] == "done" for i in items)
        return f"todo updated: {done}/{len(items)} done", True

    def exit_plan(self, args):
        text = str(args.get("plan") or "").strip()
        if not self.plan:
            return "plan mode is not on", False
        if not text:
            return "exit_plan needs a plan", False
        ev = {"t": "plan", "text": text, "status": "pending", "run": True}
        self.events.append(ev)
        out, _ = self.ask({"questions": [{"title": "Approve this plan?", "options": [{"label": "Approve & build", "description": "leave plan mode and start working"}, {"label": "Revise", "description": "tell me what to change below"}], "other": True}]})
        if '"Approve & build"' in out:
            self.plan = False
            ev.update(status="approved", run=False)
            return "plan approved, plan mode is off. execute the plan now.", True
        ev.update(status="revise", run=False)
        return "plan not approved. user feedback: " + out + "\nrevise the plan and call exit_plan again.", True

    def sub_tools(self):
        if self.plan:
            return [t for t in TOOLS if t["function"]["name"] in PLAN_ALLOWED]
        return TOOLS + self.mcp.specs()

    def compact(self, everything=False):
        ui = [i for i, m in enumerate(self.convo) if m.get("role") == "user"]
        if everything:
            cut = len(self.convo)
        elif len(ui) >= 2:
            cut = ui[-1]
        else:
            return False
        head, tail = self.convo[:cut], self.convo[cut:]
        if len(head) < 3:
            return False
        lines = []
        for m in head:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") if x.get("type") == "text" else "[image]" for x in c)
            if m.get("role") == "tool":
                c = clip(str(c), 400)
            calls = ", ".join(f"{t['function']['name']}({t['function']['arguments'][:120]})" for t in m.get("tool_calls", []))
            lines.append(f"{m.get('role')}: {c or ''}{' [calls: ' + calls + ']' if calls else ''}")
        req = [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": clip("\n".join(lines), 60000)}]
        self.label = "compacting"
        resp = self.call_api(req, [])
        text = str(((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return False
        self.convo[:] = [{"role": "user", "content": "[Summary of the earlier conversation]\n" + text}, {"role": "assistant", "content": "Understood. Continuing from that summary."}] + tail
        self.last_pt = 0
        self.note(f"compacted {len(head)} messages into a summary")
        return True

    def maybe_compact(self):
        lim = int(self.cfg.get("compact_at", 48000))
        if lim and self.last_pt > lim and len(self.convo) > 8:
            self.compact()

    def compact_job(self):
        with self.lock:
            if self.busy:
                return
            self.busy = True
        self.cancel_evt.clear()
        self.since = time.perf_counter()
        self.label = "compacting"
        try:
            if not self.compact(True):
                self.note("nothing to compact yet")
        except Exception as e:
            self.note(str(e), True)
        finally:
            try:
                self.save_session()
            except Exception:
                pass
            self.busy = False

    def pause(self, secs, why):
        m, sec = divmod(int(secs), 60)
        self.note(f"api stopped ({str(why)[:100]}). waiting {m}:{sec:02d}, then continuing", True)
        end = time.monotonic() + secs
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            if self.cancel_evt.is_set():
                return False
            self.label = f"api limit · retry in {int(left)}s"
            time.sleep(0.4)
        self.label = "continuing"
        return True

    def call_api(self, msgs, tools=None):
        tries = 0
        while True:
            try:
                resp = self.api_chat(msgs, tools)
                if tools is None:
                    self.last_pt = int((resp.get("usage") or {}).get("prompt_tokens") or 0)
                return resp
            except ApiError as e:
                tries += 1
                if not e.wait or tries > int(self.cfg.get("retry_max", 20)):
                    raise
                if not self.pause(int(self.cfg.get("retry_wait", 120)), e):
                    raise Cancelled()

    def launch_sub(self, args):
        task = str(args.get("task") or "").strip()
        if not task:
            return None
        name = str(args.get("name") or "agent").strip()[:40] or "agent"
        ev = {"t": "agent", "id": uuid.uuid4().hex[:8], "name": name, "task": task, "steps": [], "result": "", "status": "run", "run": True, "dur": 0}
        self.events.append(ev)
        th = threading.Thread(target=self.run_sub, args=(ev,), daemon=True)
        th.start()
        return ev, th

    def run_sub(self, ev):
        t0 = time.perf_counter()
        msgs = [{"role": "system", "content": build_system(False) + SUB_RULES}, {"role": "user", "content": ev["task"]}]
        final, status = "", "done"
        try:
            with self.sub_sem:
                for _ in range(int(self.cfg.get("sub_steps", 15))):
                    if self.cancel_evt.is_set():
                        raise Cancelled()
                    resp = self.call_api(msgs, self.sub_tools())
                    msg = (resp.get("choices") or [{}])[0].get("message") or {}
                    text = (msg.get("content") or "").strip()
                    u = resp.get("usage") or {}
                    self.sess["tokens"] += int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
                    norm = norm_calls(msg.get("tool_calls") or [])
                    entry = {"role": "assistant", "content": text}
                    if norm:
                        entry["tool_calls"] = [
                            {"id": n["id"], "type": "function", "function": {"name": n["name"], "arguments": json.dumps(n["args"])}}
                            for n in norm
                        ]
                    msgs.append(entry)
                    if not norm:
                        final = text or "(no report)"
                        break
                    for n in norm:
                        summary = describe(n["name"], n["args"])
                        if n["err"]:
                            result, ok = n["err"], False
                        elif not self.permit(n["name"], summary):
                            result, ok = "denied by user", False
                        else:
                            result, ok, _ = self.exec_tool(n["name"], n["args"])
                        result = clean(result)
                        ev["steps"].append({"name": n["name"], "summary": summary, "ok": ok})
                        msgs.append({"role": "tool", "tool_call_id": n["id"], "content": clip(result, 12000)})
                else:
                    final = "(sub-agent hit its step limit)"
        except Cancelled:
            final, status = "stopped", "stopped"
        except Exception as e:
            final, status = f"sub-agent failed: {e}", "failed"
        ev.update(result=final, status=status, dur=time.perf_counter() - t0, run=False)

    def goal_continue(self, note):
        g = self.goal
        if not g:
            return False
        if g["done"]:
            self.note("✓ goal complete")
            return False
        if note == "stopped" or self.cancel_evt.is_set():
            self.note("goal stopped")
            return False
        if note and note != "step limit reached":
            self.note("goal stopped: " + note, True)
            return False
        if g["round"] >= int(self.cfg.get("goal_rounds", 40)):
            self.note("goal stopped: round limit reached", True)
            return False
        g["round"] += 1
        self.note(f"↻ goal round {g['round']}")
        self.convo.append({"role": "user", "content": GOAL_NUDGE})
        return True

    def run(self):
        try:
            while True:
                note = self.turn()
                if not self.goal_continue(note):
                    break
        except Exception as e:
            self.note(str(e), True)
        finally:
            self.goal = None
            for e in self.events:
                if e.get("t") in ("todo", "plan") and e.get("run"):
                    e["run"] = False
                    if e.get("status") == "pending":
                        e["status"] = "dismissed"
            try:
                self.save_session()
            except Exception:
                pass
            self.busy = False

    def permit(self, name, summary):
        if self.plan or self.perm != "ask" or (name not in GUARDED and not name.startswith("mcp__")) or name in self.always:
            return True
        out, _ = self.ask({"questions": [{"title": f"Allow {name}?", "options": [{"label": "Allow", "description": summary}, "Always allow " + name, "Deny"], "other": False}]})
        if "Always allow" in out:
            self.always.add(name)
            return True
        return '"Allow"' in out

    def ask(self, args):
        raw = args.get("questions")
        if not isinstance(raw, list):
            raw = [args] if args.get("options") else []
        qs = []
        for q in raw:
            if not isinstance(q, dict):
                continue
            opts = []
            for o in q.get("options") or []:
                if isinstance(o, dict):
                    lab = str(o.get("label") or o.get("title") or o.get("name") or "")
                    desc = str(o.get("description") or "")
                else:
                    lab, desc = str(o), ""
                if lab:
                    opts.append({"label": lab, "desc": desc})
            title = str(q.get("title") or q.get("question") or "").strip()
            if title and opts:
                qs.append({"title": title, "options": opts, "multi": bool(q.get("multi")), "other": q.get("other") is not False})
        if not qs:
            return "ask_user needs questions with a title and options", False
        pid = uuid.uuid4().hex[:8]
        ev = {"t": "poll", "id": pid, "questions": qs, "answers": None, "status": "pending", "run": True}
        wait = threading.Event()
        self.polls[pid] = {"ev": ev, "wait": wait}
        self.events.append(ev)
        self.label = "waiting for your answer"
        while not wait.wait(0.3):
            if self.cancel_evt.is_set():
                ev.update(status="skipped", answers=[], run=False)
                break
        self.polls.pop(pid, None)
        if ev["status"] != "answered":
            return "user skipped the questions", True
        ans = list(ev["answers"]) + [[]] * (len(qs) - len(ev["answers"]))
        return json.dumps([{"question": q["title"], "answer": a} for q, a in zip(qs, ans)]), True

    def answer(self, pid, answers):
        p = self.polls.get(str(pid))
        if not p or p["ev"]["status"] != "pending":
            return False
        ev = p["ev"]
        if isinstance(answers, list):
            fixed = [[str(x) for x in (a if isinstance(a, list) else [a])] for a in answers]
            ev.update(status="answered", answers=fixed, run=False)
        else:
            ev.update(status="skipped", answers=[], run=False)
        p["wait"].set()
        return True

    def cmd(self, name, arg):
        if name == "new":
            if self.busy:
                self.note("busy, stop the current task first", True)
                return
            self.save_session()
            self.reset()
        elif name == "rename":
            if arg:
                self.sess["title"] = arg[:60]
                self.save_session()
            else:
                self.note("usage: /rename <name>")
        elif name == "cd":
            if not arg:
                self.note(os.getcwd())
                return
            try:
                os.chdir(Path(arg).expanduser())
                self.note(f"→ {os.getcwd()}")
            except Exception as e:
                self.note(str(e), True)
        elif name == "models":
            if arg:
                prov, mid = resolve_model(self.cfg, arg)
                self.select(prov, mid)
        elif name == "perm":
            if arg in ("auto", "ask"):
                self.perm = arg
                self.cfg["permissions"] = arg
                save_cfg(self.cfg)
                self.note(f"permissions → {arg}")
            else:
                self.note(f"permissions: {self.perm}  (use /perm auto or /perm ask)")
        elif name == "goal":
            if arg == "stop":
                self.stop()
            elif not arg:
                g = self.goal
                self.note(f"goal: {g['text']} (round {g['round']})" if g else "no active goal, use /goal <what to finish>")
            elif self.busy:
                self.note("busy, stop the current task first", True)
            else:
                self.goal = {"text": arg, "round": 1, "done": False}
                if not self.send(GOAL_PROMPT.replace("{goal}", arg), display="/goal " + arg):
                    self.goal = None
        elif name == "plan":
            self.plan = (arg == "on") if arg in ("on", "off") else not self.plan
            self.note("plan mode on, read-only until you approve a plan" if self.plan else "plan mode off")
        elif name == "undo":
            self.undo_last()
        elif name == "compact":
            if self.busy:
                self.note("busy, stop the current task first", True)
            else:
                threading.Thread(target=self.compact_job, daemon=True).start()
        elif name == "mcp":
            parts = arg.split(None, 2)
            sub = parts[0] if parts else ""
            if sub == "add" and len(parts) >= 3:
                target = parts[2].strip()
                try:
                    conf = {"url": target} if target.startswith("http") else {"command": shlex.split(target)[0], "args": shlex.split(target)[1:]}
                except Exception as e:
                    self.note(str(e), True)
                    return
                self.cfg["mcp"][parts[1]] = conf
                save_cfg(self.cfg)
                self.mcp.add(parts[1], conf)
                self.note(f"mcp {parts[1]} connecting")
            elif sub == "remove" and len(parts) >= 2:
                self.cfg["mcp"].pop(parts[1], None)
                save_cfg(self.cfg)
                self.mcp.remove(parts[1])
                self.note(f"mcp {parts[1]} removed")
            elif sub == "reload":
                for nm, c in list(self.cfg["mcp"].items()):
                    self.mcp.add(nm, c)
                self.note("mcp reloading")
            else:
                self.note("usage: /mcp add <name> <command or url> · /mcp remove <name> · /mcp reload", True)
        elif name == "retry":
            self.retry()
        elif name == "export":
            self.note(export_file(self.sess, self.events))
        elif name in ("shell", "image"):
            if not arg:
                self.note(f"usage: /{name} <{'command' if name == 'shell' else 'prompt'}>")
            else:
                threading.Thread(target=self.aux, args=(name, arg), daemon=True).start()
        else:
            self.note(f"unknown command /{name}", True)

    def aux(self, kind, arg):
        ev = {
            "t": "tool",
            "name": kind,
            "summary": describe(kind, {"command": arg, "prompt": arg}),
            "result": "",
            "ok": True,
            "dur": 0,
            "run": True,
        }
        self.events.append(ev)
        s = time.perf_counter()
        try:
            if kind == "shell":
                out, ok = run_shell(arg, None, int(self.cfg["shell_timeout"]))
            else:
                out, ok = generate_image(self.client, self.cfg, arg, None)
        except Exception as e:
            out, ok = str(e), False
        ev.update(result=clean(out)[:1500], ok=ok, dur=time.perf_counter() - s, run=False)

    def turn(self):
        t0 = time.perf_counter()
        calls = 0
        seen = {}
        note = None
        empties = 0
        try:
            for _ in range(int(self.cfg["max_steps"])):
                if self.cancel_evt.is_set():
                    note = "stopped"
                    break
                self.maybe_compact()
                self.label = "thinking"
                msgs = [{"role": "system", "content": build_system(self.web, True) + (PLAN_RULES if self.plan else "")}] + self.convo
                s = time.perf_counter()
                resp = self.call_api(msgs)
                dt = time.perf_counter() - s
                calls += 1
                msg = (resp.get("choices") or [{}])[0].get("message") or {}
                text = (msg.get("content") or "").strip()
                tcs = msg.get("tool_calls") or []
                usage = resp.get("usage") or {}
                ct = usage.get("completion_tokens")
                pt = usage.get("prompt_tokens")
                est = ct is None or pt is None
                if ct is None:
                    arg_len = sum(len(str((tc.get("function") or {}).get("arguments", ""))) for tc in tcs)
                    ct = max(1, (len(text) + arg_len) // 4)
                if pt is None:
                    pt = max(1, len(json.dumps(msgs)) // 4)
                ct, pt = int(ct), int(pt)
                self.sess["tokens"] += ct + pt
                norm = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    args, err = parse_args(fn.get("arguments"))
                    norm.append(
                        {
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            "name": fn.get("name", ""),
                            "args": args,
                            "err": err,
                        }
                    )
                entry = {"role": "assistant", "content": text}
                if norm:
                    entry["tool_calls"] = [
                        {
                            "id": n["id"],
                            "type": "function",
                            "function": {"name": n["name"], "arguments": json.dumps(n["args"])},
                        }
                        for n in norm
                    ]
                self.convo.append(entry)
                meta = {
                    "model": self.cur_model,
                    "time": dt,
                    "ct": ct,
                    "pt": pt,
                    "tps": ct / dt if dt > 0 else 0,
                    "est": est,
                    "turn": None,
                    "calls": None,
                }
                if not norm and calls > 1:
                    meta["turn"] = time.perf_counter() - t0
                    meta["calls"] = calls
                if text:
                    self.events.append({"t": "ai", "text": text, "meta": meta})
                elif not norm:
                    self.convo.pop()
                    empties += 1
                    if empties > int(self.cfg.get("retry_max", 20)) or not self.pause(int(self.cfg.get("retry_wait", 120)), "empty reply"):
                        self.note("empty reply from model", True)
                        break
                    self.convo.append({"role": "user", "content": "continue"})
                    continue
                if not norm:
                    break
                subs = {}
                for n in norm:
                    if n["name"] == "spawn_agent" and not n["err"]:
                        sub = self.launch_sub(n["args"])
                        if sub:
                            subs[n["id"]] = sub
                for i, n in enumerate(norm):
                    if self.cancel_evt.is_set():
                        for rest in norm[i:]:
                            self.convo.append({"role": "tool", "tool_call_id": rest["id"], "content": "cancelled by user"})
                        note = "stopped"
                        break
                    if n["name"] == "ask_user" and not n["err"]:
                        result, ok = self.ask(n["args"])
                        self.convo.append({"role": "tool", "tool_call_id": n["id"], "content": result})
                        continue
                    if n["name"] == "goal_done":
                        if self.goal:
                            self.goal["done"] = True
                        self.convo.append({"role": "tool", "tool_call_id": n["id"], "content": "goal marked complete" if self.goal else "no active goal"})
                        continue
                    if n["name"] == "todo_write":
                        result, ok = self.todo(n["args"])
                        self.convo.append({"role": "tool", "tool_call_id": n["id"], "content": result})
                        continue
                    if n["name"] == "exit_plan":
                        result, ok = self.exit_plan(n["args"])
                        self.convo.append({"role": "tool", "tool_call_id": n["id"], "content": result})
                        continue
                    if n["name"] == "spawn_agent":
                        if n["id"] in subs:
                            sev, th = subs[n["id"]]
                            self.label = "agents working"
                            while th.is_alive():
                                th.join(0.3)
                            out = sev["result"] or "sub-agent returned nothing"
                        else:
                            out = "spawn_agent needs a task"
                        self.convo.append({"role": "tool", "tool_call_id": n["id"], "content": clip(out, 12000)})
                        continue
                    summary = describe(n["name"], n["args"])
                    ev = {"t": "tool", "name": n["name"], "summary": summary, "result": "", "ok": True, "dur": 0, "run": True}
                    self.events.append(ev)
                    self.label = f"running {n['name']}"
                    diff = ""
                    key = n["name"] + json.dumps(n["args"], sort_keys=True)
                    seen[key] = seen.get(key, 0) + 1
                    ts = time.perf_counter()
                    if n["err"]:
                        result, ok = n["err"], False
                    elif seen[key] > 2:
                        result, ok = "duplicate call blocked, this exact call already ran. move on or finish.", False
                    elif not self.permit(n["name"], summary):
                        result, ok = "denied by user", False
                    else:
                        result, ok, diff = self.exec_tool(n["name"], n["args"])
                    result = clean(result)
                    ev.update(result=result[:1500], ok=ok, dur=time.perf_counter() - ts, run=False, diff=diff)
                    self.convo.append({"role": "tool", "tool_call_id": n["id"], "content": clip(result, 12000)})
                if note:
                    break
            else:
                note = "step limit reached"
        except Cancelled:
            note = "stopped"
        except Exception as e:
            note = str(e)
        finally:
            if note and not (self.goal and note == "step limit reached"):
                self.note(note, note != "stopped")
            try:
                self.save_session()
            except Exception:
                pass
        return note


def free_port(start):
    import socket

    for p in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def open_url(url):
    for cmd in (["termux-open-url", url], ["am", "start", "-a", "android.intent.action.VIEW", "-d", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            continue
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def run_web(cfg, port, resume):
    try:
        from flask import Flask, Response, abort, jsonify, request
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "flask"])
        from flask import Flask, Response, abort, jsonify, request
    from flask import cli as flask_cli

    flask_cli.show_server_banner = lambda *a, **k: None
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    core = WebCore(cfg)
    if resume:
        items = list_sessions()
        if items:
            core.load(items[0]["id"])
    token = uuid.uuid4().hex
    boot = {
        "commands": [c for c in COMMANDS if c[0] != "exit"],
        "aliases": ALIASES,
        "themes": THEMES,
        "noarg": ["connect", "mcp"],
        "skillDir": str(ROOT / "skills"),
    }
    app = Flask(__name__)

    @app.before_request
    def guard():
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.path.startswith("/api/") and request.headers.get("X-Vez") != token:
            abort(403)

    @app.get("/")
    def index():
        page = WEB_HTML.replace("__TOKEN__", token).replace("__BOOT__", json.dumps(boot))
        return Response(page, mimetype="text/html")

    @app.get("/api/poll")
    def poll():
        return jsonify(core.state(request.args.get("since", 0, type=int)))

    @app.post("/api/send")
    def send():
        d = request.get_json(silent=True) or {}
        text = str(d.get("text", "")).strip()
        imgs = [u for u in (d.get("images") or [])[:4] if isinstance(u, str) and u.startswith("data:image/") and len(u) < 6_000_000]
        thumbs = [u for u in (d.get("thumbs") or [])[:4] if isinstance(u, str) and u.startswith("data:image/") and len(u) < 40_000]
        return jsonify(ok=bool(text) and core.send(text, images=imgs, thumbs=thumbs))

    @app.post("/api/stop")
    def stop():
        core.stop()
        return jsonify(ok=True)

    @app.post("/api/cmd")
    def cmd():
        d = request.get_json(silent=True) or {}
        core.cmd(str(d.get("name", "")), str(d.get("arg", "")).strip())
        return jsonify(ok=True, model=core.cur_model)

    @app.get("/api/sessions")
    def sessions():
        return jsonify(list_sessions())

    @app.post("/api/load")
    def load():
        return jsonify(ok=core.load((request.get_json(silent=True) or {}).get("id")))

    @app.post("/api/delete")
    def delete():
        core.delete((request.get_json(silent=True) or {}).get("id"))
        return jsonify(ok=True)

    @app.get("/api/providers")
    def providers():
        return jsonify(core.providers())

    @app.post("/api/model")
    def set_model():
        d = request.get_json(silent=True) or {}
        return jsonify(ok=core.select(str(d.get("provider", "")), str(d.get("model", ""))))

    @app.post("/api/connect")
    def connect():
        return jsonify(core.connect(request.get_json(silent=True) or {}))

    @app.post("/api/refresh")
    def refresh():
        return jsonify(core.refresh((request.get_json(silent=True) or {}).get("name", "")))

    @app.post("/api/disconnect")
    def disconnect():
        return jsonify(core.remove((request.get_json(silent=True) or {}).get("name", "")))

    @app.get("/api/files")
    def files():
        return jsonify(list_files(request.args.get("q", "")))

    @app.get("/api/mcp")
    def mcp_status():
        return jsonify(core.mcp.status())

    @app.get("/sw.js")
    def sw():
        r = Response(
            "self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(clients.claim()));self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.matchAll({type:'window'}).then(l=>{for(const c of l){if('focus' in c)return c.focus()}return clients.openWindow('/')}))});",
            mimetype="application/javascript",
        )
        r.headers["Service-Worker-Allowed"] = "/"
        return r

    @app.get("/api/skills")
    def skills():
        return jsonify([{"name": x["name"], "description": x["description"], "path": x["path"]} for x in load_skills()])

    @app.post("/api/answer")
    def answer():
        d = request.get_json(silent=True) or {}
        return jsonify(ok=core.answer(d.get("id"), d.get("answers")))

    port = free_port(port)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  vezcli web  {url}\n  ctrl+c to stop\n")
    threading.Timer(1.0, open_url, args=(url,)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


def pick_mode():
    print("\n  vezcli\n")
    print("  [ 1 ] Terminal")
    print("  [ 2 ] Web\n")
    try:
        c = input("  C: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0)
    return "web" if c in ("2", "web", "w") else "term"


def headless(cfg, prompt, goal=False):
    core = WebCore(cfg)
    core.web = False
    core.tools = TOOLS + [SPAWN_TOOL, TODO_TOOL]
    core.mcp.wait(15)
    core.perm = "auto"
    if goal:
        core.cmd("goal", prompt)
    else:
        core.send(prompt)
    done = 0
    err = False
    try:
        while True:
            time.sleep(0.12)
            while done < len(core.events):
                e = core.events[done]
                k = e.get("t")
                if k in ("tool", "agent") and e.get("run"):
                    break
                if k == "agent":
                    print(f"  agent {e['name']} · {len(e['steps'])} steps · {e['status']}", file=sys.stderr)
                elif k == "tool":
                    print(f"  {'ok  ' if e['ok'] else 'fail'} {e['name']} {e['summary']}", file=sys.stderr)
                elif k == "ai":
                    print(e["text"])
                    m = e["meta"]
                    print(f"  {m['model']} · {fmt_time(m['time'])} · {m['ct']} tok · {m['tps']:.0f} tok/s", file=sys.stderr)
                elif k == "note":
                    print(e["text"], file=sys.stderr)
                    err = err or bool(e.get("err"))
                done += 1
            if not core.busy and done >= len(core.events):
                break
    except KeyboardInterrupt:
        core.stop()
        return 130
    return 1 if err else 0


def main():
    ap = argparse.ArgumentParser(prog="vezcli", description="terminal agent for your phone")
    ap.add_argument("prompt", nargs="*", help="run a task and exit (stdin is appended if piped)")
    ap.add_argument("-c", "--continue", dest="resume", action="store_true", help="resume last session")
    ap.add_argument("-m", "--model", help="model id or provider:model")
    ap.add_argument("-P", "--provider", help="provider name")
    ap.add_argument("-w", "--web", action="store_true", help="start web ui")
    ap.add_argument("-t", "--term", action="store_true", help="start terminal ui")
    ap.add_argument("-g", "--goal", action="store_true", help="run the prompt as an autonomous goal")
    ap.add_argument("-p", "--port", type=int, default=5000, help="web port")
    ap.add_argument("--models", action="store_true", help="list providers and models")
    ap.add_argument("-v", "--version", action="version", version=f"vezcli {VERSION}")
    a = ap.parse_args()
    cfg = load_config()
    if a.provider:
        if a.provider not in cfg["providers"]:
            raise SystemExit(f"unknown provider {a.provider}, have: {', '.join(cfg['providers'])}")
        cfg["provider"] = a.provider
        apply_provider(cfg)
    if a.model:
        prov, mid = resolve_model(cfg, a.model)
        cfg["provider"], cfg["model"] = prov, mid
        apply_provider(cfg)
    if a.models:
        for name, p in cfg["providers"].items():
            print(f"{name}{' *' if name == cfg['provider'] else ''}  {p['base_url']}")
            for m in p.get("models", []):
                print(f"  {m}{' *' if name == cfg['provider'] and m == cfg['model'] else ''}")
        return
    piped = "" if sys.stdin.isatty() else sys.stdin.read()
    prompt = " ".join(a.prompt).strip()
    if piped.strip():
        prompt = (prompt + "\n\n" + piped.strip()).strip()
    if prompt:
        raise SystemExit(headless(cfg, prompt, a.goal))
    mode = "web" if a.web else "term" if a.term else pick_mode()
    if mode == "web":
        run_web(cfg, a.port, a.resume)
    else:
        VezCLI(cfg, a.resume).run()


if __name__ == "__main__":
    main()
