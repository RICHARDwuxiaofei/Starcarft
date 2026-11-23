"""Starcarft Arena Control: loopback-only, dependency-free web launcher.

This server never imports contestants and never starts SC2 until an explicit Start click.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parent.parent
LAUNCHER = Path(__file__).resolve().parent
WORKTREES = PROJECT.parent / "Starcarft-agent-worktrees"
RESULTS = PROJECT / "results" / "gui"
CLIENT_TOKEN = secrets.token_urlsafe(28)
CURRENT: dict = {"process": None, "folder": None, "log_handle": None}
GUARD = threading.Lock()

def roots():
    dirs = [PROJECT]
    if WORKTREES.is_dir():
        dirs.extend(sorted(p for p in WORKTREES.iterdir() if p.is_dir()))
    return dirs

def catalog():
    entries = []
    seen = set()
    for root in roots():
        bot_root = root / "bots"
        if not bot_root.is_dir():
            continue
        for package in sorted(bot_root.iterdir()):
            if not package.is_dir():
                continue
            candidates = [package / "bot.py"]
            candidates += sorted(p for p in package.glob("*bot.py") if p.name != "bot.py")
            for path in candidates:
                if not path.is_file():
                    continue
                resolved = str(path.resolve())
                if resolved.lower() in seen:
                    continue
                seen.add(resolved.lower())
                model = {"webgpt": "GPT / webGPT", "mizuki": "mizuki",
                         "antigravity": "Gemini / Antigravity",
                         "codex": "Codex", "other": "Other / DeepSeek"}.get(
                             package.name.lower(), package.name)
                entries.append({
                    "id": str(len(entries)),
                    "model": model,
                    "agent": package.name,
                    "source": root.name,
                    "script": path.name,
                    "path": resolved,
                    "label": f"{model} · {path.name} · {root.name}",
                })
    return entries

def game_info():
    choices = []
    env = os.getenv("SC2PATH")
    if env:
        choices.append(Path(env))
    choices += [Path(r"C:\baoxue\StarCraft II"),
                Path(r"D:\baoxue\StarCraft II"),
                Path(r"C:\Program Files (x86)\StarCraft II")]
    for base in choices:
        if not (base / "Versions").is_dir():
            continue
        executables = list((base / "Versions").glob("Base*/SC2_x64.exe"))
        def build_number(p):
            name = p.parent.name
            return int(name.removeprefix("Base")) if name.removeprefix("Base").isdigit() else -1
        executables.sort(key=build_number, reverse=True)
        if executables:
            maps = sorted(p.stem for p in (base / "Maps").glob("*.SC2Map"))
            return {"root": str(base), "executable": str(executables[0]),
                    "build": executables[0].parent.name, "maps": maps, "available": True}
    return {"root": env or "", "executable": "", "build": "",
            "maps": [], "available": False}

def load_report(folder):
    if not folder:
        return None
    for name in ("result.json", "stop.json"):
        file = folder / name
        if file.exists():
            try:
                return json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    return None

def current_state():
    with GUARD:
        process = CURRENT["process"]
        folder = CURRENT["folder"]
        if process is None or folder is None:
            return {"running": False, "status": "IDLE", "folder": None}
        code = process.poll()
        report = load_report(folder)
        if code is None:
            status = "RUNNING"
        elif report:
            status = report.get("status", "UNKNOWN")
        else:
            status = "FAILED" if code else "EXITED"
        return {"running": code is None, "status": status, "exit_code": code,
                "folder": str(folder), "report": report}

def history():
    if not RESULTS.is_dir():
        return []
    out = []
    for folder in sorted((d for d in RESULTS.iterdir() if d.is_dir()), reverse=True)[:16]:
        report = load_report(folder)
        out.append({"id": folder.name, "report": report,
                    "status": (report or {}).get("status", "UNKNOWN")})
    return out

def resolve_bot(entry_id, options):
    record = next((e for e in options if e["id"] == str(entry_id)), None)
    if record is None:
        raise ValueError("请选择有效的 Bot 脚本（不可直接执行未登记的路径）。")
    file = Path(record["path"])
    if not file.is_file():
        raise ValueError("Bot 脚本已不存在，请刷新页面。")
    return {key: record[key] for key in ("model", "agent", "source", "script", "path", "label")}

def parse_request(payload):
    info = game_info()
    if not info["available"]:
        raise ValueError("没有找到 SC2_x64.exe。请在官方启动器完成游戏安装/更新后重试。")
    available_maps = info["maps"]
    map_name = str(payload.get("map", "RedCityLE"))
    if map_name not in available_maps:
        raise ValueError("所选地图不存在于游戏的 Maps 目录。")
    options = catalog()
    left = resolve_bot(payload.get("left"), options)
    opponent = str(payload.get("opponent", "computer"))
    if opponent not in {"computer", "bot", "human"}:
        raise ValueError("未知对手类型。")
    right = resolve_bot(payload.get("right"), options) if opponent == "bot" else None
    if right and left["path"].casefold() == right["path"].casefold():
        # Mirror is allowed: two separate instances from the same class.
        pass
    race = str(payload.get("race", "Terran"))
    if race not in {"Terran", "Protoss", "Zerg", "Random"}:
        raise ValueError("未知种族。")
    difficulty = str(payload.get("difficulty", "Medium"))
    if difficulty not in {"VeryEasy", "Easy", "Medium", "MediumHard",
                          "Hard", "Harder", "VeryHard"}:
        raise ValueError("未知电脑难度。")
    try:
        duration = int(payload.get("duration", 1800))
        seed = int(payload.get("seed", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("局时或随机种子不是整数。") from exc
    if not (30 <= duration <= 86400 and 0 <= seed <= 4294967295):
        raise ValueError("局时须为 30~86400 游戏秒，种子须为 32 位非负整数。")
    return {"sc2_path": info["root"], "map": map_name,
            "left": left, "opponent": opponent, "right": right,
            "race": race, "difficulty": difficulty, "duration": duration,
            "seed": seed, "realtime": bool(payload.get("realtime", False)) or opponent == "human",
            "model_label": str(payload.get("model_label", ""))[:64]}

def run_worker(config, inspect=False):
    executable = PROJECT / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        executable = Path(sys.executable)
    command = [str(executable), "-u", str(LAUNCHER / "worker.py")]
    if inspect:
        command += ["--inspect"]
    command.append(str(config))
    return command

class Handler(BaseHTTPRequestHandler):
    server_version = "StarcarftArena/1.0"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def send_json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        route = urlsplit(self.path).path
        if route in ("/", "/index.html"):
            raw = (LAUNCHER / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                             "frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif route == "/app.js":
            raw = (LAUNCHER / "app.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif route == "/app.css":
            raw = (LAUNCHER / "app.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif route == "/api/state":
            self.send_json(200, {"token": CLIENT_TOKEN, "bots": catalog(),
                                 "game": game_info(), "process": current_state(),
                                 "history": history(), "project": str(PROJECT),
                                 "python": str(PROJECT / ".venv" / "Scripts" / "python.exe")})
        elif route == "/api/log":
            state = current_state()
            path = Path(state["folder"]) / "console.log" if state["folder"] else None
            if path and path.is_file():
                with path.open("rb") as f:
                    f.seek(max(0, path.stat().st_size - 45000))
                    raw = f.read().decode("utf-8", errors="replace")
                self.send_json(200, {"log": raw})
            else:
                self.send_json(200, {"log": "尚无运行日志。"})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.headers.get("X-Arena-Token") != CLIENT_TOKEN:
            self.send_json(403, {"error": "安全校验失败，请刷新页面。"})
            return
        origin = self.headers.get("Origin")
        if origin and origin != ("http://" + self.headers.get("Host", "")):
            self.send_json(403, {"error": "拒绝跨站请求。"})
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_json(415, {"error": "请求必须是 JSON。"})
            return
        try:
            count = int(self.headers.get("Content-Length", "0"))
            if not 0 < count <= 16384:
                raise ValueError("请求长度错误。")
            data = json.loads(self.rfile.read(count))
            route = urlsplit(self.path).path
            if route == "/api/inspect":
                config = parse_request(data)
                RESULTS.mkdir(parents=True, exist_ok=True)
                config_file = RESULTS / "_last_inspection.json"
                config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                result = subprocess.run(run_worker(config_file, inspect=True), cwd=PROJECT,
                                        capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=45, check=False,
                                        env={**os.environ, "SC2PATH": config["sc2_path"]})
                self.send_json(200, {"pass": result.returncode == 0,
                                     "exit_code": result.returncode,
                                     "output": (result.stdout + result.stderr)[-15000:]})
            elif route == "/api/start":
                config = parse_request(data)
                with GUARD:
                    old = CURRENT["process"]
                    if old is not None and old.poll() is None:
                        raise ValueError("已有比赛正在运行，请先停止。")
                    folder = RESULTS / (datetime.now().strftime("%Y%m%d-%H%M%S")
                                        + "-" + uuid.uuid4().hex[:6])
                    folder.mkdir(parents=True, exist_ok=False)
                    config["result_path"] = str(folder / "result.json")
                    config["replay_path"] = str(folder / "game.SC2Replay")
                    config_file = folder / "config.json"
                    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
                    log_handle = (folder / "console.log").open("w", encoding="utf-8")
                    env = {**os.environ, "SC2PATH": config["sc2_path"],
                           "PYTHONIOENCODING": "utf-8"}
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    try:
                        proc = subprocess.Popen(run_worker(config_file), cwd=PROJECT,
                                                stdin=subprocess.DEVNULL,
                                                stdout=log_handle, stderr=subprocess.STDOUT,
                                                env=env, creationflags=flags)
                    finally:
                        log_handle.close()
                    CURRENT.update(process=proc, folder=folder, log_handle=None)
                self.send_json(200, {"ok": True, "pid": proc.pid, "folder": str(folder)})
            elif route == "/api/stop":
                with GUARD:
                    proc = CURRENT["process"]
                    folder = CURRENT["folder"]
                    if proc is None or proc.poll() is not None:
                        raise ValueError("当前没有正在运行的比赛。")
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       capture_output=True, text=True, timeout=12, check=False)
                    else:
                        proc.terminate()
                    (folder / "stop.json").write_text(json.dumps({
                        "status": "STOPPED", "message": "用户从 GUI 主动停止",
                        "timestamp": datetime.now().isoformat()}, ensure_ascii=False),
                        encoding="utf-8")
                self.send_json(200, {"ok": True})
            else:
                self.send_json(404, {"error": "Unknown action"})
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": f"服务器异常：{type(exc).__name__}: {exc}"})

def main():
    parser = argparse.ArgumentParser(description="Local-only StarCraft II Arena GUI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        info = game_info()
        print(json.dumps({"project": str(PROJECT), "game": info,
                          "bots": catalog()}, ensure_ascii=False, indent=2))
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Starcarft Arena: http://127.0.0.1:{args.port}", flush=True)
    print("仅本机访问，比赛必须通过页面明确点击启动。", flush=True)
    if not args.no_browser:
        import threading
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
