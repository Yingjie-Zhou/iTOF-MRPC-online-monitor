#!/usr/bin/env python3
import argparse
import html
import json
import os
import shlex
import shutil
import signal
import socketserver
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen


ONLINE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(ONLINE_DIR, "..", ".."))


def find_root_setup():
    configured = os.environ.get("ROOT_SETUP", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if shutil.which("root"):
        return ""
    candidates = []
    rootsys = os.environ.get("ROOTSYS", "").strip()
    if rootsys:
        candidates.append(os.path.join(rootsys, "bin", "thisroot.sh"))
    candidates.extend([
        os.path.expanduser("~/Software/root/bin/thisroot.sh"),
        "/opt/root/bin/thisroot.sh",
        "/usr/local/root/bin/thisroot.sh",
    ])
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


ROOT_SETUP = find_root_setup()
DEFAULT_INPUT_ROOT = os.path.join(PROJECT_DIR, "itof/data")
DEFAULT_INPUT = DEFAULT_INPUT_ROOT
DEFAULT_CALIB = os.path.join(PROJECT_DIR, "itof/macro/newchip/Calib_iTOF.root")
DEFAULT_MAP = os.path.join(PROJECT_DIR, "itof/reco/map/iTOFMap_newchip2511.csv")
ROOT_PORT = 8090
LOG_DIR = os.path.join(PROJECT_DIR, "itof/online/logs")
LOG_FILE = os.path.join(LOG_DIR, "runtimedisplay_root_monitor.log")
SETTINGS_FILE = os.path.join(LOG_DIR, "web_settings.json")


def open_log_file():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        return open(LOG_FILE, "a")
    except Exception:
        fallback = "/tmp/runtimedisplay_root_monitor_%s.log" % os.getuid()
        return open(fallback, "a")


def relocate_project_path(value):
    if not isinstance(value, str) or not os.path.isabs(value) or os.path.exists(value):
        return value
    marker = os.sep + "itof" + os.sep
    marker_at = value.find(marker)
    if marker_at < 0:
        return value
    candidate = os.path.join(PROJECT_DIR, value[marker_at + 1:])
    return candidate if os.path.exists(candidate) else value


def load_web_settings():
    try:
        with open(SETTINGS_FILE, "r") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        for key in ("input_root", "input_subdir", "input_path", "map_file", "calib_file"):
            if key in data:
                data[key] = relocate_project_path(data[key])
        return data
    except Exception:
        return {}


def save_web_settings(data):
    if not isinstance(data, dict):
        return False
    allowed = {
        "activeTab",
        "input_root",
        "input_subdir",
        "input_path",
        "map_file",
        "calib_mode",
        "calib_file",
        "track_algo",
        "auto_latest_subdir",
        "selectionState",
        "eventShowN",
        "eventPaused",
    }
    clean = load_web_settings()
    clean.update({key: data[key] for key in allowed if key in data})
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(clean, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except Exception:
        return False


def saved_start_settings():
    settings = load_web_settings()
    input_path = settings.get("input_path") or settings.get("input_subdir") or DEFAULT_INPUT
    return {
        "input_path": input_path,
        "calib_mode": settings.get("calib_mode", "self"),
        "calib_file": settings.get("calib_file", DEFAULT_CALIB),
        "map_file": settings.get("map_file", DEFAULT_MAP),
        "track_algo": settings.get("track_algo", "Fast"),
    }


def available_maps():
    map_dir = os.path.join(PROJECT_DIR, "itof/reco/map")
    out = []
    try:
        names = sorted(os.listdir(map_dir))
    except Exception:
        return out
    for name in names:
        if name.endswith(".csv"):
            out.append(os.path.join(map_dir, name))
    return out


def resolve_map(path):
    if not path:
        return DEFAULT_MAP
    if os.path.isabs(path):
        return path
    project_relative = os.path.join(PROJECT_DIR, path)
    if os.path.exists(project_relative):
        return project_relative
    return os.path.join(PROJECT_DIR, "itof/reco/map", path)


def elemap_info(path):
    path = resolve_map(path or STATE.map_file)
    detectors = set()
    strips = set()
    sides = set()
    fees = set()
    channels = set()
    channels_by_fee = {}
    sides_by_detector = {}
    rows = 0
    try:
        with open(path, "r") as handle:
            for line_no, line in enumerate(handle, 1):
                if line_no < 4:
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 5:
                    continue
                try:
                    det, strip, side, fee, channel = [int(parts[i]) for i in range(5)]
                except Exception:
                    continue
                rows += 1
                detectors.add(det)
                strips.add(strip)
                sides.add(side)
                fees.add(fee)
                channels.add(channel)
                channels_by_fee.setdefault(fee, set()).add(channel)
                sides_by_detector.setdefault(det, set()).add(side)
    except Exception as exc:
        return {
            "map_file": path,
            "rows": 0,
            "detectors": [],
            "strips": [],
            "sides": [],
            "fees": [],
            "channels": [],
            "channels_by_fee": {},
            "sides_by_detector": {},
            "error": str(exc),
        }
    return {
        "map_file": path,
        "rows": rows,
        "detectors": sorted(detectors),
        "strips": sorted(strips),
        "sides": sorted(sides),
        "fees": sorted(fees),
        "channels": sorted(channels),
        "channels_by_fee": {str(k): sorted(v) for k, v in channels_by_fee.items()},
        "sides_by_detector": {str(k): sorted(v) for k, v in sides_by_detector.items()},
    }


def input_subdirs(root):
    root = root or DEFAULT_INPUT_ROOT
    root = os.path.abspath(os.path.expanduser(root))
    out = []
    try:
        names = sorted(os.listdir(root))
    except Exception as exc:
        return {"root": root, "subdirs": [], "error": str(exc)}
    for name in names:
        path = os.path.join(root, name)
        if os.path.isdir(path):
            out.append(path)
    return {"root": root, "subdirs": out}


def trigger_count():
    url = "http://127.0.0.1:%d/iTOF/DigiMultiplicity/root.json" % ROOT_PORT
    try:
        data = json.loads(urlopen(url, timeout=2).read().decode("utf-8"))
        return {"count": int(data.get("fEntries", 0))}
    except Exception as exc:
        return {"count": None, "error": str(exc)}


def channel_warnings():
    url = "http://127.0.0.1:%d/iTOF/Warnings/ChannelHealth/root.json" % ROOT_PORT
    try:
        data = json.loads(urlopen(url, timeout=2).read().decode("utf-8"))
        text = data.get("fTitle") or data.get("fText") or ""
        if not text:
            return {"level": "waiting", "warnings": [], "error": "empty warning status"}
        parsed = json.loads(text)
        parsed.setdefault("warnings", [])
        return parsed
    except Exception as exc:
        return {"level": "waiting", "warnings": [], "error": str(exc)}


def hpos_entries():
    out = {}
    info = elemap_info(STATE.map_file)
    detectors = info.get("detectors") or [0, 1, 2, 3]
    for det in detectors:
        name = "hPos_%d" % det
        url = "http://127.0.0.1:%d/iTOF/Position/Detectors/%s/root.json" % (ROOT_PORT, name)
        try:
            data = json.loads(urlopen(url, timeout=2).read().decode("utf-8"))
            out[name] = {
                "entries": data.get("fEntries", 0),
                "title": data.get("fTitle", ""),
            }
        except Exception as exc:
            out[name] = {"entries": None, "error": str(exc)}
    return out


class MonitorState:
    def __init__(self):
        self.proc = None
        self.input_path = DEFAULT_INPUT
        self.calib_mode = "self"
        self.calib_file = DEFAULT_CALIB
        self.map_file = DEFAULT_MAP
        self.track_algo = "Fast"
        self.started_at = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.running():
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        self.proc = None
        self.started_at = None

    def stop_existing_root_servers(self):
        subprocess.call(["bash", "-lc", "pkill -f 'root .*iTOFRootOnlineMonitor' 2>/dev/null || true"])
        subprocess.call(["bash", "-lc", "pkill -f 'root.exe .*iTOFRootOnlineMonitor' 2>/dev/null || true"])

    def start(self, input_path, calib_mode, calib_file, map_file, track_algo="Fast"):
        self.stop()
        self.stop_existing_root_servers()
        self.input_path = input_path or DEFAULT_INPUT
        self.calib_mode = calib_mode if calib_mode in ("self", "file") else "self"
        self.calib_file = calib_file or DEFAULT_CALIB
        self.map_file = resolve_map(map_file)
        self.track_algo = track_algo if track_algo in ("Fast", "RANSAC") else "Fast"
        root_calib_arg = self.calib_file if self.calib_mode == "file" else ""

        root_setup = "source %s" % shlex.quote(ROOT_SETUP) if ROOT_SETUP else ":"
        command = """
set +u
{root_setup}
set -u
command -v root >/dev/null 2>&1 || {{ echo "ROOT executable not found. Set ROOT_SETUP or initialize ROOT first."; exit 127; }}
export VMCWORKDIR="{project}"
export ITOF_ONLINE_MAP="{map_file}"
export ITOF_ONLINE_CLSB="{project}/itof/macro/newchip/CLSB.txt"
export ITOF_ONLINE_TRACK_ALGO="{track_algo}"
cd "{project}"
rm -f itof/online/iTOFRootOnlineMonitor_C.d \
      itof/online/iTOFRootOnlineMonitor_C.so \
      itof/online/iTOFRootOnlineMonitor_C_ACLiC_dict_rdict.pcm
exec root -l -b -q 'itof/online/iTOFRootOnlineMonitor.C+("{input}",{port},-1,1000,true,"{calib}")'
""".format(
            root_setup=root_setup,
            project=PROJECT_DIR,
            map_file=self.map_file.replace('"', '\\"'),
            track_algo=self.track_algo,
            input=self.input_path.replace('"', '\\"'),
            port=ROOT_PORT,
            calib=root_calib_arg.replace('"', '\\"'),
        )
        log = open_log_file()
        log.write("\n=== start %s mode=%s track=%s map=%s input=%s ===\n" %
                  (time.ctime(), self.calib_mode, self.track_algo, self.map_file, self.input_path))
        log.flush()
        self.proc = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_DIR,
            preexec_fn=os.setsid,
        )
        self.started_at = time.time()


STATE = MonitorState()


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>iTOF Online Demo</title>
  <style>
    :root {
      color: #1f2937;
      background: #f5f7fb;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 14px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f5f7fb;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      min-height: 44px;
      padding: 0 16px;
      color: #1f2937;
      background: #ffffff;
      border-bottom: 1px solid #e5eaf2;
    }
    header strong {
      position: relative;
      padding-left: 16px;
      font-size: 16px;
      letter-spacing: .01em;
    }
    header strong::before {
      content: "";
      position: absolute;
      left: 0;
      top: 4px;
      width: 4px;
      height: 18px;
      border-radius: 999px;
      background: #5b8cff;
    }
    .subtitle { margin-left: 10px; color: #94a3b8; font-weight: 600; }
    .top-meta { display: flex; align-items: center; gap: 14px; color: #94a3b8; font: 12px Consolas, "Cascadia Mono", monospace; }
    .live-dot { color: #10b981; font-weight: 700; }
    .live-dot.offline { color: #94a3b8; }
    .live-dot::before {
      content: "";
      display: inline-block;
      width: 7px;
      height: 7px;
      margin-right: 6px;
      border-radius: 50%;
      background: #10b981;
      box-shadow: 0 0 0 3px rgba(16, 185, 129, .12);
      vertical-align: 1px;
    }
    .live-dot.offline::before {
      background: #94a3b8;
      box-shadow: 0 0 0 3px rgba(148, 163, 184, .12);
    }
    main { padding: 0 12px 16px; max-width: none; margin: 0; }
    .toolbar {
      display: grid;
      grid-template-columns: 300px 180px 108px 190px 104px 124px minmax(220px, 300px) 1fr 72px 72px 96px;
      gap: 8px;
      align-items: center;
      min-height: 48px;
      padding: 8px 4px;
      background: #f7f9fc;
      border-bottom: 1px solid #e4e9f1;
      overflow-x: auto;
    }
    .toolbar-spacer { min-width: 24px; }
    label { position: relative; display: grid; gap: 4px; font-size: 10px; font-weight: 800; color: #94a3b8; text-transform: uppercase; letter-spacing: .08em; }
    .check-label {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      height: 32px;
      margin-top: 14px;
      padding: 0 9px;
      border: 1px solid #dce3ed;
      border-radius: 4px;
      background: #ffffff;
      color: #64748b;
      white-space: nowrap;
      text-transform: none;
      letter-spacing: 0;
      font-size: 12px;
    }
    .check-label input { width: 15px; height: 15px; padding: 0; accent-color: #0f766e; }
    input, select, button {
      height: 34px;
      min-width: 0;
      border: 1px solid #dce3ed;
      border-radius: 4px;
      padding: 0 10px;
      font-size: 13px;
      color: #1f2937;
      background: #ffffff;
      outline: none;
      text-transform: none;
      letter-spacing: 0;
    }
    input:focus, select:focus { border-color: #0ea5e9; box-shadow: 0 0 0 3px rgba(14, 165, 233, .16); }
    button {
      cursor: pointer;
      color: #5b6b84;
      background: #ffffff;
      border-color: #e0e6ef;
      font-weight: 700;
      box-shadow: none;
    }
    button:hover { filter: brightness(1.04); }
    .toolbar button:not(.stop):not(.secondary),
    .event-controls button:not(.stop):not(.secondary) { background: #eef4ff; border-color: #dbe7ff; color: #5b8cff; }
    button.stop { background: #fff5f6; border-color: #ff8fa3; color: #ef476f; }
    button.secondary { background: #ffffff; border-color: #e0e6ef; color: #64748b; box-shadow: none; }
    button:disabled {
      cursor: not-allowed;
      color: #b8c4d6 !important;
      background: #f5f7fb !important;
      border-color: #e6ebf2 !important;
      filter: none;
    }
    .action-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
    }
    .action-icon {
      display: inline-block;
      flex: 0 0 auto;
    }
    .icon-start {
      width: 0;
      height: 0;
      border-top: 5px solid transparent;
      border-bottom: 5px solid transparent;
      border-left: 8px solid currentColor;
    }
    .icon-stop {
      width: 9px;
      height: 9px;
      border-radius: 2px;
      background: currentColor;
    }
    .icon-refresh {
      width: 12px;
      height: 12px;
      border: 2px solid currentColor;
      border-left-color: transparent;
      border-radius: 50%;
      position: relative;
    }
    .icon-refresh::after {
      content: "";
      position: absolute;
      right: -3px;
      top: -3px;
      width: 0;
      height: 0;
      border-left: 5px solid currentColor;
      border-top: 3px solid transparent;
      border-bottom: 3px solid transparent;
      transform: rotate(38deg);
    }
    .path-label:focus-within {
      z-index: 20;
    }
    .path-label:focus-within input {
      width: min(760px, calc(100vw - 32px));
      box-shadow: 0 0 0 3px rgba(91, 140, 255, .14);
    }
    .monitor-strip {
      display: flex;
      align-items: stretch;
      height: 50px;
      margin: 0 -12px;
      padding: 0 12px;
      background: #ffffff;
      border-bottom: 1px solid #e4e9f1;
      overflow: visible;
    }
    #triggerStatus { display: flex; flex: 0 0 auto; flex-wrap: nowrap; justify-content: flex-end; align-items: stretch; min-height: 50px; overflow: visible; position: relative; z-index: 30; }
    #triggerStatus .status-item {
      position: relative;
      min-width: 132px;
      max-width: none;
      border-right: 0;
      border-left: 1px solid #edf1f6;
      overflow: visible;
    }
    .trigger-trend {
      position: absolute;
      right: 8px;
      top: calc(100% + 8px);
      z-index: 200;
      display: none;
      width: 320px;
      height: 190px;
      padding: 12px;
      color: #26364d;
      background: rgba(255, 255, 255, .97);
      border: 1px solid #dbe5f0;
      border-radius: 6px;
      box-shadow: 0 16px 40px rgba(15, 23, 42, .16);
    }
    #triggerStatus .status-item:hover .trigger-trend,
    #triggerStatus .status-item:focus-within .trigger-trend { display: block; }
    .trigger-trend-title {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      margin-bottom: 8px;
      font-size: 12px;
      font-weight: 800;
      color: #172033;
    }
    .trigger-trend-title span {
      font: 10px Consolas, "Cascadia Mono", monospace;
      color: #94a3b8;
      font-weight: 500;
    }
    .trigger-trend svg {
      width: 100%;
      height: 138px;
      display: block;
    }
    .trigger-trend text {
      font: 10px Consolas, "Cascadia Mono", monospace;
      fill: #8ca0bb;
    }
    .trigger-trend .grid-line { stroke: #e7edf5; stroke-width: 1; }
    .trigger-trend .axis-line { stroke: #cbd7e6; stroke-width: 1; }
    .trigger-trend .trend-fill { fill: rgba(91, 140, 255, .12); }
    .trigger-trend .trend-line { fill: none; stroke: #5b8cff; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
    .trigger-trend-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      height: 138px;
      color: #94a3b8;
      font: 11px Consolas, "Cascadia Mono", monospace;
      text-transform: uppercase;
    }
    .warning-card {
      display: flex;
      align-items: center;
      gap: 16px;
      min-height: 46px;
      margin: 10px 0 0;
      padding: 10px 14px;
      color: #475569;
      background: rgba(255, 255, 255, .86);
      border: 1px solid #dbe5f0;
      border-left: 4px solid #94a3b8;
      border-radius: 8px;
      box-shadow: 0 8px 22px rgba(30, 41, 59, .05);
    }
    .warning-card.ok { border-left-color: #10b981; background: rgba(240, 253, 250, .8); color: #0f766e; }
    .warning-card.waiting { border-left-color: #94a3b8; }
    .warning-card.warning {
      border-color: #fecaca;
      border-left-color: #ef4444;
      background: linear-gradient(90deg, rgba(254, 242, 242, .96), rgba(255, 255, 255, .9));
      color: #991b1b;
      box-shadow: 0 0 0 1px rgba(239, 68, 68, .16), 0 12px 28px rgba(153, 27, 27, .10);
    }
    .warning-head { flex: 0 0 auto; min-width: 145px; }
    .warning-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 800;
      color: #0f172a;
    }
    .warning-card.warning .warning-title { color: #991b1b; }
    .warning-dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: currentColor;
      box-shadow: 0 0 0 5px rgba(148, 163, 184, .16);
    }
    .warning-card.warning .warning-dot {
      background: #ef4444;
      box-shadow: 0 0 0 5px rgba(239, 68, 68, .18), 0 0 18px rgba(239, 68, 68, .55);
    }
    .warning-subtitle { margin-top: 2px; font-size: 11px; color: #64748b; }
    .warning-card.warning .warning-subtitle { color: #b91c1c; }
    .warning-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      min-width: 0;
    }
    .warning-item {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      max-width: 100%;
      padding: 4px 8px;
      border: 1px solid #e2e8f0;
      border-radius: 999px;
      background: rgba(255, 255, 255, .72);
      color: #334155;
      font: 11px Consolas, "Cascadia Mono", monospace;
    }
    .warning-card.warning .warning-item {
      border-color: #fecaca;
      background: rgba(255, 255, 255, .84);
      color: #991b1b;
    }
    .warning-empty { color: #64748b; font-size: 12px; }
    #status {
      display: flex;
      flex: 1 1 auto;
      align-items: stretch;
      gap: 0;
      margin: 0;
      padding: 0;
      background: transparent;
      border: 0;
      border-radius: 0;
      box-shadow: none;
    }
    .status-item {
      flex: 0 1 auto;
      min-width: max-content;
      max-width: 220px;
      padding: 8px 12px;
      background: transparent;
      border: 0;
      border-right: 1px solid #edf1f6;
      border-radius: 0;
    }
    .status-label {
      margin-bottom: 4px;
      color: #94a3b8;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .status-value {
      overflow: hidden;
      color: #17202e;
      font-family: Consolas, "Cascadia Mono", monospace;
      font-size: 12px;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .status-value.ok { color: #047857; }
    .status-value.stop { color: #b91c1c; }
    .status-value.ok::before,
    .status-value.stop::before {
      content: "";
      display: inline-block;
      width: 7px;
      height: 7px;
      margin-right: 6px;
      border-radius: 50%;
      vertical-align: 1px;
    }
    .status-value.ok::before { background: #10b981; box-shadow: 0 0 0 3px rgba(16, 185, 129, .12); }
    .status-value.stop::before { background: #ef4444; box-shadow: 0 0 0 3px rgba(239, 68, 68, .12); }
    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 0 -12px 12px;
      padding: 0 20px;
      height: 40px;
      align-items: end;
      background: #ffffff;
      border-bottom: 1px solid #e4e9f1;
      border-radius: 0;
    }
    .tabs .tab {
      height: 40px;
      color: #7b8ba3;
      background: transparent !important;
      border: 0;
      border-radius: 0;
      box-shadow: none;
      padding: 0 10px;
      border-bottom: 2px solid transparent;
      font-weight: 700;
    }
    .tabs .tab.active { background: transparent !important; color: #5b8cff; border-bottom-color: #5b8cff; }
    .selectors { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; margin: 8px 0 12px; }
    .selectors label { min-width: 120px; }
    .event-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
    .event-controls button { min-width: 82px; }
    .event-controls input { width: 72px; }
    .event-info { min-height: 34px; display: inline-flex; align-items: center; padding: 0 4px; font-size: 13px; color: #334155; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(360px, 1fr)); gap: 12px; padding: 0 0 12px; }
    .grid.dense { grid-template-columns: repeat(3, minmax(320px, 1fr)); }
    .view { display: none; }
    .view.active { display: grid; }
    .panel {
      background: #ffffff;
      border: 1px solid #e5eaf2;
      border-radius: 6px;
      overflow: hidden;
      min-height: 340px;
      box-shadow: 0 1px 3px rgba(15, 23, 42, .035);
    }
    .panel.wide { grid-column: 1 / -1; }
    .panel h2 {
      margin: 0;
      padding: 12px 14px;
      font-size: 14px;
      color: #172033;
      background: #ffffff;
      border-bottom: 1px solid #eef2f7;
    }
    .plot { position: relative; width: 100%; height: 310px; background: #ffffff; padding: 4px 8px 8px; }
    .plot.tall { height: 650px; }
    .plot-canvas { position: absolute; inset: 0; }
    .plot svg text,
    .plot .jsroot text {
      font-family: Inter, "Segoe UI", "Helvetica Neue", Arial, sans-serif !important;
      letter-spacing: 0 !important;
      font-weight: 500;
    }
    .plot-loading {
      position: absolute;
      inset: 0;
      z-index: 12;
      display: none;
      align-items: center;
      justify-content: center;
      pointer-events: none;
      background: linear-gradient(180deg, rgba(255,255,255,.88), rgba(246,249,253,.82));
      backdrop-filter: blur(1px);
    }
    .plot-loading.active { display: flex; }
    .plot-loading-box {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border: 1px solid #dbe7f6;
      border-radius: 999px;
      background: rgba(255,255,255,.92);
      color: #5b6f91;
      font: 11px Consolas, "Cascadia Mono", monospace;
      letter-spacing: .08em;
      text-transform: uppercase;
      box-shadow: 0 10px 24px rgba(91, 140, 255, .12);
    }
    .plot-spinner {
      width: 16px;
      height: 16px;
      border: 2px solid #dbeafe;
      border-top-color: #5b8cff;
      border-radius: 50%;
      animation: plotSpin .8s linear infinite;
    }
    @keyframes plotSpin { to { transform: rotate(360deg); } }
    .hit-html-overlay { position: absolute; inset: 0; z-index: 20; pointer-events: none; overflow: hidden; }
    .hit-html-track { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
    .hit-html-track-line { stroke: #00ff66; stroke-width: 1.33; stroke-linecap: round; filter: drop-shadow(0 0 2px #000) drop-shadow(0 0 4px #00ff66); }
    .hit-html-dot { position: absolute; width: 7px; height: 7px; border-radius: 50%; background: #ff0000; border: 1px solid #ffff00; box-shadow: 0 0 0 1px #000, 0 0 6px #ff0000; transform: translate(-50%, -50%); }
    .plot-msg { position: absolute; right: 12px; top: -27px; padding: 0; background: transparent; font: 10px Consolas, "Cascadia Mono", monospace; color: #94a3b8; z-index: 2; border: 0; text-transform: uppercase; }
    @media (max-width: 1100px) {
      .toolbar, .grid, .grid.dense { grid-template-columns: 1fr; }
      .monitor-strip { flex-direction: column; }
      #status { flex-wrap: wrap; }
      .monitor-strip { height: auto; }
      #triggerStatus { justify-content: flex-start; min-height: 32px; padding-bottom: 8px; overflow: visible; }
      #triggerStatus .status-item { border-left: 0; border-right: 1px solid #edf1f6; }
    }
    @media (max-width: 640px) { #status { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <div><strong>iTOF</strong><span class="subtitle">/ online display</span></div>
    <div class="top-meta"><span>online monitor</span><span id="liveState" class="live-dot offline">OFFLINE</span></div>
  </header>
  <main>
    <section class="toolbar">
      <label class="path-label">Data path
        <input id="inputRoot" value="@DEFAULT_INPUT_ROOT@">
      </label>
      <label>Subfolder
        <select id="inputSubdir"></select>
      </label>
      <label class="check-label">
        <input id="autoLatestSubdir" type="checkbox">
        Auto latest
      </label>
      <label>Elemap
        <select id="mapFile"></select>
      </label>
      <label>Calib mode
        <select id="calibMode"><option value="self">self</option><option value="file">file</option></select>
      </label>
      <label>Track algo
        <select id="trackAlgo"><option value="Fast">Fast</option><option value="RANSAC">RANSAC</option></select>
      </label>
      <label class="path-label">Calib file
        <input id="calibFile" value="@DEFAULT_CALIB@">
      </label>
      <div class="toolbar-spacer"></div>
      <button id="startBtn" class="action-btn" onclick="startMonitor()"><span class="action-icon icon-start"></span><span>Start</span></button>
      <button id="stopBtn" class="stop action-btn" onclick="stopMonitor()"><span class="action-icon icon-stop"></span><span>Stop</span></button>
      <button class="secondary action-btn" onclick="refreshActive()"><span class="action-icon icon-refresh"></span><span>Refresh plots</span></button>
    </section>
    <div class="monitor-strip">
      <section id="status"></section>
      <div id="triggerStatus">
        <div class="status-item"><div class="status-label">Trigger count</div><div class="status-value">-</div></div>
      </div>
    </div>
    <section id="warningCard" class="warning-card waiting">
      <div class="warning-head">
        <div class="warning-title"><span class="warning-dot"></span><span>Channel health</span></div>
        <div class="warning-subtitle">waiting for strip statistics</div>
      </div>
      <div class="warning-list"><span class="warning-empty">No detector has reached the 100-hit activation threshold.</span></div>
    </section>
    <nav class="tabs" id="tabs"></nav>
    <section class="selectors" id="selectors"></section>
    <section id="plotGrid"></section>
  </main>
<script>
const rootBase = window.location.protocol + "//" + window.location.hostname + ":8090";
let jsrootPromise = null;
let geoPainterPromise = null;
let threePromise = null;
let activeTab = "overview";
const drawInFlight = {};
const drawRetryCount = {};
const objectCache = {};
const overlayPainters = {};
const selectionState = {fee: 0, channel: 0, det: 0, side: 0, geomObject: "MapTOPiTOF"};
const eventDisplayLayerSpacingCm = 17;
let appReady = false;
let settingsHydrated = false;
let serverSavedSettings = {};
let refreshSequence = 0;
let drawGeneration = 0;
let mapInfo = defaultMapInfo();
let mapInfoSignature = "";
let mapViewRevision = 0;
const eventState = {paused: false, cache: {}, cursor: 0, showN: 1, capacity: 200, first: 0, latest: 0, count: 0};
let autoLatestInFlight = false;
const settingsKey = "itofOnlineDemoSettingsV1";
const triggerHistory = [];
const triggerHistoryMaxPoints = 240;
const triggerHistoryMaxAgeMs = 10 * 60 * 1000;
const tabs = [
  {id: "overview", label: "Overview"},
  {id: "hpos", label: "Hitmap"},
  {id: "geometry", label: "Geometry"},
  {id: "fee", label: "FEE"},
  {id: "channel", label: "Channel"},
  {id: "detector", label: "Detector"},
  {id: "timing", label: "Timing"},
  {id: "event", label: "Event"}
];
function rootPath(path) { return "/api/root_object?path=" + encodeURIComponent(path) + "&ts=" + Date.now(); }
function safeId(path, prefix) { return prefix + path.replace(/[^A-Za-z0-9_]/g, "_"); }
function plotId(path) { return safeId(path, "plot_"); }
function msgId(path) { return safeId(path, "msg_"); }
function specKey(spec) { return spec.id || spec.path; }
function showPlotMessage(path, text) { const msg = document.getElementById(msgId(path)); if (msg) msg.textContent = text; }
function setPlotLoading(path, loading, text) {
  const canvas = document.getElementById(plotId(path));
  const plot = canvas ? canvas.parentElement : null;
  const layer = plot ? plot.querySelector(".plot-loading") : null;
  if (!layer) return;
  const label = layer.querySelector(".plot-loading-text");
  if (label && text) label.textContent = text;
  const alreadyDrawn = canvas && canvas.dataset.drawn === "1";
  layer.classList.toggle("active", !!loading && !alreadyDrawn);
}
function isEventDisplaySpec(spec) { return spec && spec.id === "EventDisplay_Hits"; }
function plotStillCurrent(key, generation) {
  return generation === drawGeneration && !!document.getElementById(plotId(key));
}
function clearPlotCaches(dropViews) {
  drawGeneration += 1;
  Object.keys(drawInFlight).forEach(function(key) { delete drawInFlight[key]; });
  Object.keys(drawRetryCount).forEach(function(key) { delete drawRetryCount[key]; });
  Object.keys(objectCache).forEach(function(key) { delete objectCache[key]; });
  Object.keys(overlayPainters).forEach(function(key) { delete overlayPainters[key]; });
  if (dropViews) {
    const holder = document.getElementById("plotGrid");
    if (holder) holder.innerHTML = "";
  }
}
function seq(n) { return Array.from({length: n}, function(_, i) { return i; }); }
function defaultMapInfo() {
  return {
    detectors: [0, 1, 2, 3],
    strips: seq(32),
    sides: [0, 1],
    fees: seq(16),
    channels: seq(16),
    channels_by_fee: {},
    sides_by_detector: {}
  };
}
function numericList(values, fallback) {
  const out = (Array.isArray(values) ? values : []).map(function(v) { return parseInt(v, 10); }).filter(Number.isFinite);
  return out.length ? Array.from(new Set(out)).sort(function(a, b) { return a - b; }) : fallback.slice();
}
function channelsForFee(fee) {
  return numericList(mapInfo.channels_by_fee && mapInfo.channels_by_fee[String(fee)], mapInfo.channels || seq(16));
}
function sidesForDetector(det) {
  return numericList(mapInfo.sides_by_detector && mapInfo.sides_by_detector[String(det)], mapInfo.sides || [0, 1]);
}
function normalizeMapInfo(data) {
  const fallback = defaultMapInfo();
  const nextInfo = {
    map_file: data && data.map_file,
    rows: data && data.rows || 0,
    detectors: numericList(data && data.detectors, fallback.detectors),
    strips: numericList(data && data.strips, fallback.strips),
    sides: numericList(data && data.sides, fallback.sides),
    fees: numericList(data && data.fees, fallback.fees),
    channels: numericList(data && data.channels, fallback.channels),
    channels_by_fee: data && data.channels_by_fee || {},
    sides_by_detector: data && data.sides_by_detector || {},
    error: data && data.error
  };
  const nextSignature = JSON.stringify({
    map_file: nextInfo.map_file || "",
    detectors: nextInfo.detectors,
    fees: nextInfo.fees,
    channels: nextInfo.channels,
    channels_by_fee: nextInfo.channels_by_fee,
    sides_by_detector: nextInfo.sides_by_detector
  });
  if (nextSignature !== mapInfoSignature) {
    mapInfoSignature = nextSignature;
    mapViewRevision += 1;
    clearPlotCaches(true);
  }
  mapInfo = nextInfo;
  clampSelectionToMap();
}
function pickExisting(value, values) {
  return values.indexOf(value) >= 0 ? value : values[0];
}
function clampSelectionToMap() {
  selectionState.fee = pickExisting(selectionState.fee, mapInfo.fees);
  selectionState.channel = pickExisting(selectionState.channel, channelsForFee(selectionState.fee));
  selectionState.det = pickExisting(selectionState.det, mapInfo.detectors);
  selectionState.side = pickExisting(selectionState.side, sidesForDetector(selectionState.det));
}
function loadSavedSettings() {
  let local = {};
  try {
    const raw = window.localStorage ? window.localStorage.getItem(settingsKey) : null;
    local = raw ? JSON.parse(raw) : {};
  } catch (e) {
  }
  return Object.assign({}, local || {}, serverSavedSettings || {});
}
function currentSettingsPayload() {
  const old = loadSavedSettings();
  const mapSelect = document.getElementById("mapFile");
  const mapValue = mapSelect && mapSelect.options.length ? mapSelect.value : (old.map_file || "");
  return {
    activeTab: activeTab,
    input_root: document.getElementById("inputRoot")?.value || "",
    input_subdir: document.getElementById("inputSubdir")?.value || "",
    input_path: selectedInputPath(),
    map_file: mapValue,
    calib_mode: document.getElementById("calibMode")?.value || "self",
    calib_file: document.getElementById("calibFile")?.value || "",
    track_algo: document.getElementById("trackAlgo")?.value || "Fast",
    auto_latest_subdir: !!document.getElementById("autoLatestSubdir")?.checked,
    selectionState: Object.assign({}, selectionState),
    eventShowN: eventState.showN,
    eventPaused: eventState.paused
  };
}
function saveSettings() {
  if (!settingsHydrated) return;
  const data = currentSettingsPayload();
  serverSavedSettings = Object.assign({}, serverSavedSettings || {}, data);
  try {
    if (window.localStorage) window.localStorage.setItem(settingsKey, JSON.stringify(data));
  } catch (e) {
  }
  api("/api/settings", data).catch(function() {});
}
async function loadServerSettings() {
  try {
    const data = await api("/api/settings");
    serverSavedSettings = data && typeof data === "object" ? data : {};
  } catch (e) {
    serverSavedSettings = {};
  }
  return serverSavedSettings;
}
function pathParent(path) {
  const parts = String(path || "").replace(/\/+$/, "").split("/");
  if (parts.length <= 1) return "/";
  parts.pop();
  return parts.join("/") || "/";
}
function pathWithinRoot(path, root) {
  const cleanPath = String(path || "").replace(/\/+$/, "");
  const cleanRoot = String(root || "").replace(/\/+$/, "");
  return cleanPath === cleanRoot || cleanPath.indexOf(cleanRoot + "/") === 0;
}
function applyRunningStatusToControls(status) {
  if (!status || !status.running) return;
  const rootInput = document.getElementById("inputRoot");
  if (rootInput && status.input_path && !pathWithinRoot(status.input_path, rootInput.value)) {
    rootInput.value = pathParent(status.input_path);
  }
  if (status.calib_mode) document.getElementById("calibMode").value = status.calib_mode;
  if (status.calib_file) document.getElementById("calibFile").value = status.calib_file;
  if (status.track_algo) document.getElementById("trackAlgo").value = status.track_algo;
}
function applySavedSettings() {
  const data = loadSavedSettings();
  if (data.input_root) document.getElementById("inputRoot").value = data.input_root;
  else if (data.input_path) document.getElementById("inputRoot").value = data.input_path.replace(/\/[^/]+\/?$/, "");
  if (data.calib_mode) document.getElementById("calibMode").value = data.calib_mode;
  if (data.calib_file) document.getElementById("calibFile").value = data.calib_file;
  if (data.track_algo) document.getElementById("trackAlgo").value = data.track_algo;
  if (data.auto_latest_subdir) document.getElementById("autoLatestSubdir").checked = true;
  if (data.selectionState && typeof data.selectionState === "object") {
    if (Number.isFinite(data.selectionState.fee)) selectionState.fee = data.selectionState.fee;
    if (Number.isFinite(data.selectionState.channel)) selectionState.channel = data.selectionState.channel;
    if (Number.isFinite(data.selectionState.det)) selectionState.det = data.selectionState.det;
    if (Number.isFinite(data.selectionState.side)) selectionState.side = data.selectionState.side;
    if (typeof data.selectionState.geomObject === "string") selectionState.geomObject = data.selectionState.geomObject;
  }
  if (Number.isFinite(data.eventShowN)) eventState.showN = Math.max(1, Math.min(50, data.eventShowN));
  if (typeof data.eventPaused === "boolean") eventState.paused = data.eventPaused;
  if (data.activeTab && tabs.some(function(tab) { return tab.id === data.activeTab; })) activeTab = data.activeTab;
}
function attachSettingsPersistence() {
  ["inputRoot", "inputSubdir", "autoLatestSubdir", "mapFile", "calibMode", "calibFile", "trackAlgo"].forEach(function(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("change", saveSettings);
    if (el.tagName === "INPUT") el.addEventListener("input", saveSettings);
  });
  const root = document.getElementById("inputRoot");
  if (root) root.addEventListener("change", function() {
    const select = document.getElementById("inputSubdir");
    if (select) select.innerHTML = "";
    loadInputSubdirs(false);
  });
  const autoLatest = document.getElementById("autoLatestSubdir");
  if (autoLatest) autoLatest.addEventListener("change", function() {
    saveSettings();
    checkAutoLatestSubdir();
  });
  const mapSelect = document.getElementById("mapFile");
  if (mapSelect) mapSelect.addEventListener("change", function() {
    saveSettings();
    clearPlotCaches(true);
    loadMapInfo().then(function() {
      renderSelectors();
      renderActive();
    });
  });
}
function cloneRootObject(obj) {
  if (!obj) return obj;
  const seen = new WeakMap();
  function clone(value) {
    if (value === null || typeof value !== "object") return typeof value === "function" ? undefined : value;
    if (seen.has(value)) return seen.get(value);
    const out = Array.isArray(value) ? [] : {};
    seen.set(value, out);
    for (const key in value) {
      if (typeof value[key] === "function") continue;
      const copied = clone(value[key]);
      if (copied !== undefined || value[key] === undefined) out[key] = copied;
    }
    return out;
  }
  return clone(obj);
}
function applyGeoTransparency(obj, transparency) {
  const seen = new WeakSet();
  function visit(node) {
    if (!node || typeof node !== "object" || seen.has(node)) return;
    seen.add(node);
    if (typeof node._typename === "string" && node._typename.indexOf("TGeo") >= 0) {
      node.fTransparency = transparency;
    }
    if (typeof node.fTransparency === "number") node.fTransparency = transparency;
    if (node.fMaterial && typeof node.fMaterial === "object") node.fMaterial.fTransparency = transparency;
    if (node.fMedium && node.fMedium.fMaterial && typeof node.fMedium.fMaterial === "object") {
      node.fMedium.fMaterial.fTransparency = transparency;
    }
    for (const key in node) {
      const value = node[key];
      if (value && typeof value === "object") visit(value);
    }
  }
  visit(obj);
  return obj;
}
function applyPlotTheme(obj) {
  if (!obj || typeof obj !== "object") return obj;
  const applyAxisFont = function(axis) {
    if (!axis || typeof axis !== "object") return;
    axis.fTitleFont = 42;
    axis.fLabelFont = 42;
    if (typeof axis.fTitleSize === "number") axis.fTitleSize = Math.max(axis.fTitleSize, 0.04);
    if (typeof axis.fLabelSize === "number") axis.fLabelSize = Math.max(axis.fLabelSize, 0.032);
  };
  if (typeof obj.fTitleFont === "number") obj.fTitleFont = 42;
  if (typeof obj.fTextFont === "number") obj.fTextFont = 42;
  applyAxisFont(obj.fXaxis);
  applyAxisFont(obj.fYaxis);
  applyAxisFont(obj.fZaxis);
  if (obj.fFunctions && obj.fFunctions.arr) {
    for (const item of obj.fFunctions.arr) {
      if (!item || typeof item !== "object") continue;
      if (typeof item.fTextFont === "number") item.fTextFont = 42;
      if (typeof item.fLabelFont === "number") item.fLabelFont = 42;
    }
  }
  const type = obj._typename || "";
  if (/^TH1/.test(type) || (typeof obj.fNcells === "number" && obj.fArray && !/^TH2/.test(type) && !/^TH3/.test(type))) {
    obj.fTitle = "";
    obj.fLineColor = 860;
    obj.fLineWidth = 1;
    obj.fLineStyle = 1;
    obj.fFillStyle = 1001;
    obj.fFillColor = 851;
    obj.fMarkerColor = 860;
    obj.fMarkerStyle = 20;
    obj.fMarkerSize = 0.8;
  }
  if (/^TH2/.test(type)) {
    obj.fTitle = "";
    obj.fLineColor = 860;
    obj.fMarkerColor = 860;
    obj.fFillStyle = 1001;
  }
  return obj;
}
function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise(function(resolve, reject) {
      setTimeout(function() { reject(new Error(label + " timeout")); }, ms);
    })
  ]);
}
async function drawObject(jsroot, divId, obj, opt, cleanup) {
  if (cleanup) {
    if (jsroot.cleanup) jsroot.cleanup(divId);
    return await withTimeout(jsroot.draw(divId, obj, opt), 12000, "draw");
  }
  if (jsroot.redraw) return await withTimeout(jsroot.redraw(divId, obj, opt), 12000, "redraw");
  return await withTimeout(jsroot.draw(divId, obj, opt), 12000, "draw");
}
function promoteOverlayObjects(painter, rootObj) {
  if (!painter || !painter._toplevel || !painter._toplevel.traverse) return;
  painter._toplevel.traverse(function(node) {
    if (node.geo_object !== rootObj && !node.main_track) return;
    node.renderOrder = 10000000;
    if (!node.material) return;
    const materials = Array.isArray(node.material) ? node.material : [node.material];
    for (const mat of materials) {
      if (!mat) continue;
      mat.depthTest = false;
      mat.depthWrite = false;
      mat.transparent = false;
      mat.opacity = 1;
      mat.needsUpdate = true;
    }
  });
  if (painter.render3D) painter.render3D();
}
function loadJsroot() {
  if (window.JSROOT) return Promise.resolve(window.JSROOT);
  if (jsrootPromise) return jsrootPromise;
  jsrootPromise = new Promise(function(resolve, reject) {
    const script = document.createElement("script");
    script.async = true;
    script.src = rootBase + "/jsrootsys/scripts/JSRoot.core.js";
    script.onload = function() {
      setTimeout(function() {
        if (window.JSROOT) resolve(window.JSROOT);
        else {
          jsrootPromise = null;
          reject(new Error("JSROOT loaded without global object"));
        }
      }, 0);
    };
    script.onerror = function() { jsrootPromise = null; reject(new Error("failed to load JSROOT")); };
    document.head.appendChild(script);
  });
  return jsrootPromise;
}
function loadGeoPainter(jsroot) {
  if (geoPainterPromise) return geoPainterPromise;
  if (jsroot.require) {
    geoPainterPromise = jsroot.require(["geom"]).then(function() { return jsroot; });
    return geoPainterPromise;
  }
  geoPainterPromise = new Promise(function(resolve, reject) {
    const script = document.createElement("script");
    script.src = rootBase + "/jsrootsys/scripts/JSRoot.geom.js";
    script.onload = function() { resolve(jsroot); };
    script.onerror = function() { geoPainterPromise = null; reject(new Error("failed to load JSROOT geometry painter")); };
    document.head.appendChild(script);
  });
  return geoPainterPromise;
}
function loadThree(jsroot) {
  if (threePromise) return threePromise;
  if (jsroot.require) {
    threePromise = jsroot.require(["three"]).then(function(module) {
      return Array.isArray(module) ? module[0] : module;
    });
    return threePromise;
  }
  threePromise = Promise.resolve(window.THREE);
  return threePromise;
}
async function drawTopHitOverlay(jsroot, painter, hits) {
  if (!painter || !painter._toplevel) return;
  const THREE = await loadThree(jsroot);
  if (!THREE) return;

  if (painter.getExtrasContainer) painter.getExtrasContainer("delete", "hit_top_overlay");
  if (!hits || !hits.fP || !hits.fN) {
    if (painter.render3D) painter.render3D();
    return;
  }
  const group = painter.getExtrasContainer ? painter.getExtrasContainer("", "hit_top_overlay") : new THREE.Object3D();
  group.name = "hit_top_overlay";
  group.renderOrder = 20000000;

  const overall = painter.getOverallSize ? painter.getOverallSize() : 60;
  const radius = Math.max(0.4, overall * 0.00875);
  const haloRadius = radius * 1.65;
  const sphere = new THREE.SphereGeometry(radius, 24, 16);
  const halo = new THREE.SphereGeometry(haloRadius, 24, 16);
  const red = new THREE.MeshBasicMaterial({
    color: 0xff0000,
    depthTest: false,
    depthWrite: false,
    transparent: false
  });
  const yellow = new THREE.MeshBasicMaterial({
    color: 0xffff00,
    depthTest: false,
    depthWrite: false,
    transparent: false
  });

  for (let i = 0; i < hits.fN; ++i) {
    const x = hits.fP[i * 3], y = hits.fP[i * 3 + 1], z = hits.fP[i * 3 + 2];
    if (![x, y, z].every(Number.isFinite)) continue;

    const h = new THREE.Mesh(halo, yellow);
    h.position.set(x, y, z);
    h.renderOrder = 19999999;
    h.frustumCulled = false;
    group.add(h);

    const m = new THREE.Mesh(sphere, red);
    m.position.set(x, y, z);
    m.renderOrder = 20000000;
    m.frustumCulled = false;
    group.add(m);
  }

  if (!painter.getExtrasContainer) painter._toplevel.add(group);
  if (painter.render3D) painter.render3D();
}
function markerPointVector(THREE, obj, index) {
  const x = obj.fP[index * 3], y = obj.fP[index * 3 + 1], z = obj.fP[index * 3 + 2];
  if (![x, y, z].every(Number.isFinite)) return null;
  return new THREE.Vector3(x, y, z);
}
function detectorOrderFromZ(z) {
  return Number.isFinite(z) ? Math.round(z / eventDisplayLayerSpacingCm) : 0;
}
function interpolateTrackPointAtZ(THREE, points, z) {
  if (!points || points.length < 2) return null;
  const a = points[0], b = points[1];
  const dz = b.z - a.z;
  if (Math.abs(dz) <= 1e-9) return a.clone();
  const t = Math.max(0, Math.min(1, (z - a.z) / dz));
  return new THREE.Vector3(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + dz * t);
}
async function drawHtmlHitOverlay(jsroot, painter, hits, track, divId, snapshots, options) {
  if (!painter || !painter._camera) return;
  const THREE = await loadThree(jsroot);
  if (!THREE) return;
  options = options || {};

  const canvas = document.getElementById(divId);
  const plot = canvas ? canvas.parentElement : null;
  if (!plot) return;

  let overlay = plot.querySelector(".hit-html-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.className = "hit-html-overlay";
    plot.appendChild(overlay);
  }
  if (overlay._timer) clearInterval(overlay._timer);
  if (overlay._animationFrame) cancelAnimationFrame(overlay._animationFrame);
  overlay.innerHTML = "";

  const frames = snapshots && snapshots.length ? snapshots : [{hits: hits, track: track}];
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "hit-html-track");
  overlay.appendChild(svg);

  const trackItems = [];
  const dots = [];
  const detectorOrders = [];
  for (const frame of frames) {
    const frameTrack = frame ? frame.track : null;
    if (frameTrack && frameTrack.fP && frameTrack.fN >= 2) {
      const points = [];
      for (let i = 0; i < 2; ++i) {
        const point = markerPointVector(THREE, frameTrack, i);
        if (point) points.push(point);
      }
      if (points.length >= 2) {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("class", "hit-html-track-line");
        svg.appendChild(line);
        trackItems.push({line: line, points: points, zMin: null, zMax: null});
      }
    }
    const frameHits = frame ? frame.hits : null;
    const hitN = frameHits && frameHits.fP ? (frameHits.fN || 0) : 0;
    for (let i = 0; i < hitN; ++i) {
      const pos = markerPointVector(THREE, frameHits, i);
      if (!pos) continue;
      const detectorOrder = detectorOrderFromZ(pos.z);
      const dot = document.createElement("div");
      dot.className = "hit-html-dot";
      overlay.appendChild(dot);
      dots.push({dot: dot, pos: pos, detectorOrder: detectorOrder});
      detectorOrders.push(detectorOrder);
    }
  }
  const sortedOrders = Array.from(new Set(detectorOrders)).sort(function(a, b) { return a - b; });
  const zByOrder = {};
  for (const item of dots) zByOrder[item.detectorOrder] = Math.max(zByOrder[item.detectorOrder] || -Infinity, item.pos.z);
  const detectorZValues = sortedOrders.map(function(order) { return zByOrder[order]; }).filter(Number.isFinite);
  for (const item of trackItems) {
    item.zMin = detectorZValues.length ? Math.min.apply(null, detectorZValues) : Math.min(item.points[0].z, item.points[1].z);
    item.zMax = detectorZValues.length ? Math.max.apply(null, detectorZValues) : Math.max(item.points[0].z, item.points[1].z);
  }

  let animationProgress = options.animateDetectors && sortedOrders.length ? 0 : 1;
  const animationMs = options.durationMs || 500;
  const startAnimationAt = performance.now();

  const update = function() {
    if (!document.body.contains(overlay)) {
      if (overlay._timer) clearInterval(overlay._timer);
      return;
    }
    const rect = overlay.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    if (painter._toplevel && painter._toplevel.updateMatrixWorld) painter._toplevel.updateMatrixWorld(true);
    if (painter._camera.updateMatrixWorld) painter._camera.updateMatrixWorld(true);
    if (painter._camera.updateProjectionMatrix) painter._camera.updateProjectionMatrix();
    const projectPoint = function(pos) {
      const v = pos.clone();
      if (painter._toplevel && painter._toplevel.localToWorld) painter._toplevel.localToWorld(v);
      v.project(painter._camera);
      return {
        visible: Number.isFinite(v.x) && Number.isFinite(v.y) && v.z >= -1 && v.z <= 1,
        x: (v.x + 1) * 0.5 * rect.width,
        y: (1 - v.y) * 0.5 * rect.height
      };
    };

    let visibleZLimit = Infinity;
    let trackZLimit = Infinity;
    if (animationProgress < 1 && sortedOrders.length) {
      const scaled = animationProgress * sortedOrders.length;
      const index = Math.min(sortedOrders.length - 1, Math.floor(scaled));
      const frac = Math.max(0, Math.min(1, scaled - index));
      const currentZ = zByOrder[sortedOrders[index]];
      const prevZ = index > 0 ? zByOrder[sortedOrders[index - 1]] : currentZ;
      trackZLimit = Number.isFinite(currentZ) ? prevZ + (currentZ - prevZ) * frac : Infinity;
      visibleZLimit = trackZLimit + 1.e-6;
    }

    for (const item of trackItems) {
      let trackStart = item.points[0];
      let trackEnd = item.points[1];
      if (options.animateDetectors && sortedOrders.length) {
        trackStart = interpolateTrackPointAtZ(THREE, item.points, item.zMin) || item.points[0];
        const cappedZ = animationProgress >= 1 ? item.zMax : Math.max(item.zMin, Math.min(item.zMax, trackZLimit));
        trackEnd = interpolateTrackPointAtZ(THREE, item.points, cappedZ) || trackStart;
      }
      const a = projectPoint(trackStart);
      const b = projectPoint(trackEnd);
      const visible = a.visible && b.visible;
      item.line.style.display = visible ? "block" : "none";
      if (visible) {
        item.line.setAttribute("x1", a.x);
        item.line.setAttribute("y1", a.y);
        item.line.setAttribute("x2", b.x);
        item.line.setAttribute("y2", b.y);
      }
    }

    for (const item of dots) {
      const p = projectPoint(item.pos);
      const visible = p.visible && (!options.animateDetectors || animationProgress >= 1 || item.pos.z <= visibleZLimit);
      item.dot.style.display = visible ? "block" : "none";
      if (visible) {
        item.dot.style.left = p.x + "px";
        item.dot.style.top = p.y + "px";
      }
    }
  };
  const animate = function(now) {
    animationProgress = Math.max(0, Math.min(1, (now - startAnimationAt) / animationMs));
    update();
    if (animationProgress < 1 && document.body.contains(overlay)) {
      overlay._animationFrame = requestAnimationFrame(animate);
    } else {
      overlay._animationFrame = null;
    }
  };
  if (options.animateDetectors && sortedOrders.length) overlay._animationFrame = requestAnimationFrame(animate);
  else update();
  overlay._timer = setInterval(update, 80);
}
function hitCount(obj) {
  return obj && obj.fP ? (obj.fN || obj.fLastPoint || obj.fEntries || 0) : 0;
}
function trackOn(obj) {
  return !!(obj && obj.fP && obj.fN >= 2);
}
function parseKeyValues(text) {
  const out = {};
  String(text || "").split(/\s+/).forEach(function(part) {
    const idx = part.indexOf("=");
    if (idx <= 0) return;
    const key = part.slice(0, idx);
    const value = part.slice(idx + 1);
    const number = parseInt(value, 10);
    out[key] = Number.isFinite(number) ? number : value;
  });
  return out;
}
function markerEventNumber(obj) {
  const unique = obj && typeof obj.fUniqueID === "number" ? obj.fUniqueID : 0;
  if (unique) return unique;
  const meta = parseKeyValues(obj && obj.fTitle ? obj.fTitle : "");
  return typeof meta.event === "number" ? meta.event : 0;
}
function clampEventCursor() {
  if (!eventState.count) {
    eventState.cursor = 0;
    return;
  }
  if (!eventState.cursor) eventState.cursor = eventState.latest;
  eventState.cursor = Math.max(eventState.first, Math.min(eventState.cursor, eventState.latest));
}
function visibleEventSerials() {
  if (!eventState.count) return [];
  clampEventCursor();
  const showN = Math.max(1, Math.min(50, eventState.showN || 1));
  const start = Math.max(eventState.first, eventState.cursor - showN + 1);
  const serials = [];
  for (let serial = start; serial <= eventState.cursor; ++serial) serials.push(serial);
  return serials;
}
function renderEventInfo(label) {
  clampEventCursor();
  const shown = visibleEventSerials().length;
  const mode = eventState.paused ? "paused" : "live";
  const prefix = label ? label + ": " : "";
  return prefix + mode + ", event " + eventState.cursor + " / latest " + eventState.latest +
         ", first " + eventState.first + ", stored " + eventState.count + ", showing " + shown;
}
function syncEventControls() {
  const pauseBtn = document.getElementById("eventPauseBtn");
  if (pauseBtn) pauseBtn.textContent = eventState.paused ? "Start" : "Pause";
  const showN = document.getElementById("eventShowN");
  if (showN && String(showN.value) !== String(eventState.showN)) showN.value = eventState.showN;
  clampEventCursor();
  const prevBtn = document.getElementById("eventPrevBtn");
  const nextBtn = document.getElementById("eventNextBtn");
  if (prevBtn) prevBtn.disabled = !eventState.count || eventState.cursor <= eventState.first;
  if (nextBtn) nextBtn.disabled = !eventState.count || eventState.cursor >= eventState.latest;
  const info = document.getElementById("eventInfo");
  if (info) info.textContent = renderEventInfo("");
}
function resetEventHistory() {
  eventState.cache = {};
  eventState.cursor = -1;
  eventState.first = 0;
  eventState.latest = 0;
  eventState.count = 0;
  eventState.capacity = 200;
  syncEventControls();
}
async function refreshEventHistoryStatus(jsroot) {
  const status = await jsroot.httpRequest(rootPath("/iTOF/EventHistory/EventHistoryStatus"), "object");
  const meta = parseKeyValues((status && (status.fTitle || status.fText)) || "");
  if (typeof meta.capacity === "number") eventState.capacity = meta.capacity;
  if (typeof meta.latest === "number") eventState.latest = meta.latest;
  if (typeof meta.first === "number") eventState.first = meta.first;
  if (typeof meta.count === "number") eventState.count = meta.count;
  if (!eventState.paused && eventState.latest) eventState.cursor = eventState.latest;
  clampEventCursor();
  syncEventControls();
  return meta;
}
function historySlot(eventSerial) {
  return ((eventSerial - 1) % eventState.capacity + eventState.capacity) % eventState.capacity;
}
async function fetchEventSnapshot(jsroot, eventSerial) {
  if (!eventSerial || eventSerial < eventState.first || eventSerial > eventState.latest) return null;
  if (eventState.cache[eventSerial]) return eventState.cache[eventSerial];
  const slot = historySlot(eventSerial);
  const suffix = String(slot).padStart(3, "0");
  const hits = await jsroot.httpRequest(rootPath("/iTOF/EventHistory/EventHits_" + suffix), "object");
  const track = await jsroot.httpRequest(rootPath("/iTOF/EventHistory/EventTrack_" + suffix), "object");
  if (markerEventNumber(hits) !== eventSerial || markerEventNumber(track) !== eventSerial) return null;
  const snapshot = {serial: eventSerial, hits: cloneRootObject(hits), track: cloneRootObject(track)};
  eventState.cache[eventSerial] = snapshot;
  for (const key in eventState.cache) {
    const serial = parseInt(key, 10);
    if (serial < eventState.first || serial > eventState.latest) delete eventState.cache[key];
  }
  return snapshot;
}
async function loadVisibleEventSnapshots(jsroot) {
  const snapshots = [];
  for (const serial of visibleEventSerials()) {
    const snapshot = await fetchEventSnapshot(jsroot, serial);
    if (snapshot) snapshots.push(snapshot);
  }
  return snapshots;
}
async function renderCachedEventOverlay(label) {
  const spec = plotSpecs().find(isEventDisplaySpec);
  if (!spec) return;
  const key = specKey(spec);
  const painter = overlayPainters[key];
  const canvas = document.getElementById(plotId(key));
  if (!painter || !canvas || canvas.dataset.drawn !== "1") return;
  const jsroot = await loadJsroot();
  const snapshots = await loadVisibleEventSnapshots(jsroot);
  const latest = snapshots.length ? snapshots[snapshots.length - 1] : {hits: null, track: null};
  if (painter.getExtrasContainer) painter.getExtrasContainer("delete", "tracks");
  await drawTopHitOverlay(jsroot, painter, null);
  await drawHtmlHitOverlay(jsroot, painter, null, null, plotId(key), snapshots, {animateDetectors: true, durationMs: 200});
  const hits = snapshots.reduce(function(sum, item) { return sum + hitCount(item.hits); }, 0);
  showPlotMessage(key, renderEventInfo(label || "cached") + ", hits " + hits + ", track " + (trackOn(latest.track) ? "on" : "off"));
  syncEventControls();
}
function toggleEventRefresh() {
  eventState.paused = !eventState.paused;
  if (!eventState.paused && eventState.latest) eventState.cursor = eventState.latest;
  saveSettings();
  syncEventControls();
  refreshActive();
}
function eventPrev() {
  if (!eventState.count) return;
  eventState.paused = true;
  clampEventCursor();
  const step = Math.max(1, Math.min(50, eventState.showN || 1));
  eventState.cursor = Math.max(eventState.first, eventState.cursor - step);
  saveSettings();
  syncEventControls();
  renderCachedEventOverlay("cached");
}
function eventNext() {
  if (!eventState.count) return;
  eventState.paused = true;
  clampEventCursor();
  const step = Math.max(1, Math.min(50, eventState.showN || 1));
  eventState.cursor = Math.min(eventState.latest, eventState.cursor + step);
  saveSettings();
  syncEventControls();
  renderCachedEventOverlay("cached");
}
function updateEventShowN() {
  const input = document.getElementById("eventShowN");
  const value = parseInt(input ? input.value : eventState.showN, 10);
  eventState.showN = Math.max(1, Math.min(50, Number.isFinite(value) ? value : 1));
  saveSettings();
  syncEventControls();
  renderCachedEventOverlay("cached");
}
async function drawRootPlot(spec) {
  const key = specKey(spec);
  const generation = drawGeneration;
  if (drawInFlight[key]) return;
  drawInFlight[key] = true;
  setPlotLoading(key, true, "loading");
  try {
    const jsroot = await loadJsroot();
    if (!plotStillCurrent(key, generation)) return;
    if (spec.overlayPath) {
      await loadGeoPainter(jsroot);
      if (!plotStillCurrent(key, generation)) return;
      await drawOverlayPlot(jsroot, spec, generation);
      drawRetryCount[key] = 0;
      setPlotLoading(key, false);
      return;
    }
    const opt = spec.opt || "hist";
    if (opt === "ogl" || opt.indexOf("geo") >= 0) await loadGeoPainter(jsroot);
    const canvas = document.getElementById(plotId(key));
    if (!canvas) return;
    if (objectCache[spec.path] && canvas && canvas.dataset.drawn !== "1") {
      showPlotMessage(key, "cached: " + (objectCache[spec.path].fEntries || 0));
      let cachedObj = objectCache[spec.path];
      applyPlotTheme(cachedObj);
      let cleanupDraw = !!spec.forceDraw;
      if (typeof spec.transparency === "number") {
        cachedObj = cloneRootObject(cachedObj);
        applyGeoTransparency(cachedObj, spec.transparency);
        cleanupDraw = true;
      }
      if (!plotStillCurrent(key, generation)) return;
      await drawObject(jsroot, plotId(key), cachedObj, opt, cleanupDraw);
      if (!plotStillCurrent(key, generation)) return;
      canvas.dataset.drawn = "1";
      setPlotLoading(key, false);
    }
    if (spec.staticObject && objectCache[spec.path]) {
      drawRetryCount[key] = 0;
      setPlotLoading(key, false);
      return;
    }
    const obj = await jsroot.httpRequest(rootPath(spec.path), "object");
    if (!plotStillCurrent(key, generation)) return;
    objectCache[spec.path] = obj;
    showPlotMessage(key, "entries: " + (obj.fEntries || 0));
    let drawObj = obj;
    applyPlotTheme(drawObj);
    let cleanupDraw = !!spec.forceDraw;
    if (typeof spec.transparency === "number") {
      drawObj = cloneRootObject(drawObj);
      applyGeoTransparency(drawObj, spec.transparency);
      cleanupDraw = true;
    }
    await drawObject(jsroot, plotId(key), drawObj, opt, cleanupDraw);
    if (!plotStillCurrent(key, generation)) return;
    if (canvas) canvas.dataset.drawn = "1";
    drawRetryCount[key] = 0;
    setPlotLoading(key, false);
  } catch (err) {
    const detail = err && err.message ? err.message : String(err);
    if (objectCache[spec.path]) {
      showPlotMessage(key, "draw failed: " + detail);
      setPlotLoading(key, false);
    } else {
      showPlotMessage(key, "ROOT server is not ready: " + detail);
      setPlotLoading(key, true, "waiting for ROOT");
      const tries = (drawRetryCount[key] || 0) + 1;
      drawRetryCount[key] = tries;
      if (tries <= 5 && plotStillCurrent(key, generation)) {
        setTimeout(function() {
          if (appReady && plotStillCurrent(key, generation)) drawRootPlot(spec);
        }, 1200 * tries);
      } else {
        setPlotLoading(key, false);
      }
    }
  } finally {
    drawInFlight[key] = false;
    if (!plotStillCurrent(key, generation)) setPlotLoading(key, false);
  }
}
async function drawOverlayPlot(jsroot, spec, generation) {
  const key = specKey(spec);
  const divId = plotId(key);
  const canvas = document.getElementById(divId);
  const current = function() { return plotStillCurrent(key, generation); };
  if (!canvas || !current()) return;
  const updateHits = async function(painter, hitObj, trackObj, label) {
    if (!current()) return;
    const eventDisplay = isEventDisplaySpec(spec);
    if (eventDisplay) {
      await refreshEventHistoryStatus(jsroot);
    }
    const snapshots = eventDisplay ? await loadVisibleEventSnapshots(jsroot) : [{hits: cloneRootObject(hitObj), track: cloneRootObject(trackObj)}];
    if (!current()) return;
    const latest = snapshots.length ? snapshots[snapshots.length - 1] : {hits: cloneRootObject(hitObj), track: cloneRootObject(trackObj)};
    if (painter && painter.getExtrasContainer) painter.getExtrasContainer("delete", "tracks");
    if (eventDisplay) {
      await drawTopHitOverlay(jsroot, painter, null);
      await drawHtmlHitOverlay(jsroot, painter, latest.hits, latest.track, divId, snapshots, {animateDetectors: true, durationMs: 200});
    } else {
      await drawTopHitOverlay(jsroot, painter, latest.hits);
      await drawHtmlHitOverlay(jsroot, painter, latest.hits, latest.track, divId, snapshots);
    }
    if (canvas) canvas.dataset.drawn = "1";
    const hits = snapshots.reduce(function(sum, item) { return sum + hitCount(item.hits); }, 0);
    if (eventDisplay) {
      showPlotMessage(key, renderEventInfo(label) + ", hits " + hits + ", track " + (trackOn(latest.track) ? "on" : "off"));
      syncEventControls();
    } else {
      showPlotMessage(key, label + ": hits " + hits + ", track " + (trackOn(latest.track) ? "on" : "off"));
    }
  };
  if (isEventDisplaySpec(spec) && eventState.paused && overlayPainters[key] && canvas && canvas.dataset.drawn === "1") {
    await updateHits(overlayPainters[key], null, null, "paused");
    return;
  }
  if (overlayPainters[key] && canvas && canvas.dataset.drawn === "1") {
    if (isEventDisplaySpec(spec)) {
      await updateHits(overlayPainters[key], null, null, "updated");
      return;
    }
    const hitObj = await jsroot.httpRequest(rootPath(spec.overlayPath), "object");
    const trackObj = spec.trackPath ? await jsroot.httpRequest(rootPath(spec.trackPath), "object") : null;
    if (!current()) return;
    objectCache[spec.overlayPath] = hitObj;
    if (spec.trackPath) objectCache[spec.trackPath] = trackObj;
    await updateHits(overlayPainters[key], hitObj, trackObj, "updated");
    return;
  }
  const drawPair = async function(geoObj, hitObj, trackObj, label) {
    if (!current()) return;
    const geo = cloneRootObject(geoObj);
    if (typeof spec.transparency === "number") applyGeoTransparency(geo, spec.transparency);
    const painter = await drawObject(jsroot, divId, geo, spec.opt || "all", true);
    if (!current()) return;
    if (painter) overlayPainters[key] = painter;
    await updateHits(painter, hitObj, trackObj, label);
  };
  if (isEventDisplaySpec(spec) && objectCache[spec.path] && canvas && canvas.dataset.drawn !== "1") {
    await drawPair(objectCache[spec.path], null, null, "cached");
  } else if (objectCache[spec.path] && objectCache[spec.overlayPath] && (!spec.trackPath || objectCache[spec.trackPath]) && canvas && canvas.dataset.drawn !== "1") {
    await drawPair(objectCache[spec.path], objectCache[spec.overlayPath], spec.trackPath ? objectCache[spec.trackPath] : null, "cached");
  }
  const geoObj = objectCache[spec.path] || await jsroot.httpRequest(rootPath(spec.path), "object");
  if (!current()) return;
  if (isEventDisplaySpec(spec)) {
    objectCache[spec.path] = geoObj;
    await drawPair(geoObj, null, null, "entries");
    return;
  }
  const hitObj = await jsroot.httpRequest(rootPath(spec.overlayPath), "object");
  const trackObj = spec.trackPath ? await jsroot.httpRequest(rootPath(spec.trackPath), "object") : null;
  if (!current()) return;
  objectCache[spec.path] = geoObj;
  objectCache[spec.overlayPath] = hitObj;
  if (spec.trackPath) objectCache[spec.trackPath] = trackObj;
  await drawPair(geoObj, hitObj, trackObj, "entries");
}
async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return await r.json();
}
function shortPath(path) {
  if (!path) return "-";
  const parts = String(path).split("/").filter(Boolean);
  if (parts.length <= 2) return path;
  return parts.slice(-2).join("/");
}
function formatTime(seconds) {
  if (!seconds) return "-";
  try {
    const date = new Date(seconds * 1000);
    const pad = function(value) { return String(value).padStart(2, "0"); };
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
           " " + pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  } catch (e) {
    return "-";
  }
}
function formatClockMs(ms) {
  const date = new Date(ms);
  const pad = function(value) { return String(value).padStart(2, "0"); };
  return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
}
function compactCount(value) {
  if (!Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 1000000) return (value / 1000000).toFixed(1) + "M";
  if (Math.abs(value) >= 1000) return (value / 1000).toFixed(1) + "k";
  return String(Math.round(value));
}
function recordTriggerCount(count) {
  if (!Number.isFinite(count)) return;
  const now = Date.now();
  triggerHistory.push({t: now, c: count});
  const cutoff = now - triggerHistoryMaxAgeMs;
  while (triggerHistory.length && (triggerHistory[0].t < cutoff || triggerHistory.length > triggerHistoryMaxPoints)) {
    triggerHistory.shift();
  }
}
function triggerTrendMarkup() {
  const points = triggerHistory.slice();
  const latest = points.length ? points[points.length - 1].c : null;
  let html = "<div class='trigger-trend'><div class='trigger-trend-title'>Trigger count <span>" +
             (latest === null ? "no samples" : ("latest " + compactCount(latest))) +
             "</span></div>";
  if (points.length < 2) {
    return html + "<div class='trigger-trend-empty'>collecting samples</div></div>";
  }

  const width = 296, height = 138;
  const left = 42, right = 8, top = 10, bottom = 24;
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  const tMin = points[0].t;
  const tMax = points[points.length - 1].t;
  let cMin = Math.min.apply(null, points.map(function(item) { return item.c; }));
  let cMax = Math.max.apply(null, points.map(function(item) { return item.c; }));
  if (cMin === cMax) {
    const pad = Math.max(1, Math.abs(cMin) * 0.02);
    cMin -= pad;
    cMax += pad;
  } else {
    const pad = (cMax - cMin) * 0.08;
    cMin -= pad;
    cMax += pad;
  }
  const xOf = function(t) { return left + ((t - tMin) / Math.max(1, tMax - tMin)) * plotW; };
  const yOf = function(c) { return top + (1 - (c - cMin) / Math.max(1, cMax - cMin)) * plotH; };
  const line = points.map(function(item) { return xOf(item.t).toFixed(1) + "," + yOf(item.c).toFixed(1); }).join(" ");
  const area = left + "," + (top + plotH) + " " + line + " " + (left + plotW) + "," + (top + plotH);
  const midY = (cMin + cMax) / 2;
  const midT = tMin + (tMax - tMin) / 2;
  html += "<svg viewBox='0 0 " + width + " " + height + "'>";
  [cMin, midY, cMax].forEach(function(value) {
    const y = yOf(value).toFixed(1);
    html += "<line class='grid-line' x1='" + left + "' y1='" + y + "' x2='" + (left + plotW) + "' y2='" + y + "'/>";
    html += "<text x='4' y='" + (parseFloat(y) + 3).toFixed(1) + "'>" + compactCount(value) + "</text>";
  });
  html += "<line class='axis-line' x1='" + left + "' y1='" + top + "' x2='" + left + "' y2='" + (top + plotH) + "'/>";
  html += "<line class='axis-line' x1='" + left + "' y1='" + (top + plotH) + "' x2='" + (left + plotW) + "' y2='" + (top + plotH) + "'/>";
  html += "<polygon class='trend-fill' points='" + area + "'/>";
  html += "<polyline class='trend-line' points='" + line + "'/>";
  html += "<text x='" + left + "' y='" + (height - 5) + "'>" + formatClockMs(tMin) + "</text>";
  html += "<text x='" + (left + plotW * 0.5 - 24) + "' y='" + (height - 5) + "'>" + formatClockMs(midT) + "</text>";
  html += "<text x='" + (left + plotW - 48) + "' y='" + (height - 5) + "'>" + formatClockMs(tMax) + "</text>";
  html += "</svg></div>";
  return html;
}
function statusItem(label, value, cls) {
  const item = document.createElement("div");
  item.className = "status-item";
  const name = document.createElement("div");
  name.className = "status-label";
  name.textContent = label;
  const text = document.createElement("div");
  text.className = "status-value" + (cls ? " " + cls : "");
  text.title = value || "";
  text.textContent = value || "-";
  item.appendChild(name);
  item.appendChild(text);
  return item;
}
function updateRunButtons(running) {
  const start = document.getElementById("startBtn");
  const stop = document.getElementById("stopBtn");
  const live = document.getElementById("liveState");
  if (start) start.disabled = !!running;
  if (stop) stop.disabled = !running;
  if (live) {
    live.textContent = running ? "LIVE" : "OFFLINE";
    live.classList.toggle("offline", !running);
  }
}
function renderStatus(data) {
  const root = document.getElementById("status");
  if (!root) return;
  updateRunButtons(!!data.running);
  root.innerHTML = "";
  root.appendChild(statusItem("State", data.running ? "Running" : "Stopped", data.running ? "ok" : "stop"));
  root.appendChild(statusItem("Input", shortPath(data.input_path), ""));
  root.appendChild(statusItem("Elemap", shortPath(data.map_file), ""));
  root.appendChild(statusItem("Calib", data.calib_mode || "-", ""));
  root.appendChild(statusItem("Track", data.track_algo || "-", ""));
  root.appendChild(statusItem("ROOT port", String(data.root_port || "-"), ""));
  root.appendChild(statusItem("PID", data.pid ? String(data.pid) : "-", ""));
  root.appendChild(statusItem("Started", formatTime(data.started_at), ""));
}
async function refreshStatus() {
  try {
    const data = await api("/api/status");
    renderStatus(data);
    return data;
  } catch (e) {
    const root = document.getElementById("status");
    if (root) {
      root.innerHTML = "";
      root.appendChild(statusItem("State", "Control server unavailable", "stop"));
    }
    return null;
  }
}
async function refreshTriggerStatus() {
  const root = document.getElementById("triggerStatus");
  if (!root) return;
  try {
    const data = await api("/api/trigger_count");
    const value = data.count === null || data.count === undefined ? "not ready" : String(data.count);
    if (data.count !== null && data.count !== undefined) recordTriggerCount(Number(data.count));
    root.innerHTML = "";
    const item = statusItem("Trigger count", value, data.count === null || data.count === undefined ? "stop" : "");
    item.insertAdjacentHTML("beforeend", triggerTrendMarkup());
    root.appendChild(item);
  } catch (e) {
    root.innerHTML = "";
    const item = statusItem("Trigger count", "not ready", "stop");
    item.insertAdjacentHTML("beforeend", triggerTrendMarkup());
    root.appendChild(item);
  }
}
function warningItemText(item) {
  const type = item.type === "high" ? "HIGH" : "LOW";
  return "D" + item.det + " strip " + item.strip + " " + type +
         " count " + item.count + " vs avg " + item.neighbor_avg + " (" + item.ratio + "x)";
}
async function refreshChannelWarnings() {
  const card = document.getElementById("warningCard");
  if (!card) return;
  const subtitle = card.querySelector(".warning-subtitle");
  const list = card.querySelector(".warning-list");
  try {
    const data = await api("/api/channel_warnings");
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const level = warnings.length ? "warning" : (data.level === "ok" ? "ok" : "waiting");
    card.className = "warning-card " + level;
    if (subtitle) {
      if (level === "warning") {
        subtitle.textContent = warnings.length + " abnormal strip(s), threshold: >5x or <20% of neighbor average";
      } else if (level === "ok") {
        subtitle.textContent = "monitoring " + (data.ready_detectors || 0) + "/" + (data.detectors || mapInfo.detectors.length) + " active detector(s)";
      } else {
        subtitle.textContent = "waiting for strip statistics";
      }
    }
    if (!list) return;
    list.innerHTML = "";
    if (warnings.length) {
      warnings.slice(0, 12).forEach(function(item) {
        const chip = document.createElement("span");
        chip.className = "warning-item";
        chip.textContent = warningItemText(item);
        list.appendChild(chip);
      });
      if (warnings.length > 12) {
        const more = document.createElement("span");
        more.className = "warning-item";
        more.textContent = "+" + (warnings.length - 12) + " more";
        list.appendChild(more);
      }
    } else {
      const empty = document.createElement("span");
      empty.className = "warning-empty";
      empty.textContent = level === "ok" ? "No abnormal strip detected." : "No detector has reached the 100-hit activation threshold.";
      list.appendChild(empty);
    }
  } catch (e) {
    card.className = "warning-card waiting";
    if (subtitle) subtitle.textContent = "warning status is not ready";
    if (list) list.innerHTML = "<span class='warning-empty'>ROOT warning object is not ready.</span>";
  }
}
function selectedInputPath() {
  const subdir = document.getElementById("inputSubdir")?.value || "";
  const root = document.getElementById("inputRoot")?.value || "";
  return subdir || root;
}
function autoLatestEnabled() {
  return !!document.getElementById("autoLatestSubdir")?.checked;
}
function validRunDate(dateText) {
  const year = parseInt(dateText.slice(0, 4), 10);
  const month = parseInt(dateText.slice(4, 6), 10);
  const day = parseInt(dateText.slice(6, 8), 10);
  if (month < 1 || month > 12 || day < 1) return false;
  const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= days[month - 1];
}
function runFolderKey(path) {
  const name = (path || "").split("/").filter(Boolean).pop() || "";
  const match = name.match(/^([0-9]{8})_([0-9]{1,4})$/);
  if (!match) return null;
  if (!validRunDate(match[1])) return null;
  const run = parseInt(match[2], 10);
  if (!Number.isFinite(run) || run < 0 || run > 1000) return null;
  return {date: parseInt(match[1], 10), run: run};
}
function compareRunFolder(a, b) {
  if (a.date !== b.date) return a.date - b.date;
  return a.run - b.run;
}
function latestRunSubdir(subdirs) {
  let bestPath = "";
  let bestKey = null;
  for (const path of subdirs || []) {
    const key = runFolderKey(path);
    if (!key) continue;
    if (!bestKey || compareRunFolder(key, bestKey) > 0) {
      bestKey = key;
      bestPath = path;
    }
  }
  return bestPath;
}
function fillInputSubdirSelect(data, selected) {
  const select = document.getElementById("inputSubdir");
  const rootInput = document.getElementById("inputRoot");
  if (!select || !rootInput) return "";
  const root = rootInput.value.trim();
  const subdirs = data.subdirs || [];
  if (autoLatestEnabled()) selected = latestRunSubdir(subdirs) || selected;
  select.innerHTML = "";
  for (const path of subdirs) {
    const opt = document.createElement("option");
    opt.value = path;
    opt.textContent = path.split("/").filter(Boolean).pop() || path;
    if (path === selected) opt.selected = true;
    select.appendChild(opt);
  }
  if (selected && !Array.from(select.options).some(function(opt) { return opt.value === selected; })) {
    const opt = document.createElement("option");
    opt.value = selected;
    opt.textContent = selected.split("/").filter(Boolean).pop() || selected;
    opt.selected = true;
    select.appendChild(opt);
  }
  if (!select.options.length) {
    const opt = document.createElement("option");
    opt.value = data.root || root;
    opt.textContent = "(use root folder)";
    select.appendChild(opt);
  }
  if (select.selectedIndex < 0 && select.options.length) select.selectedIndex = 0;
  return select.value;
}
async function loadInputSubdirs(preferSaved, selectedOverride) {
  if (preferSaved === undefined) preferSaved = true;
  const rootInput = document.getElementById("inputRoot");
  const select = document.getElementById("inputSubdir");
  if (!rootInput || !select) return;
  const root = rootInput.value.trim();
  const saved = loadSavedSettings();
  let selected = selectedOverride || (preferSaved ? (saved.input_subdir || saved.input_path || select.value) : "");
  const rootPrefix = root.replace(/\/+$/, "") + "/";
  if (selected && selected !== root && selected.indexOf(rootPrefix) !== 0) selected = "";
  select.innerHTML = "";
  try {
    const data = await api("/api/input_subdirs?root=" + encodeURIComponent(root));
    fillInputSubdirSelect(data, selected);
    saveSettings();
  } catch (e) {
    const opt = document.createElement("option");
    opt.value = root;
    opt.textContent = "(cannot scan; use root)";
    select.appendChild(opt);
    saveSettings();
  }
}
async function loadMaps(selectedOverride) {
  const data = await api("/api/maps");
  const saved = loadSavedSettings();
  const select = document.getElementById("mapFile");
  select.innerHTML = "";
  let selected = selectedOverride || saved.map_file || data.selected;
  if (selected && selected.indexOf("/") < 0) {
    const matched = (data.maps || []).find(function(path) { return path.split("/").pop() === selected; });
    if (matched) selected = matched;
  }
  for (const path of data.maps) {
    const opt = document.createElement("option");
    opt.value = path;
    opt.textContent = path.split("/").pop();
    if (path === selected) opt.selected = true;
    select.appendChild(opt);
  }
  if (selected && !Array.from(select.options).some(function(opt) { return opt.value === selected; })) {
    const opt = document.createElement("option");
    opt.value = selected;
    opt.textContent = selected.split("/").pop();
    opt.selected = true;
    select.appendChild(opt);
  }
}
async function loadMapInfo(persist) {
  if (persist === undefined) persist = true;
  const mapSelect = document.getElementById("mapFile");
  const mapPath = mapSelect && mapSelect.value ? mapSelect.value : "";
  const data = await api("/api/map_info?map=" + encodeURIComponent(mapPath));
  normalizeMapInfo(data);
  if (persist) saveSettings();
  return mapInfo;
}
async function checkAutoLatestSubdir() {
  if (!autoLatestEnabled() || autoLatestInFlight) return;
  const rootInput = document.getElementById("inputRoot");
  const select = document.getElementById("inputSubdir");
  if (!rootInput || !select) return;
  autoLatestInFlight = true;
  try {
    const data = await api("/api/input_subdirs?root=" + encodeURIComponent(rootInput.value.trim()));
    const latest = latestRunSubdir(data.subdirs || []);
    if (!latest) return;
    const current = selectedInputPath();
    fillInputSubdirSelect(data, latest);
    saveSettings();
    if (latest !== current) {
      const status = await api("/api/status");
      if (status.running) {
        await startMonitor();
      }
      await refreshStatus();
    }
  } catch (e) {
  } finally {
    autoLatestInFlight = false;
  }
}
function selectValue(id, fallback) {
  if (id === "feeSelect") return selectionState.fee;
  if (id === "channelSelect") return selectionState.channel;
  if (id === "detSelect") return selectionState.det;
  if (id === "sideSelect") return selectionState.side;
  if (id === "geomObjectSelect") return selectionState.geomObject;
  return parseInt(document.getElementById(id)?.value || fallback, 10);
}
function plotSpecs() {
  const fee = selectValue("feeSelect", 0), ch = selectValue("channelSelect", 0), det = selectValue("detSelect", 0), side = selectValue("sideSelect", 0);
  if (activeTab === "overview") return [
    {title: "TOT spectrum", path: "/iTOF/Unpacker/Overview/TOT_all", opt: "hist"},
    {title: "Digi multiplicity", path: "/iTOF/DigiMultiplicity", opt: "hist"},
    {title: "Hit multiplicity", path: "/iTOF/HitMultiplicity", opt: "hist"},
    {title: "Digi hit map", path: "/iTOF/Debug/DigiMap", opt: "colz"}
  ];
  if (activeTab === "hpos") {
    const specs = mapInfo.detectors.map(function(detId) {
      return {title: "Detector " + detId + " hit map", path: "/iTOF/Position/Detectors/hPos_" + detId, opt: "colz"};
    });
    specs.push(
      {title: "Wall hit map (Z)", path: "/iTOF/Position/hPos_z", opt: "colz"},
      {title: "Wall hit map (X-)", path: "/iTOF/Position/hPos_xn", opt: "colz"},
      {title: "Wall hit map (X+)", path: "/iTOF/Position/hPos_xp", opt: "colz"}
    );
    return specs;
  }
  if (activeTab === "geometry") {
    const objectName = selectValue("geomObjectSelect", "MapTOPiTOF");
    return [
      {title: objectName, path: "/iTOF/EventDisplay/" + objectName, opt: "all;opacity100", tall: true, wide: true, staticObject: true, forceDraw: true}
    ];
  }
  if (activeTab === "fee") return [
    {title: "Channel occupancy - FEE " + fee, path: "/iTOF/Unpacker/FEE/count_channel_" + fee, opt: "hist"},
    {title: "TOT map - FEE " + fee, path: "/iTOF/Unpacker/FEE/count_FEE_" + fee, opt: "colz"}
  ];
  if (activeTab === "channel") return [
    {title: "TOT spectrum - FEE " + fee + " CH " + ch, path: "/iTOF/Unpacker/ChannelTOT/TOT_channel_" + fee + "_" + ch, opt: "hist"},
    {title: "Leading time - FEE " + fee + " CH " + ch, path: "/iTOF/Unpacker/ChannelLeading/Tt_Leading_channel_" + fee + "_" + ch, opt: "hist"},
    {title: "Double-chain time diff - FEE " + fee + " CH " + ch, path: "/iTOF/Unpacker/ChannelTiming/double_chain_timediff_" + fee + "_" + ch, opt: "hist"}
  ];
  if (activeTab === "detector") return [
    {title: "Detector " + det + " side " + side + " TOT map", path: "/iTOF/Unpacker/Detector/TOT_Det_" + det + "_" + side, opt: "colz"}
  ];
  if (activeTab === "timing") return mapInfo.detectors.map(function(detId) {
    return {title: "Leading time - detector " + detId, path: "/iTOF/Unpacker/Timing/LeadingTime_det" + detId, opt: "hist"};
  });
  return [
    {id: "EventDisplay_Hits", title: "Event display", path: "/iTOF/EventDisplay/MapTOPiTOF", opt: "all;transp96", overlayPath: "/iTOF/LatestHits3D", trackPath: "/iTOF/LatestTrack3D", overlayOpt: "same p", tall: true, wide: true, staticObject: true, forceDraw: true}
  ];
}
function makeSelect(id, label, values) {
  const wrap = document.createElement("label");
  wrap.textContent = label;
  const sel = document.createElement("select");
  sel.id = id;
  if (id === "geomObjectSelect" && values.indexOf(selectionState.geomObject) < 0) selectionState.geomObject = values[0];
  for (const value of values) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    if ((id === "feeSelect" && value === selectionState.fee) ||
        (id === "channelSelect" && value === selectionState.channel) ||
        (id === "detSelect" && value === selectionState.det) ||
        (id === "sideSelect" && value === selectionState.side) ||
        (id === "geomObjectSelect" && value === selectionState.geomObject)) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.onchange = function() {
    const value = /^-?\d+$/.test(sel.value) ? parseInt(sel.value, 10) : sel.value;
    if (id === "feeSelect") selectionState.fee = value;
    if (id === "channelSelect") selectionState.channel = value;
    if (id === "detSelect") selectionState.det = value;
    if (id === "sideSelect") selectionState.side = value;
    if (id === "geomObjectSelect") selectionState.geomObject = value;
    clampSelectionToMap();
    saveSettings();
    renderSelectors();
    renderActive();
  };
  wrap.appendChild(sel);
  return wrap;
}
function makeEventControls() {
  const wrap = document.createElement("div");
  wrap.className = "event-controls";

  const pauseBtn = document.createElement("button");
  pauseBtn.id = "eventPauseBtn";
  pauseBtn.type = "button";
  pauseBtn.onclick = toggleEventRefresh;
  wrap.appendChild(pauseBtn);

  const prevBtn = document.createElement("button");
  prevBtn.id = "eventPrevBtn";
  prevBtn.type = "button";
  prevBtn.className = "secondary";
  prevBtn.textContent = "Previous";
  prevBtn.onclick = eventPrev;
  wrap.appendChild(prevBtn);

  const nextBtn = document.createElement("button");
  nextBtn.id = "eventNextBtn";
  nextBtn.type = "button";
  nextBtn.className = "secondary";
  nextBtn.textContent = "Next";
  nextBtn.onclick = eventNext;
  wrap.appendChild(nextBtn);

  const nLabel = document.createElement("label");
  nLabel.textContent = "Show events";
  const nInput = document.createElement("input");
  nInput.id = "eventShowN";
  nInput.type = "number";
  nInput.min = "1";
  nInput.max = "50";
  nInput.step = "1";
  nInput.value = eventState.showN;
  nInput.onchange = updateEventShowN;
  nInput.oninput = updateEventShowN;
  nLabel.appendChild(nInput);
  wrap.appendChild(nLabel);

  const info = document.createElement("span");
  info.id = "eventInfo";
  info.className = "event-info";
  wrap.appendChild(info);
  setTimeout(syncEventControls, 0);
  return wrap;
}
function renderSelectors() {
  const root = document.getElementById("selectors");
  root.innerHTML = "";
  clampSelectionToMap();
  if (activeTab === "fee" || activeTab === "channel") root.appendChild(makeSelect("feeSelect", "FEE", mapInfo.fees));
  if (activeTab === "channel") root.appendChild(makeSelect("channelSelect", "Channel", channelsForFee(selectionState.fee)));
  if (activeTab === "detector") {
    root.appendChild(makeSelect("detSelect", "Detector", mapInfo.detectors));
    root.appendChild(makeSelect("sideSelect", "Side", sidesForDetector(selectionState.det)));
  }
  if (activeTab === "geometry") {
    root.appendChild(makeSelect("geomObjectSelect", "Geometry object", ["MapTOPiTOF","iTOFDetectorUnit","MRPC","MapGeometry","FAIRGeom","TOPiTOF","itof","M1"]));
  }
  if (activeTab === "event") root.appendChild(makeEventControls());
}
function renderTabs() {
  const nav = document.getElementById("tabs");
  nav.innerHTML = "";
  for (const tab of tabs) {
    const btn = document.createElement("button");
    btn.className = "tab" + (tab.id === activeTab ? " active" : "");
    btn.textContent = tab.label;
    btn.onclick = function() { activeTab = tab.id; saveSettings(); renderActive(); };
    nav.appendChild(btn);
  }
}
function activeViewKey() {
  if (activeTab === "hpos" || activeTab === "timing") return activeTab + "_map_" + mapViewRevision;
  if (activeTab === "fee") return "fee_" + selectionState.fee;
  if (activeTab === "channel") return "channel_" + selectionState.fee + "_" + selectionState.channel;
  if (activeTab === "detector") return "detector_" + selectionState.det + "_" + selectionState.side;
  if (activeTab === "geometry") return "geometry_" + selectionState.geomObject;
  return activeTab;
}
function renderActive() {
  renderTabs();
  renderSelectors();
  if (!appReady) return;
  drawGeneration += 1;
  refreshSequence += 1;
  Object.keys(drawInFlight).forEach(function(key) { delete drawInFlight[key]; });
  const specs = plotSpecs();
  const holder = document.getElementById("plotGrid");
  const key = activeViewKey();
  for (const view of holder.querySelectorAll(".view")) view.classList.remove("active");
  let grid = document.getElementById("view_" + key);
  const specSignature = specs.map(function(spec) { return specKey(spec); }).join("|");
  if (grid && grid.dataset.specSignature !== specSignature) {
    grid.remove();
    grid = null;
  }
  if (!grid) {
    grid = document.createElement("section");
    grid.id = "view_" + key;
    grid.className = "view grid" + (specs.length > 4 ? " dense" : "");
    grid.dataset.specSignature = specSignature;
    holder.appendChild(grid);
    for (const spec of specs) {
      const panel = document.createElement("div");
      panel.className = "panel" + (spec.wide ? " wide" : "");
      panel.innerHTML = "<h2></h2><div class='plot " + (spec.tall ? "tall" : "") + "'><div class='plot-canvas'></div><div class='plot-loading active'><div class='plot-loading-box'><span class='plot-spinner'></span><span class='plot-loading-text'>loading</span></div></div><div class='plot-msg'>loading...</div></div>";
      panel.querySelector("h2").textContent = spec.title;
      panel.querySelector(".plot-canvas").id = plotId(specKey(spec));
      panel.querySelector(".plot-msg").id = msgId(specKey(spec));
      grid.appendChild(panel);
    }
  }
  grid.classList.add("active");
  refreshActive();
}
async function refreshActive() {
  if (!appReady) return;
  const sequence = ++refreshSequence;
  for (const spec of plotSpecs()) {
    if (sequence !== refreshSequence) return;
    const canvas = document.getElementById(plotId(specKey(spec)));
    if (canvas && !spec.overlayPath) canvas.dataset.drawn = "0";
    await drawRootPlot(spec);
  }
  if (sequence === refreshSequence) {
    refreshTriggerStatus();
    refreshChannelWarnings();
  }
}
async function startMonitor() {
  clearPlotCaches(false);
  resetEventHistory();
  if (!document.getElementById("inputSubdir").value) await loadInputSubdirs();
  saveSettings();
  await api("/api/start", {
    input_path: selectedInputPath(),
    map_file: document.getElementById("mapFile").value,
    calib_mode: document.getElementById("calibMode").value,
    calib_file: document.getElementById("calibFile").value,
    track_algo: document.getElementById("trackAlgo").value
  });
  setTimeout(refreshActive, 2500);
  setTimeout(refreshActive, 10000);
  setTimeout(refreshStatus, 700);
}
async function stopMonitor() { await api("/api/stop", {}); await refreshStatus(); }
async function initApp() {
  await loadServerSettings();
  applySavedSettings();
  attachSettingsPersistence();
  updateRunButtons(false);
  renderTabs();
  renderSelectors();
  const status = await refreshStatus();
  const saved = loadSavedSettings();
  const hasSavedRunSettings = !!(saved.input_path || saved.input_subdir || saved.map_file);
  if (!hasSavedRunSettings) applyRunningStatusToControls(status);
  refreshChannelWarnings();
  await loadMaps(status && status.running && !hasSavedRunSettings ? status.map_file : "");
  await loadMapInfo(false);
  await loadInputSubdirs(true, status && status.running && !hasSavedRunSettings ? status.input_path : "");
  settingsHydrated = true;
  saveSettings();
  appReady = true;
  checkAutoLatestSubdir();
  renderActive();
}
setInterval(function() { refreshActive(); }, 30000);
setInterval(refreshStatus, 5000);
setInterval(refreshChannelWarnings, 5000);
setInterval(function() { if (appReady) checkAutoLatestSubdir(); }, 10000);
initApp().catch(function(err) {
  settingsHydrated = true;
  appReady = true;
  renderActive();
  showPlotMessage(specKey((plotSpecs() || [])[0] || {id: "init"}), "init failed: " + (err && err.message ? err.message : err));
});
</script>
</body>
</html>
"""


def render_control_page():
    return (HTML
            .replace("@DEFAULT_INPUT_ROOT@", html.escape(DEFAULT_INPUT_ROOT, quote=True))
            .replace("@DEFAULT_CALIB@", html.escape(DEFAULT_CALIB, quote=True)))


HPOS_VIEW = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    html, body, #drawing { margin: 0; width: 100%; height: 100%; overflow: hidden; background: white; }
    #msg { position: absolute; left: 8px; top: 8px; padding: 4px 6px; background: rgba(255,255,255,.85); font: 12px Arial; color: #333; }
  </style>
</head>
<body>
  <div id="drawing"></div>
  <div id="msg">loading hPos_@DET@...</div>
  <script>
    const rootBase = window.location.protocol + "//" + window.location.hostname + ":8090";
    let jsrootReady = false;
    let drawInFlight = false;
    function showMessage(text) { document.getElementById("msg").textContent = text; }
    function refreshHpos() {
      if (!jsrootReady || drawInFlight) return;
      drawInFlight = true;
      JSROOT.httpRequest("/api/hpos_object?det=@DET@&ts=" + Date.now(), "object")
      .then(function(obj) {
        showMessage("entries: " + (obj.fEntries || 0));
        if (JSROOT.redraw) {
          return JSROOT.redraw("drawing", obj, "colz");
        }
        JSROOT.cleanup("drawing");
        return JSROOT.draw("drawing", obj, "colz");
      })
      .catch(function(err) {
        showMessage("failed to draw hPos_@DET@: " + err);
      })
      .then(function() {
        drawInFlight = false;
      });
    }
    window.addEventListener("message", function(ev) {
      if (ev.origin !== window.location.origin) return;
      if (ev.data && ev.data.type === "refreshHpos") refreshHpos();
    });
    const jsrootScript = document.createElement("script");
    jsrootScript.src = rootBase + "/jsrootsys/scripts/JSRoot.core.js";
    jsrootScript.onload = function() {
      jsrootReady = true;
      refreshHpos();
    };
    jsrootScript.onerror = function() { showMessage("failed to load JSROOT from " + rootBase); };
    document.head.appendChild(jsrootScript);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def safe_write(self, data):
        try:
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def send_json(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def send_bytes(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            data = render_control_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.safe_write(data)
            return
        if path == "/hpos_view":
            query = parse_qs(parsed.query)
            try:
                det = int(query.get("det", ["0"])[0])
            except Exception:
                det = 0
            det = max(0, min(23, det))
            self.send_bytes(HPOS_VIEW.replace("@DET@", str(det)).encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/hpos_object":
            query = parse_qs(parsed.query)
            try:
                det = int(query.get("det", ["0"])[0])
            except Exception:
                det = 0
            det = max(0, min(23, det))
            url = "http://127.0.0.1:%d/iTOF/Position/Detectors/hPos_%d/root.json" % (ROOT_PORT, det)
            try:
                data = urlopen(url, timeout=5).read()
                self.send_bytes(data, "application/json")
            except Exception as exc:
                self.send_error(503, str(exc))
            return
        if path == "/api/root_object":
            query = parse_qs(parsed.query)
            object_path = query.get("path", [""])[0]
            if not object_path.startswith("/iTOF/") or ".." in object_path:
                self.send_error(400, "bad ROOT object path")
                return
            url = "http://127.0.0.1:%d%s/root.json" % (ROOT_PORT, object_path)
            try:
                data = urlopen(url, timeout=5).read()
                self.send_bytes(data, "application/json")
            except Exception as exc:
                self.send_error(503, str(exc))
            return
        if path == "/api/status":
            self.send_json({
                "running": STATE.running(),
                "input_path": STATE.input_path,
                "map_file": STATE.map_file,
                "calib_mode": STATE.calib_mode,
                "calib_file": STATE.calib_file,
                "track_algo": STATE.track_algo,
                "root_port": ROOT_PORT,
                "pid": STATE.proc.pid if STATE.running() else None,
                "started_at": STATE.started_at,
                "log": LOG_FILE,
            })
            return
        if path == "/api/settings":
            self.send_json(load_web_settings())
            return
        if path == "/api/maps":
            self.send_json({
                "maps": available_maps(),
                "selected": STATE.map_file,
            })
            return
        if path == "/api/map_info":
            query = parse_qs(parsed.query)
            self.send_json(elemap_info(query.get("map", [STATE.map_file])[0]))
            return
        if path == "/api/input_subdirs":
            query = parse_qs(parsed.query)
            root = query.get("root", [DEFAULT_INPUT_ROOT])[0]
            self.send_json(input_subdirs(root))
            return
        if path == "/api/trigger_count":
            self.send_json(trigger_count())
            return
        if path == "/api/channel_warnings":
            self.send_json(channel_warnings())
            return
        if path == "/api/hpos":
            self.send_json(hpos_entries())
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        path = urlparse(self.path).path
        if path == "/api/start":
            save_web_settings(payload)
            STATE.start(payload.get("input_path", DEFAULT_INPUT),
                        payload.get("calib_mode", "self"),
                        payload.get("calib_file", DEFAULT_CALIB),
                        payload.get("map_file", DEFAULT_MAP),
                        payload.get("track_algo", "Fast"))
            self.send_json({"ok": True, "running": STATE.running(), "pid": STATE.proc.pid})
            return
        if path == "/api/stop":
            STATE.stop()
            self.send_json({"ok": True, "running": False})
            return
        if path == "/api/settings":
            ok = save_web_settings(payload)
            self.send_json({"ok": ok})
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        return


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()
    if args.start:
        settings = saved_start_settings()
        STATE.start(settings["input_path"],
                    settings["calib_mode"],
                    settings["calib_file"],
                    settings["map_file"],
                    settings["track_algo"])
    server = ThreadedHTTPServer(("0.0.0.0", args.port), Handler)
    print("Control page: http://127.0.0.1:%d/" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
