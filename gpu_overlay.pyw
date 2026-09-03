#!/usr/bin/env python3
"""
GPU Overlay - a lightweight, transparent, always-on-top NVIDIA telemetry HUD
tuned for local LLM work (VRAM headroom, bandwidth saturation, CPU-offload
detection, Ollama model residency).

Stdlib only: tkinter + subprocess + urllib. No pip installs, no Electron.

Run:      pythonw gpu_overlay.pyw
Config:   config.json next to this file (auto-created, hot-reloadable)
Move:     left-click drag anywhere on the panel
Menu:     right-click the panel
Escape:   hold Ctrl+Shift to regain the mouse while click-through is on
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from collections import deque

APP_NAME = "GPU Overlay"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0

# Colour key used by "ghost" style; anything painted this exact colour becomes
# a hole in the window. Deliberately obscure so it never collides with real UI.
CHROMA_KEY = "#010203"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "refresh_ms": 1000,
    "opacity": 0.88,
    "style": "card",                 # "card" (translucent panel) | "ghost" (text only)
    "font_family": "Consolas",
    "font_size": 10,
    "position": {"x": 60, "y": 60},
    "locked": False,
    "click_through": False,
    "click_through_hotkey": "G",     # Ctrl+Shift+<this> toggles click-through
    "show_sparklines": True,
    "sparkline_points": 90,
    "show_title": True,
    "bar_width": 12,
    "gpu": "all",                    # "all" or a GPU index like 0
    "ollama_url": "http://localhost:11434",
    "ollama_poll_ms": 2000,
    "fit_context_reserve_gb": 1.5,   # VRAM held back for KV cache / context
    "max_processes": 4,
    "fps_backend": "auto",           # auto | presentmon | nvenc | off
    "presentmon_path": "",           # blank = auto-discover
    "presentmon_args": [],           # extra args for the stream command
    "fps_window_s": 1.5,             # averaging window for the frame rate
    "keep_on_top": True,
    "keep_on_top_ms": 3000,
    "proc_filter": ["ollama", "python", "llama", "koboldcpp", "lm-studio",
                    "lmstudio", "vllm", "comfyui", "textgen", "exllama",
                    "stable-diffusion", "jan", "msty"],
    "metrics": [
        "gpu_name",
        "util",
        "mem_util",
        "vram",
        "vram_free",
        "temp",
        "power",
        "clocks",
        "throttle",
        "fps",
        "ollama",
        "ollama_fit",
    ],
    "warn": {
        "temp_warn": 78, "temp_crit": 86,
        "vram_pct_warn": 85, "vram_pct_crit": 95,
        "power_pct_warn": 90, "power_pct_crit": 98,
        "util_warn": 101, "util_crit": 101,
    },
    "colors": {
        "bg": "#0b0f14",
        "fg": "#d7dee8",
        "label": "#7f8ea3",
        "good": "#6fcf7f",
        "warn": "#f2c14e",
        "crit": "#ef5f5f",
        "info": "#64b5ff",
        "dim": "#3c4757",
    },
    "custom": [
        {
            "label": "Example (disabled)",
            "type": "command",
            "cmd": ["cmd", "/c", "echo 42"],
            "regex": "([0-9.]+)",
            "unit": "",
            "interval_ms": 5000,
            "enabled": False,
        }
    ],
}


def deep_merge(base: dict, over: dict) -> dict:
    """Merge `over` onto a copy of `base`, recursing into plain dicts."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        # utf-8-sig: Notepad and PowerShell's Set-Content write a BOM, which
        # plain utf-8 decoding rejects - that silently reverted the whole config.
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
            return deep_merge(DEFAULT_CONFIG, json.load(handle))
    except Exception:
        # A hand-edited config with a syntax error must not brick the overlay.
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------------------
# nvidia-smi plumbing
# ---------------------------------------------------------------------------

CANDIDATE_FIELDS = [
    "index", "name", "driver_version",
    "memory.total", "memory.used", "memory.free", "memory.reserved",
    "utilization.gpu", "utilization.memory",
    "temperature.gpu", "temperature.memory",
    "power.draw", "power.limit",
    "fan.speed", "clocks.sm", "clocks.mem", "pstate",
    "clocks_event_reasons.active", "clocks_throttle_reasons.active",
    "pcie.link.gen.current", "pcie.link.width.current",
    "encoder.stats.averageFps",
]

# Bit -> human label, per the NVML clock-event-reason mask.
THROTTLE_BITS = [
    (0x0000000000000001, "idle"),
    (0x0000000000000002, "app clocks"),
    (0x0000000000000004, "sw power cap"),
    (0x0000000000000008, "hw slowdown"),
    (0x0000000000000010, "sync boost"),
    (0x0000000000000020, "sw thermal"),
    (0x0000000000000040, "hw thermal"),
    (0x0000000000000080, "power brake"),
    (0x0000000000000100, "display clocks"),
]

NA_TOKENS = {"n/a", "[n/a]", "[not supported]", "not supported", "[unknown error]", ""}


def run(cmd: list[str], timeout: float = 4.0) -> str | None:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def as_float(raw: str | None):
    if raw is None:
        return None
    token = raw.strip()
    if token.lower() in NA_TOKENS:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    token = raw.strip()
    return None if token.lower() in NA_TOKENS else token


class NvidiaSmi:
    """Thin nvidia-smi wrapper that probes once for the fields this driver has."""

    def __init__(self) -> None:
        self.fields: list[str] = []
        self.available = False
        self.error = "probing"
        self.probe()

    def probe(self) -> None:
        if run(["nvidia-smi", "--version"], timeout=6.0) is None:
            self.available = False
            self.error = "nvidia-smi not found or not responding"
            return
        supported = []
        for field in CANDIDATE_FIELDS:
            out = run(
                ["nvidia-smi", f"--query-gpu={field}",
                 "--format=csv,noheader,nounits"], timeout=6.0)
            if out is not None:
                supported.append(field)
        # Newer drivers renamed throttle -> event; keep whichever answered first.
        if "clocks_event_reasons.active" in supported:
            supported = [f for f in supported if f != "clocks_throttle_reasons.active"]
        self.fields = supported
        self.available = bool(supported)
        self.error = "" if self.available else "no queryable GPU fields"

    def sample(self, extra_fields: list[str]) -> list[dict]:
        fields = list(self.fields)
        for field in extra_fields:
            if field not in fields:
                fields.append(field)
        out = run(["nvidia-smi", f"--query-gpu={','.join(fields)}",
                   "--format=csv,noheader,nounits"], timeout=5.0)
        if out is None:
            return []
        gpus = []
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            values = [v.strip() for v in line.split(",")]
            if len(values) != len(fields):
                continue
            gpus.append(dict(zip(fields, values)))
        return gpus

    def compute_apps(self) -> list[dict]:
        out = run(["nvidia-smi",
                   "--query-compute-apps=pid,process_name,used_memory",
                   "--format=csv,noheader,nounits"], timeout=5.0)
        if out is None:
            return []
        apps = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            apps.append({
                "pid": parts[0],
                "name": os.path.basename(parts[1]) if clean(parts[1]) else parts[1],
                "mem_mb": as_float(parts[2]),
            })
        return apps


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def fetch_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None


def parse_expiry(text: str | None) -> float | None:
    """Return seconds until an Ollama expires_at timestamp, or None."""
    if not text:
        return None
    # Ollama emits nanosecond precision; datetime only takes microseconds.
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", text)
    trimmed = trimmed.replace("Z", "+00:00")
    try:
        from datetime import datetime, timezone
        when = datetime.fromisoformat(trimmed)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (when - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


def ollama_models(base_url: str) -> list[dict] | None:
    payload = fetch_json(base_url.rstrip("/") + "/api/ps")
    if payload is None:
        return None
    models = []
    for entry in payload.get("models", []) or []:
        details = entry.get("details") or {}
        total = entry.get("size") or 0
        in_vram = entry.get("size_vram") or 0
        models.append({
            "name": entry.get("name") or entry.get("model") or "?",
            "size_gb": total / 1e9,
            "vram_gb": in_vram / 1e9,
            "gpu_pct": (in_vram / total * 100.0) if total else 0.0,
            "params": details.get("parameter_size"),
            "quant": details.get("quantization_level"),
            "context": entry.get("context_length"),
            "ttl_s": parse_expiry(entry.get("expires_at")),
        })
    return models


# ---------------------------------------------------------------------------
# Frame rate
# ---------------------------------------------------------------------------

PRESENTMON_GLOBS = [
    os.path.join(HERE, "presentmon", "PresentMon*.exe"),
    os.path.join(HERE, "PresentMon*.exe"),
    r"C:\Program Files\NVIDIA Corporation\FrameViewSDK\bin\PresentMon*.exe",
]


def find_presentmon(configured: str) -> str | None:
    import glob
    import shutil
    if configured and os.path.exists(configured):
        return configured
    for name in ("PresentMon.exe", "PresentMon-x64.exe", "PresentMon_x64.exe"):
        found = shutil.which(name)
        if found:
            return found
    for pattern in PRESENTMON_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def process_name(pid: int) -> str | None:
    """Executable name for a PID, for processes this account can open."""
    if not IS_WINDOWS:
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_uint32(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return None


def foreground_pid() -> int | None:
    if not IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_uint32(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64)]


class _JOB_BASIC_LIMITS(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOB_EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOB_BASIC_LIMITS),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def kill_child_with_parent(proc) -> object | None:
    """Put a child process in a job that dies with us.

    Without this, killing the overlay by any means other than its own Quit
    leaves PresentMon running forever - and a startup app gets killed that way
    routinely. The job handle must stay referenced for the lifetime of the
    parent, so it is returned and held.
    """
    if not IS_WINDOWS:
        return None
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _JOB_EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(limits), ctypes.sizeof(limits))
        kernel32.AssignProcessToJobObject(job, int(proc._handle))
        return job
    except Exception:
        return None


class FpsTracker:
    """Streams present events from PresentMon and derives a per-process FPS.

    PresentMon is the only way to observe real frame presentation on Windows;
    nvidia-smi cannot see it (its one FPS field is the NVENC encoder). The
    binary is optional - without a working one this reports that no source is
    available rather than inventing a number.

    Frame rate comes from each row's own MsBetweenPresents rather than from
    line arrival times, because PresentMon block-buffers stdout when it is
    redirected; arrival times would arrive in bursts and read as noise.
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.proc = None
        self.thread = None
        self.lock = threading.Lock()
        self.frames: dict[int, deque] = {}      # pid -> deque[(arrival, ms)]
        self.names: dict[int, str] = {}
        self.status = "not started"
        self.binary: str | None = None
        self._job = None
        self.stop_event = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.proc is not None:
            return
        self.binary = find_presentmon(self.cfg.get("presentmon_path", ""))
        if not self.binary:
            self.status = "no PresentMon"
            return
        # Session name is per-process: two overlays sharing one name would
        # each stop the other's ETW session on startup.
        cmd = [self.binary, "--output_stdout", "--stop_existing_session",
               "--session_name", f"gpu_overlay_{os.getpid()}"]
        cmd += list(self.cfg.get("presentmon_args", []))
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, creationflags=NO_WINDOW)
        except Exception as exc:
            self.status = ("launch failed: " + str(exc))[:40]
            return
        self._job = kill_child_with_parent(self.proc)
        self.status = "starting"
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None

    # -- stdout parsing ----------------------------------------------------

    def _read(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        cols: dict[str, int] = {}
        pid_col = app_col = ms_col = -1
        rows = 0
        for line in proc.stdout:
            if self.stop_event.is_set():
                break
            parts = line.rstrip("\n").split(",")
            if pid_col < 0:
                # Column names and order differ between PresentMon 1.x and 2.x,
                # so resolve them by name rather than by position.
                cols = {name.strip().lower(): i for i, name in enumerate(parts)}
                if "processid" not in cols:
                    continue
                pid_col = cols["processid"]
                app_col = cols.get("application", -1)
                for key in ("msbetweenpresents", "msbetweendisplaychange"):
                    if key in cols:
                        ms_col = cols[key]
                        break
                if ms_col < 0:
                    self.status = "unrecognised CSV"
                    return
                continue
            try:
                pid = int(parts[pid_col])
                ms = float(parts[ms_col])
            except (ValueError, IndexError):
                continue
            if ms <= 0.0 or ms > 10000.0:
                continue
            with self.lock:
                self.frames.setdefault(pid, deque(maxlen=600)).append(
                    (time.time(), ms))
                if pid not in self.names:
                    app = parts[app_col] if 0 <= app_col < len(parts) else ""
                    app = app.strip()
                    # Without elevation PresentMon cannot name processes owned
                    # by another account; resolve those ourselves from the PID.
                    if not app or app.startswith("<"):
                        app = process_name(pid) or f"pid {pid}"
                    self.names[pid] = app
            rows += 1
            if rows == 1:
                self.status = "ok"
        if self.status in ("starting", "not started"):
            # Exited without producing a row: usually a build that needs a
            # driver or service (NVIDIA's FrameView copy) or needs elevation.
            self.status = "PresentMon gave no data"
        self.proc = None

    # -- query -------------------------------------------------------------

    def sample(self) -> dict | None:
        if self.status != "ok":
            return None
        window = float(self.cfg.get("fps_window_s", 1.5))
        cutoff = time.time() - window
        target = foreground_pid()
        best = None
        with self.lock:
            for pid, series in list(self.frames.items()):
                recent = [ms for stamp, ms in series if stamp >= cutoff]
                if len(recent) < 3:
                    if series and series[-1][0] < cutoff - 30:
                        del self.frames[pid]     # long gone, stop tracking
                    continue
                mean_ms = sum(recent) / len(recent)
                if mean_ms <= 0:
                    continue
                entry = {"pid": pid, "app": self.names.get(pid, "?"),
                         "fps": 1000.0 / mean_ms, "frames": len(recent)}
                if pid == target:
                    return entry
                # Prefer a process we could name: the busiest presenter is
                # often the compositor, which this account cannot open.
                if best is None:
                    best = entry
                else:
                    named = not entry["app"].startswith("pid ")
                    best_named = not best["app"].startswith("pid ")
                    if (named, entry["frames"]) > (best_named, best["frames"]):
                        best = entry
        return best


# ---------------------------------------------------------------------------
# System memory (matters the moment a model spills off the GPU)
# ---------------------------------------------------------------------------

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def system_ram() -> tuple[float, float] | None:
    """(used_gb, total_gb) of physical RAM."""
    if not IS_WINDOWS:
        return None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    total = status.ullTotalPhys / 1e9
    return (total - status.ullAvailPhys / 1e9, total)


# ---------------------------------------------------------------------------
# Sampler thread
# ---------------------------------------------------------------------------

class Sampler(threading.Thread):
    """Polls every data source off the UI thread and publishes one snapshot."""

    def __init__(self, cfg: dict) -> None:
        super().__init__(daemon=True)
        self.cfg = cfg
        self.smi = NvidiaSmi()
        self.lock = threading.Lock()
        self.snapshot: dict = {"gpus": [], "ready": False, "error": self.smi.error}
        self.stop_event = threading.Event()
        self._ollama_at = 0.0
        self._ollama_cache: list[dict] | None = None
        self._custom_at: dict[str, float] = {}
        self._custom_cache: dict[str, str] = {}
        self._apps_at = 0.0
        self._apps_cache: list[dict] = []
        self.fps = FpsTracker(cfg)
        if cfg.get("fps_backend", "auto") in ("auto", "presentmon"):
            self.fps.start()

    def update_config(self, cfg: dict) -> None:
        self.cfg = cfg

    def stop(self) -> None:
        self.stop_event.set()
        self.fps.stop()

    def get(self) -> dict:
        with self.lock:
            return self.snapshot

    def _extra_fields(self) -> list[str]:
        return [c["field"] for c in self.cfg.get("custom", [])
                if c.get("enabled") and c.get("type") == "nvidia_smi" and c.get("field")]

    def _custom_values(self, now: float) -> dict[str, str]:
        for entry in self.cfg.get("custom", []):
            if not entry.get("enabled") or entry.get("type") == "nvidia_smi":
                continue
            label = entry.get("label", "custom")
            interval = entry.get("interval_ms", 5000) / 1000.0
            if now - self._custom_at.get(label, 0.0) < interval:
                continue
            self._custom_at[label] = now
            value = "-"
            try:
                if entry.get("type") == "command":
                    cmd = entry.get("cmd") or []
                    raw = run(cmd if isinstance(cmd, list) else [cmd],
                              timeout=entry.get("timeout_s", 8))
                    text = (raw or "").strip()
                    pattern = entry.get("regex")
                    if pattern and text:
                        match = re.search(pattern, text, re.S)
                        text = (match.group(1) if match and match.groups()
                                else (match.group(0) if match else ""))
                    value = " ".join(text.split())[:40] or "-"
                elif entry.get("type") == "http_json":
                    payload = fetch_json(entry.get("url", ""),
                                         timeout=entry.get("timeout_s", 3))
                    node = payload
                    for part in str(entry.get("json_path", "")).split("."):
                        if part == "":
                            continue
                        if isinstance(node, list):
                            node = node[int(part)]
                        elif isinstance(node, dict):
                            node = node.get(part)
                        else:
                            node = None
                        if node is None:
                            break
                    if isinstance(node, float):
                        node = round(node, 2)
                    value = "-" if node is None else str(node)[:40]
            except Exception:
                value = "err"
            self._custom_cache[label] = value
        return dict(self._custom_cache)

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.time()

            if not self.smi.available:
                self.smi.probe()

            gpus = self.smi.sample(self._extra_fields()) if self.smi.available else []

            if started - self._apps_at > 3.0:
                self._apps_at = started
                self._apps_cache = self.smi.compute_apps() if self.smi.available else []

            interval = max(self.cfg.get("ollama_poll_ms", 2000), 500) / 1000.0
            if started - self._ollama_at > interval:
                self._ollama_at = started
                self._ollama_cache = ollama_models(self.cfg.get("ollama_url", ""))

            snapshot = {
                "ready": True,
                "error": "" if self.smi.available else self.smi.error,
                "gpus": gpus,
                "apps": self._apps_cache,
                "ollama": self._ollama_cache,
                "ram": system_ram(),
                "custom": self._custom_values(started),
                "fps": self.fps.sample(),
                "fps_status": self.fps.status,
                "t": started,
            }
            with self.lock:
                self.snapshot = snapshot

            elapsed = time.time() - started
            self.stop_event.wait(max(0.15, self.cfg.get("refresh_ms", 1000) / 1000.0 - elapsed))


# ---------------------------------------------------------------------------
# Row model
# ---------------------------------------------------------------------------

class Row:
    __slots__ = ("key", "label", "value", "color", "kind", "series")

    def __init__(self, key, label, value, color="fg", kind="text", series=None):
        self.key = key
        self.label = label
        self.value = value
        self.color = color
        self.kind = kind
        self.series = series


def bar(fraction: float, width: int) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def grade(value, warn, crit) -> str:
    if value is None:
        return "dim"
    if value >= crit:
        return "crit"
    if value >= warn:
        return "warn"
    return "good"


# ---------------------------------------------------------------------------
# The overlay window
# ---------------------------------------------------------------------------

class Overlay(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.sampler = Sampler(self.cfg)
        self.sampler.start()

        self.history: dict[str, deque] = {}
        self.widgets: dict[str, tuple[tk.Widget, tk.Widget]] = {}
        self.canvases: dict[str, tk.Canvas] = {}
        self.signature: tuple = ()
        self._drag = None
        self._menu_open = False
        self._click_through_live = False
        self._ghost_applied = False
        self._chord_down = False
        self._hint_until = 0.0

        self.title(APP_NAME)
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self.container = tk.Frame(self, bd=0, highlightthickness=0)
        self.container.pack(fill="both", expand=True, padx=8, pady=6)

        self.apply_style()
        pos = self.cfg.get("position", {})
        self.geometry(f"+{int(pos.get('x', 60))}+{int(pos.get('y', 60))}")

        self.bind_all("<Button-1>", self.on_press, add="+")
        self.bind_all("<B1-Motion>", self.on_drag, add="+")
        self.bind_all("<ButtonRelease-1>", self.on_release, add="+")
        self.bind_all("<Button-3>", self.on_menu, add="+")
        self.bind_all("<Escape>", lambda _e: self.shutdown())

        self.after(120, self.tick)
        self.after(400, self.guard_click_through)
        self.after(200, self.assert_on_top)

    # -- appearance --------------------------------------------------------

    def color(self, name: str) -> str:
        return self.cfg["colors"].get(name, "#ffffff")

    @property
    def panel_bg(self) -> str:
        return CHROMA_KEY if self.cfg.get("style") == "ghost" else self.color("bg")

    def apply_style(self) -> None:
        self.font = tkfont.Font(family=self.cfg.get("font_family", "Consolas"),
                                size=int(self.cfg.get("font_size", 10)))
        self.font_bold = tkfont.Font(family=self.cfg.get("font_family", "Consolas"),
                                     size=int(self.cfg.get("font_size", 10)),
                                     weight="bold")
        background = self.panel_bg
        self.configure(bg=background)
        self.container.configure(bg=background)
        try:
            self.attributes("-alpha", float(self.cfg.get("opacity", 0.88)))
        except tk.TclError:
            pass
        # -transparentcolor rebuilds the layered surface, and clearing it leaves
        # Tk painting into a stale one. Only touch the attribute when the ghost
        # state actually changes, and force a redraw when it does.
        want_ghost = self.cfg.get("style") == "ghost"
        if want_ghost != self._ghost_applied:
            try:
                self.attributes("-transparentcolor", CHROMA_KEY if want_ghost else "")
                self._ghost_applied = want_ghost
                self.withdraw()
                self.update_idletasks()
                self.deiconify()
                self.attributes("-topmost", True)
            except tk.TclError:
                pass
        for label_widget, value_widget in self.widgets.values():
            label_widget.configure(bg=background, font=self.font)
            value_widget.configure(bg=background, font=self.font)
        for canvas in self.canvases.values():
            canvas.configure(bg=background)

    def hwnd(self) -> int:
        return ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()

    def set_click_through(self, enabled: bool) -> None:
        """Toggle WS_EX_TRANSPARENT so clicks fall through to what is beneath."""
        if not IS_WINDOWS or enabled == self._click_through_live:
            return
        GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x00000020
        user32 = ctypes.windll.user32
        handle = self.hwnd()
        style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
        style = (style | WS_EX_TRANSPARENT) if enabled else (style & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongW(handle, GWL_EXSTYLE, style)
        self._click_through_live = enabled
        try:                       # re-establish the layered alpha attributes
            self.attributes("-alpha", float(self.cfg.get("opacity", 0.88)))
        except tk.TclError:
            pass

    def assert_on_top(self) -> None:
        """Re-assert topmost. Setting it before the window is realised does not
        stick, and other apps grab the topmost band, dropping us behind them."""
        if self.cfg.get("keep_on_top", True):
            try:
                self.attributes("-topmost", True)
                self.lift()
            except tk.TclError:
                pass
        self.after(int(self.cfg.get("keep_on_top_ms", 3000)), self.assert_on_top)

    def guard_click_through(self) -> None:
        """Holding Ctrl+Shift temporarily hands the mouse back to the overlay."""
        if IS_WINDOWS:
            get_key = ctypes.windll.user32.GetAsyncKeyState
            ctrl = bool(get_key(0x11) & 0x8000)
            shift = bool(get_key(0x10) & 0x8000)
            chord = ctrl and shift and bool(get_key(self.hotkey_vk()) & 0x8000)
            # Edge-triggered, so holding the chord toggles exactly once. This is
            # the only way back if click-through is on and the panel is ignoring
            # the mouse - it must never depend on the window receiving input.
            if chord and not self._chord_down:
                self.set_and_save(["click_through"],
                                  not self.cfg.get("click_through", False))
                self.flash_hint()
            self._chord_down = chord
            # Holding Ctrl+Shift alone temporarily hands the mouse back.
            rescue = ctrl and shift
            self.set_click_through(self.cfg.get("click_through", False) and not rescue)
        self.after(250, self.guard_click_through)

    def hotkey_vk(self) -> int:
        """Virtual-key code of the toggle chord's letter (Ctrl+Shift+<letter>)."""
        letter = str(self.cfg.get("click_through_hotkey", "G"))[:1].upper() or "G"
        return ord(letter)

    def flash_hint(self) -> None:
        self._hint_until = time.time() + 6.0
        self.signature = ()

    # -- interaction -------------------------------------------------------

    def on_press(self, event) -> None:
        if self.cfg.get("locked", False):
            return
        self._drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def on_drag(self, event) -> None:
        if self._drag is None:
            return
        self.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def on_release(self, _event) -> None:
        if self._drag is None:
            return
        self._drag = None
        self.cfg["position"] = {"x": self.winfo_x(), "y": self.winfo_y()}
        self.persist()

    def persist(self) -> None:
        try:
            save_config(self.cfg)
        except Exception:
            pass

    def set_and_save(self, path: list[str], value) -> None:
        node = self.cfg
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
        self.persist()
        self.sampler.update_config(self.cfg)
        self.apply_style()
        self.signature = ()          # force a rebuild

    def toggle_metric(self, key: str) -> None:
        metrics = list(self.cfg.get("metrics", []))
        if key in metrics:
            metrics.remove(key)
        else:
            metrics.append(key)
            metrics = [m for m in METRIC_ORDER if m in metrics]
        self.set_and_save(["metrics"], metrics)

    def on_menu(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)

        metrics_menu = tk.Menu(menu, tearoff=0)
        active = set(self.cfg.get("metrics", []))
        for key in METRIC_ORDER:
            metrics_menu.add_checkbutton(
                label=METRIC_LABELS[key],
                variable=tk.BooleanVar(value=key in active),
                command=lambda k=key: self.toggle_metric(k),
            )
        menu.add_cascade(label="Metrics", menu=metrics_menu)

        opacity_menu = tk.Menu(menu, tearoff=0)
        for value in (0.35, 0.5, 0.65, 0.8, 0.88, 1.0):
            opacity_menu.add_command(
                label=f"{int(value * 100)}%",
                command=lambda v=value: self.set_and_save(["opacity"], v))
        menu.add_cascade(label="Opacity", menu=opacity_menu)

        rate_menu = tk.Menu(menu, tearoff=0)
        for ms in (250, 500, 1000, 2000, 5000):
            rate_menu.add_command(
                label=f"{ms} ms",
                command=lambda v=ms: self.set_and_save(["refresh_ms"], v))
        menu.add_cascade(label="Refresh", menu=rate_menu)

        size_menu = tk.Menu(menu, tearoff=0)
        for size in (8, 9, 10, 11, 12, 14, 16):
            size_menu.add_command(
                label=f"{size} pt",
                command=lambda v=size: self.set_and_save(["font_size"], v))
        menu.add_cascade(label="Font size", menu=size_menu)

        style_menu = tk.Menu(menu, tearoff=0)
        for name, value in (("Card (translucent panel)", "card"),
                            ("Ghost (text only)", "ghost")):
            style_menu.add_command(
                label=name, command=lambda v=value: self.set_and_save(["style"], v))
        menu.add_cascade(label="Style", menu=style_menu)

        menu.add_separator()
        menu.add_checkbutton(
            label="Sparklines",
            variable=tk.BooleanVar(value=self.cfg.get("show_sparklines", True)),
            command=lambda: self.set_and_save(
                ["show_sparklines"], not self.cfg.get("show_sparklines", True)))
        menu.add_checkbutton(
            label="Lock position",
            variable=tk.BooleanVar(value=self.cfg.get("locked", False)),
            command=lambda: self.set_and_save(
                ["locked"], not self.cfg.get("locked", False)))
        letter = str(self.cfg.get("click_through_hotkey", "G"))[:1].upper()
        menu.add_checkbutton(
            label=f"Click-through  (Ctrl+Shift+{letter} to toggle back)",
            variable=tk.BooleanVar(value=self.cfg.get("click_through", False)),
            command=self.toggle_click_through)
        menu.add_separator()
        menu.add_command(label="Edit config.json", command=self.open_config)
        menu.add_command(label="Reload config", command=self.reload_config)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.shutdown)

        self._menu_open = True
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            self._menu_open = False

    def toggle_click_through(self) -> None:
        self.set_and_save(["click_through"],
                          not self.cfg.get("click_through", False))
        self.flash_hint()

    def open_config(self) -> None:
        try:
            os.startfile(CONFIG_PATH)  # noqa: S606 - user-initiated, local file
        except Exception:
            pass

    def reload_config(self) -> None:
        self.cfg = load_config()
        self.sampler.update_config(self.cfg)
        self.apply_style()
        self.signature = ()

    def shutdown(self) -> None:
        self.cfg["position"] = {"x": self.winfo_x(), "y": self.winfo_y()}
        self.persist()
        self.sampler.stop()
        self.destroy()

    # -- row construction --------------------------------------------------

    def push_history(self, key: str, value) -> deque:
        points = int(self.cfg.get("sparkline_points", 90))
        series = self.history.setdefault(key, deque(maxlen=points))
        if series.maxlen != points:
            series = deque(series, maxlen=points)
            self.history[key] = series
        series.append(0.0 if value is None else float(value))
        return series

    def build_rows(self, snapshot: dict) -> list[Row]:
        rows: list[Row] = []
        warn = self.cfg["warn"]
        width = int(self.cfg.get("bar_width", 12))
        metrics = self.cfg.get("metrics", [])
        show_spark = self.cfg.get("show_sparklines", True)

        if time.time() < self._hint_until:
            letter = str(self.cfg.get("click_through_hotkey", "G"))[:1].upper()
            state = "ON" if self.cfg.get("click_through") else "OFF"
            rows.append(Row("hint", f"click-through {state}",
                            f"Ctrl+Shift+{letter}", "warn"))

        if snapshot.get("error"):
            rows.append(Row("err", "GPU", snapshot["error"], "crit"))
            return rows

        gpus = snapshot.get("gpus", [])
        want = self.cfg.get("gpu", "all")
        if want != "all":
            gpus = [g for g in gpus if g.get("index") == str(want)]
        if not gpus:
            rows.append(Row("err", "GPU", "no data", "warn"))
            return rows

        multi = len(gpus) > 1
        for gpu in gpus:
            idx = gpu.get("index", "0")
            tag = f"g{idx}:"

            def field(name):
                return gpu.get(name)

            throttle_raw = field("clocks_event_reasons.active") or \
                field("clocks_throttle_reasons.active")

            if "gpu_name" in metrics:
                name = clean(field("name")) or "GPU"
                name = name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
                suffix = f"  [{idx}]" if multi else ""
                driver = clean(field("driver_version"))
                rows.append(Row(tag + "name", name + suffix,
                                f"drv {driver}" if driver else "", "info"))

            if "util" in metrics:
                util = as_float(field("utilization.gpu"))
                series = self.push_history(tag + "util", util)
                rows.append(Row(tag + "util", "GPU",
                                f"{bar((util or 0) / 100, width)} {util:>3.0f}%"
                                if util is not None else "-",
                                grade(util, warn["util_warn"], warn["util_crit"])))
                if show_spark:
                    rows.append(Row(tag + "util_spark", "", "", "info", "spark", series))

            if "mem_util" in metrics:
                membw = as_float(field("utilization.memory"))
                rows.append(Row(tag + "membw", "VRAM bw",
                                f"{bar((membw or 0) / 100, width)} {membw:>3.0f}%"
                                if membw is not None else "-", "info"))

            total = as_float(field("memory.total"))
            used = as_float(field("memory.used"))
            free = as_float(field("memory.free"))
            pct = (used / total * 100.0) if (used is not None and total) else None

            if "vram" in metrics:
                series = self.push_history(tag + "vram", pct)
                text = (f"{bar((pct or 0) / 100, width)} "
                        f"{used / 1024:.1f}/{total / 1024:.0f}G"
                        if used is not None and total else "-")
                rows.append(Row(tag + "vram", "VRAM", text,
                                grade(pct, warn["vram_pct_warn"], warn["vram_pct_crit"])))
                if show_spark:
                    rows.append(Row(tag + "vram_spark", "", "", "good", "spark", series))

            if "vram_free" in metrics:
                rows.append(Row(
                    tag + "free", "Free",
                    f"{free / 1024:.2f} GB" if free is not None else "-",
                    "good" if (free or 0) > 2048 else "warn"))

            if "vram_reserved" in metrics:
                reserved = as_float(field("memory.reserved"))
                rows.append(Row(tag + "res", "Reserved",
                                f"{reserved:.0f} MB" if reserved is not None else "n/a",
                                "dim"))

            if "temp" in metrics:
                temp = as_float(field("temperature.gpu"))
                vram_temp = as_float(field("temperature.memory"))
                text = f"{temp:.0f} C" if temp is not None else "-"
                if vram_temp is not None:
                    text += f"   mem {vram_temp:.0f} C"
                rows.append(Row(tag + "temp", "Temp", text,
                                grade(temp, warn["temp_warn"], warn["temp_crit"])))

            if "power" in metrics:
                draw = as_float(field("power.draw"))
                limit = as_float(field("power.limit"))
                ratio = (draw / limit * 100.0) if (draw is not None and limit) else None
                text = (f"{draw:.0f}/{limit:.0f} W" if draw is not None and limit
                        else "-")
                rows.append(Row(tag + "pwr", "Power", text,
                                grade(ratio, warn["power_pct_warn"],
                                      warn["power_pct_crit"])))

            if "fan" in metrics:
                fan = as_float(field("fan.speed"))
                rows.append(Row(tag + "fan", "Fan",
                                f"{fan:.0f}%" if fan is not None else "n/a", "dim"))

            if "clocks" in metrics:
                sm = as_float(field("clocks.sm"))
                mem = as_float(field("clocks.mem"))
                pstate = clean(field("pstate")) or ""
                text = (f"{sm:.0f}/{mem:.0f} MHz  {pstate}"
                        if sm is not None and mem is not None else pstate or "-")
                rows.append(Row(tag + "clk", "Clocks", text, "dim"))

            if "pcie" in metrics:
                gen = clean(field("pcie.link.gen.current"))
                lanes = clean(field("pcie.link.width.current"))
                rows.append(Row(tag + "pcie", "PCIe",
                                f"gen{gen} x{lanes}" if gen and lanes else "n/a", "dim"))

            if "throttle" in metrics:
                mask = 0
                if throttle_raw:
                    try:
                        mask = int(throttle_raw, 16)
                    except ValueError:
                        mask = 0
                # "idle" alone is the healthy desktop state, not a throttle.
                reasons = [name for bit, name in THROTTLE_BITS if mask & bit]
                capping = [r for r in reasons if r not in ("idle", "app clocks")]
                if capping:
                    rows.append(Row(tag + "thr", "Throttle", ", ".join(capping), "crit"))
                else:
                    rows.append(Row(tag + "thr", "Throttle",
                                    "idle" if "idle" in reasons else "none", "dim"))

            if "fps" in metrics:
                backend = self.cfg.get("fps_backend", "auto")
                frame = snapshot.get("fps") if backend != "nvenc" else None
                if frame:
                    app = os.path.splitext(frame["app"])[0][:14]
                    rows.append(Row(tag + "fps", "FPS",
                                    f"{frame['fps']:.0f}  {app}", "good"))
                else:
                    # Fall back to the NVENC encoder rate, which is real but
                    # only non-zero while something is actually encoding.
                    enc = as_float(field("encoder.stats.averageFps"))
                    if enc:
                        rows.append(Row(tag + "fps", "FPS",
                                        f"{enc:.0f}  encoder", "info"))
                    elif backend == "off":
                        pass
                    else:
                        note = snapshot.get("fps_status") or "no source"
                        rows.append(Row(tag + "fps", "FPS", note[:24], "dim"))

            if "ollama_fit" in metrics and free is not None:
                headroom = max(0.0, free / 1024.0
                               - float(self.cfg.get("fit_context_reserve_gb", 1.5)))
                rows.append(Row(
                    tag + "fit", "Fits ~",
                    f"{headroom / 0.60:.0f}B q4  {headroom / 1.06:.0f}B q8  "
                    f"{headroom / 2.0:.0f}B f16",
                    "info"))

        # ---- host-level rows (not per GPU) ----
        if "ram" in metrics:
            ram = snapshot.get("ram")
            if ram:
                used_gb, total_gb = ram
                pct = used_gb / total_gb * 100 if total_gb else 0
                rows.append(Row("ram", "Sys RAM",
                                f"{bar(pct / 100, width)} {used_gb:.0f}/{total_gb:.0f}G",
                                grade(pct, 85, 95)))

        if "ollama" in metrics:
            models = snapshot.get("ollama")
            if models is None:
                rows.append(Row("ollama", "Ollama", "offline", "dim"))
            elif not models:
                rows.append(Row("ollama", "Ollama", "no model loaded", "dim"))
            else:
                for i, model in enumerate(models):
                    name = model["name"]
                    if len(name) > 22:
                        name = name[:21] + "…"
                    detail = f"{model['size_gb']:.1f}G"
                    if model.get("quant"):
                        detail += f" {model['quant']}"
                    if model.get("context"):
                        detail += f" ctx{int(model['context']) // 1024}k"
                    rows.append(Row(f"ol{i}", name, detail, "info"))

                    split = model["gpu_pct"]
                    if split >= 99.5:
                        placement, tone = "100% GPU", "good"
                    else:
                        placement = (f"{split:.0f}% GPU / "
                                     f"{model['size_gb'] - model['vram_gb']:.1f}G CPU")
                        tone = "crit" if split < 90 else "warn"
                    ttl = model.get("ttl_s")
                    if ttl is not None and ttl > 0:
                        placement += f"   ttl {int(ttl // 60)}m"
                    rows.append(Row(f"ol{i}p", "  └", placement, tone))

        if "procs" in metrics:
            apps = snapshot.get("apps", [])
            known = tuple(self.cfg.get("proc_filter", DEFAULT_CONFIG["proc_filter"]))
            ranked = sorted(apps, key=lambda a: -(a.get("mem_mb") or 0))
            # Consumer GPUs under WDDM report per-process VRAM as [N/A], so a
            # ranked dump would just be every compositing desktop app. Only fall
            # back to the full list when the driver gives us real numbers.
            named = [a for a in ranked if any(k in a["name"].lower() for k in known)]
            has_mem = any(a.get("mem_mb") is not None for a in ranked)
            interesting = named or (ranked if has_mem else [])
            if not interesting:
                rows.append(Row("p_none", "GPU procs", "no compute apps", "dim"))
            for i, app in enumerate(interesting[:int(self.cfg.get("max_processes", 4))]):
                mem = app.get("mem_mb")
                name = app["name"]
                if name.startswith("["):          # [Insufficient Permissions]
                    name = f"pid {app['pid']}"
                rows.append(Row(f"p{i}", name[:20],
                                f"{mem:.0f} MB" if mem is not None else "on GPU",
                                "dim"))

        for entry in self.cfg.get("custom", []):
            if not entry.get("enabled"):
                continue
            label = entry.get("label", "custom")
            unit = entry.get("unit", "")
            if entry.get("type") == "nvidia_smi":
                field_name = entry.get("field", "")
                value = clean(gpus[0].get(field_name)) if gpus else None
                rows.append(Row("c:" + label, label,
                                f"{value}{unit}" if value else "n/a", "info"))
            else:
                value = snapshot.get("custom", {}).get(label)
                rows.append(Row("c:" + label, label,
                                f"{value}{unit}" if value else "…", "info"))

        return rows

    # -- rendering ---------------------------------------------------------

    def render(self, rows: list[Row]) -> None:
        signature = tuple((r.key, r.kind) for r in rows)
        background = self.panel_bg
        if signature != self.signature:
            for child in self.container.winfo_children():
                child.destroy()
            self.widgets.clear()
            self.canvases.clear()
            for index, row in enumerate(rows):
                if row.kind == "spark":
                    canvas = tk.Canvas(self.container, height=16, width=170,
                                       bg=background, highlightthickness=0, bd=0)
                    canvas.grid(row=index, column=0, columnspan=2,
                                sticky="we", pady=(0, 3))
                    self.canvases[row.key] = canvas
                    continue
                label = tk.Label(self.container, text=row.label, anchor="w",
                                 bg=background, fg=self.color("label"), font=self.font)
                value = tk.Label(self.container, text=row.value, anchor="e",
                                 bg=background, fg=self.color(row.color),
                                 font=self.font)
                label.grid(row=index, column=0, sticky="w", padx=(0, 10))
                value.grid(row=index, column=1, sticky="e")
                self.widgets[row.key] = (label, value)
            self.container.grid_columnconfigure(1, weight=1)
            self.signature = signature

        for row in rows:
            if row.kind == "spark":
                self.draw_spark(row)
                continue
            pair = self.widgets.get(row.key)
            if not pair:
                continue
            label, value = pair
            label.configure(text=row.label, fg=self.color("label"))
            value.configure(text=row.value, fg=self.color(row.color))

    def draw_spark(self, row: Row) -> None:
        canvas = self.canvases.get(row.key)
        if canvas is None or not row.series:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 60)
        height = max(canvas.winfo_height(), 12)
        points = list(row.series)
        if len(points) < 2:
            return
        peak = max(100.0, max(points))
        step = width / (len(points) - 1)
        coords = []
        for i, value in enumerate(points):
            coords.append(i * step)
            coords.append(height - (value / peak) * (height - 2) - 1)
        canvas.create_line(*coords, fill=self.color(row.color), width=1, smooth=False)
        canvas.create_line(0, height - 1, width, height - 1,
                           fill=self.color("dim"), width=1)

    def tick(self) -> None:
        try:
            snapshot = self.sampler.get()
            if snapshot.get("ready"):
                self.render(self.build_rows(snapshot))
            else:
                self.render([Row("boot", "GPU Overlay", "starting…", "dim")])
        except Exception as exc:                      # never let the HUD die
            try:
                self.render([Row("boot", "error", str(exc)[:40], "crit")])
            except Exception:
                pass
        self.after(max(120, int(self.cfg.get("refresh_ms", 1000))), self.tick)


METRIC_ORDER = [
    "gpu_name", "util", "mem_util", "vram", "vram_free", "vram_reserved",
    "temp", "power", "fan", "clocks", "pcie", "throttle", "fps",
    "ollama", "ollama_fit", "procs", "ram",
]

METRIC_LABELS = {
    "gpu_name": "GPU name + driver",
    "util": "GPU utilisation",
    "mem_util": "VRAM bandwidth (decode bottleneck)",
    "vram": "VRAM used / total",
    "vram_free": "VRAM free (headroom)",
    "vram_reserved": "VRAM reserved by driver",
    "temp": "Temperature",
    "power": "Power draw / limit",
    "fan": "Fan speed",
    "clocks": "Clocks + P-state",
    "pcie": "PCIe link",
    "throttle": "Throttle reasons",
    "fps": "Frame rate (needs PresentMon)",
    "ollama": "Ollama loaded models",
    "ollama_fit": "Model size that fits free VRAM",
    "procs": "GPU processes",
    "ram": "System RAM",
}


def main() -> None:
    app = Overlay()
    app.protocol("WM_DELETE_WINDOW", app.shutdown)
    app.mainloop()


if __name__ == "__main__":
    main()
