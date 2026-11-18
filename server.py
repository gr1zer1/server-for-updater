from collections import deque
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import json
import re

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APP_PATH = BASE_DIR / "updates" / "app.exe"
SCREEN_FADE_PATH = BASE_DIR / "updates" / "screen_fade.exe"

VERSION: int = 6

MINUTES: int = 0

# CORS: allow localhost:8080 (web UI) and any origin for dev
ALLOWED_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "https://server-for-updater.onrender.com",
]

# Очередь команд для агента. Одной переменной мало: CLI шлёт подряд
# "kill --ppid N" и "kill --pid N", вторая команда затёрла бы первую.
# snapshot, kill --pid 1234, kill --ppid 1234 (+ опционально --force)
COMMANDS: deque[str] = deque(["snapshot"])
COMMAND_RE = re.compile(r"^(snapshot|kill --(pid|ppid) \d+( --force)?)$")

LAST_SNAPSHOT = None



@dataclass
class SystemInfo:
    cpu_percent: float
    mem_total: int
    mem_used: int
    uptime: int


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    user: str
    cpu: float
    mem_rss: int
    threads: int
    status: str
    create_time: float
    cmdline: str


@dataclass
class Response:
    host: str
    timestamp: float
    system: SystemInfo
    processes: list[ProcessInfo]


class Handler(BaseHTTPRequestHandler):
    def _get_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        return origin if origin in ALLOWED_ORIGINS else ALLOWED_ORIGINS[0]

    def send_cors_headers(self):
        origin = self._get_origin()
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, ngrok-skip-browser-warning")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def send_json(self, obj, status: int = 200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        # self.path включает query (?cmd=...), поэтому сравнивать надо только путь
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/download":
            with open(APP_PATH, "rb") as file:
                data = file.read()

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(data)
        elif path == "/download/screen_fade":
            with open(SCREEN_FADE_PATH, "rb") as file:
                data = file.read()

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(data)
        elif path == "/version":
            self.send_json({
                "version": 1  # поменять
            })
        elif path == "/version_test":
            self.send_json({
                "version": VERSION
            })
        elif path == "/firewall":
            self.send_json({
                "minutes": MINUTES
            })
        elif path == "/command":
            # агент забирает следующую команду; пусто = делать нечего
            command = COMMANDS.popleft() if COMMANDS else ""
            self.send_json({
                "command": command
            })
        elif path == "/snapshot":
            # просим агента прислать свежий снапшот, а отдаём последний, что есть
            if "snapshot" not in COMMANDS:
                COMMANDS.append("snapshot")

            if LAST_SNAPSHOT is None:
                self.send_json({"error": "no snapshot yet"}, 503)
                return

            self.send_json(asdict(LAST_SNAPSHOT))  # dataclass сам в json не превратится
        elif path == "/command/set":
            command = parse_qs(parsed.query).get("cmd", [""])[0].strip()

            # белый список: сюда нельзя пропускать произвольную строку,
            # агент её потом исполнит
            # if not COMMAND_RE.match(command):
            #     self.send_json({"error": "bad command", "got": command}, 400)
            #     return

            COMMANDS.append(command)
            self.send_json({
                "status": "queued",
                "command": command,
                "queue": len(COMMANDS),
            })
        else:
            self.send_error(404)

    def do_POST(self):
        global LAST_SNAPSHOT

        if self.path == "/snapshot":
            try:
                content_length = int(
                    self.headers.get("Content-Length", 0)
                )

                if content_length <= 0:
                    self.send_error(400, "Empty request body")
                    return

                raw_data = self.rfile.read(content_length)

                data = json.loads(raw_data.decode("utf-8"))

                system_data = data["system"]

                system_info = SystemInfo(
                    cpu_percent=system_data["cpu_percent"],
                    mem_total=system_data["mem_total"],
                    mem_used=system_data["mem_used"],
                    uptime=system_data["uptime"],
                )

                processes = []

                for process_data in data["processes"]:

                    process = ProcessInfo(
                        pid=process_data["pid"],
                        ppid=process_data["ppid"],
                        name=process_data["name"],
                        user=process_data["user"],
                        cpu=process_data["cpu"],
                        mem_rss=process_data["mem_rss"],
                        threads=process_data["threads"],
                        status=process_data["status"],
                        create_time=process_data["create_time"],
                        cmdline=process_data["cmdline"],
                    )

                    processes.append(process)

                snapshot = Response(
                    host=data["host"],
                    timestamp=data["timestamp"],
                    system=system_info,
                    processes=processes,
                )

                LAST_SNAPSHOT = snapshot

                print(f"\nSnapshot received from {snapshot.host}")

                print(
                    f"CPU: {snapshot.system.cpu_percent:.2f}%"
                )

                print(
                    f"RAM: {snapshot.system.mem_used / 1024 / 1024:.2f} MB"
                )

                print(
                    f"Processes: {len(snapshot.processes)}"
                )

                self.send_json({
                    "status": "ok",
                    "processes_received": len(snapshot.processes),
                })

            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "Invalid JSON")

            except (KeyError, TypeError, ValueError):
                self.send_error(400, "Invalid snapshot structure")
        else:
            self.send_error(404)


server = HTTPServer(("0.0.0.0", 80), Handler)

print("Server running on port 80")
server.serve_forever()