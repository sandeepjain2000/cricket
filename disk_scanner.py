#!/usr/bin/env python3
"""
disk_scanner.py  v2.0.0
Disk Space Analyzer — full Web GUI, no third-party packages.

Run:   python disk_scanner.py
Opens: http://localhost:8765  automatically in your browser.

Requires disk_ui.html in the same folder as this script.
All scan results are stored in disk_analyzer_output/disk_analyzer.db (SQLite).
"""

import os, sys, json, sqlite3, shutil, traceback
import threading, uuid, webbrowser, string
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

SCRIPT_NAME = "disk_scanner.py"
VERSION     = "2.0.0"
PORT        = 8765
DB_NAME     = "disk_analyzer.db"
UI_FILE     = "disk_ui.html"


def load_ui() -> bytes:
    """Load the HTML UI from disk_ui.html (same directory as this script)."""
    p = Path(__file__).parent / UI_FILE
    try:
        return p.read_bytes()
    except FileNotFoundError:
        msg = (f"<h2 style='font-family:sans-serif;color:#c00;padding:40px'>"
               f"Error: {UI_FILE} not found.<br>"
               f"Place {UI_FILE} in the same folder as {SCRIPT_NAME}.</h2>")
        return msg.encode()


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # WAL mode is faster but may fail on network/SMB mounts — fall back silently
    for jm in ("WAL", "DELETE"):
        try:
            conn.execute(f"PRAGMA journal_mode={jm}")
            break
        except Exception:
            pass
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scan_path TEXT NOT NULL,
        scan_time TEXT NOT NULL, duration_sec REAL,
        total_size INTEGER, total_files INTEGER, total_folders INTEGER,
        disk_total INTEGER, disk_free INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        parent_id INTEGER REFERENCES folders(id),
        name TEXT NOT NULL, path TEXT NOT NULL,
        depth INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0,
        file_count INTEGER NOT NULL DEFAULT 0, folder_count INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
        name TEXT NOT NULL, size INTEGER NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_f_scan    ON folders(scan_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_f_parent  ON folders(parent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fi_folder ON files(folder_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fi_scan   ON files(scan_id)")
    conn.commit()
    return conn


def save_to_db(conn: sqlite3.Connection, tree: dict, meta: dict) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scans (scan_path,scan_time,duration_sec,total_size,"
        "total_files,total_folders,disk_total,disk_free) VALUES(?,?,?,?,?,?,?,?)",
        (meta["scan_path"], meta["scan_time"], meta["duration_seconds"],
         meta["total_size"], meta["total_files"], meta["total_folders"],
         meta["disk_total"], meta["disk_free"]))
    scan_id = cur.lastrowid
    queue = [(tree, None, 0)]
    while queue:
        node, parent_id, depth = queue.pop(0)
        cur.execute(
            "INSERT INTO folders(scan_id,parent_id,name,path,depth,"
            "size,file_count,folder_count,errors) VALUES(?,?,?,?,?,?,?,?,?)",
            (scan_id, parent_id, node["name"], node["path"], depth,
             node["size"], node["file_count"], node["folder_count"],
             node.get("errors", 0)))
        fid = cur.lastrowid
        for fname, fsize in node.get("top_files", []):
            cur.execute(
                "INSERT INTO files(scan_id,folder_id,name,size) VALUES(?,?,?,?)",
                (scan_id, fid, fname, fsize))
        for child in node.get("children", []):
            queue.append((child, fid, depth + 1))
    conn.commit()
    return scan_id


def load_scan_metas(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT id,scan_path,scan_time,duration_sec,total_size,"
        "total_files,total_folders,disk_total,disk_free FROM scans ORDER BY id"
    ).fetchall()
    return [{"id": r[0], "scan_path": r[1], "scan_time": r[2],
             "duration_seconds": r[3], "total_size": r[4], "total_files": r[5],
             "total_folders": r[6], "disk_total": r[7], "disk_free": r[8]}
            for r in rows]


def load_scan(conn: sqlite3.Connection, scan_id: int):
    meta = conn.execute(
        "SELECT scan_path,scan_time,duration_sec,total_size,total_files,"
        "total_folders,disk_total,disk_free FROM scans WHERE id=?",
        (scan_id,)).fetchone()
    if not meta:
        return None
    folder_rows = conn.execute(
        "SELECT id,parent_id,name,path,depth,size,file_count,folder_count,errors "
        "FROM folders WHERE scan_id=? ORDER BY id", (scan_id,)).fetchall()
    file_rows = conn.execute(
        "SELECT folder_id,name,size FROM files WHERE scan_id=? "
        "ORDER BY folder_id,size DESC", (scan_id,)).fetchall()

    files_by = defaultdict(list)
    for fid, name, size in file_rows:
        files_by[fid].append([name, size])

    nodes = {}
    root  = None
    for r in folder_rows:
        nid, pid, name, path, depth, size, fc, fdc, err = r
        node = {"name": name, "path": path, "depth": depth, "size": size,
                "file_count": fc, "folder_count": fdc, "errors": err,
                "children": [], "top_files": files_by.get(nid, [])}
        nodes[nid] = node
        if pid is None:
            root = node
        elif pid in nodes:
            nodes[pid]["children"].append(node)

    return {"scan_path": meta[0], "scan_time": meta[1], "duration_seconds": meta[2],
            "total_size": meta[3], "total_files": meta[4], "total_folders": meta[5],
            "disk_total": meta[6], "disk_free": meta[7], "tree": root}


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER
# ─────────────────────────────────────────────────────────────────────────────

def scan_directory(root_path: Path, progress: dict = None) -> dict:
    def _scan(path: Path, depth: int = 0) -> dict:
        if progress is not None:
            progress["current_dir"]     = str(path)
            progress["folders_scanned"] += 1
        node = {"name": path.name or str(path), "path": str(path),
                "size": 0, "file_count": 0, "folder_count": 0,
                "children": [], "errors": 0, "top_files": []}
        try:
            entries = list(os.scandir(path))
        except Exception:
            node["errors"] = 1
            return node
        direct_files = []
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child = _scan(Path(entry.path), depth + 1)
                    node["children"].append(child)
                    node["size"]         += child["size"]
                    node["file_count"]   += child["file_count"]
                    node["folder_count"] += 1 + child["folder_count"]
                elif entry.is_file(follow_symlinks=False):
                    try:
                        sz = entry.stat().st_size
                        node["size"]       += sz
                        node["file_count"] += 1
                        direct_files.append((entry.name, sz))
                        if progress is not None:
                            progress["files_scanned"] += 1
                    except (PermissionError, OSError):
                        pass
            except Exception:
                pass
        direct_files.sort(key=lambda x: x[1], reverse=True)
        node["top_files"] = direct_files[:10]
        node["children"].sort(key=lambda c: c["size"], reverse=True)
        return node

    tree = _scan(root_path)
    tree["name"] = root_path.name or str(root_path)
    return tree


def get_disk_info(path: Path) -> dict:
    try:
        u = shutil.disk_usage(str(path))
        return {"total": u.total, "used": u.used, "free": u.free}
    except Exception:
        return {"total": 0, "used": 0, "free": 0}


def fmt_size(b: int) -> str:
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


# ─────────────────────────────────────────────────────────────────────────────
# SCAN JOB MANAGER  (background threads)
# ─────────────────────────────────────────────────────────────────────────────

_jobs: dict        = {}
_jobs_lock         = threading.Lock()


def start_scan_job(scan_path: str, db_path: Path) -> str:
    job_id   = str(uuid.uuid4())
    progress = {"status": "scanning", "folders_scanned": 0, "files_scanned": 0,
                "current_dir": scan_path, "scan_id": None, "error": None}

    def run():
        try:
            sys.setrecursionlimit(15000)
            root  = Path(scan_path).resolve()
            start = datetime.now()
            tree  = scan_directory(root, progress=progress)
            disk  = get_disk_info(root)
            dur   = round((datetime.now() - start).total_seconds(), 1)
            meta  = {"scan_path": str(root),
                     "scan_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                     "duration_seconds": dur,
                     "total_size":    tree["size"],
                     "total_files":   tree["file_count"],
                     "total_folders": tree["folder_count"],
                     "disk_total":    disk["total"],
                     "disk_free":     disk["free"]}
            progress["status"] = "saving"
            conn    = init_db(db_path)
            scan_id = save_to_db(conn, tree, meta)
            conn.close()
            progress["scan_id"] = scan_id
            progress["status"]  = "done"
        except Exception as exc:
            progress["status"] = "error"
            progress["error"]  = str(exc)

    t = threading.Thread(target=run, daemon=True)
    with _jobs_lock:
        _jobs[job_id] = {"thread": t, "progress": progress}
    t.start()
    return job_id


# ─────────────────────────────────────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    db_path: Path = None

    def log_message(self, fmt, *args):
        pass  # suppress per-request logging

    def _json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, status: int = 400):
        self._json({"error": msg}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        p, qs  = parsed.path, parse_qs(parsed.query)

        if p == "/":
            body = load_ui()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif p == "/api/scans":
            conn = init_db(self.db_path)
            self._json(load_scan_metas(conn))
            conn.close()

        elif p.startswith("/api/scan/") and p.count("/") == 3:
            try:
                sid = int(p.split("/")[-1])
            except ValueError:
                return self._err("Invalid id")
            conn  = init_db(self.db_path)
            data  = load_scan(conn, sid)
            conn.close()
            if data is None:
                return self._err("Not found", 404)
            self._json(data)

        elif p.startswith("/api/progress/"):
            job_id = p.split("/")[-1]
            with _jobs_lock:
                job = _jobs.get(job_id)
            if not job:
                return self._err("Job not found", 404)
            self._json(job["progress"])

        elif p == "/api/browse":
            self._browse(unquote(qs.get("path", [""])[0]).strip())

        elif p == "/api/drives":
            self._drives()

        else:
            self._err("Not found", 404)

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        if urlparse(self.path).path != "/api/scan":
            return self._err("Not found", 404)
        body      = self._body()
        scan_path = (body.get("path") or "").strip()
        if not scan_path:
            return self._err("path is required")
        pt = Path(scan_path)
        if not pt.exists():
            return self._err(f"Path not found: {scan_path}")
        if not pt.is_dir():
            return self._err(f"Not a directory: {scan_path}")
        self._json({"job_id": start_scan_job(scan_path, self.db_path)})

    # ── DELETE ────────────────────────────────────────────────────────────────
    def do_DELETE(self):
        p = urlparse(self.path).path
        if not (p.startswith("/api/scan/") and p.count("/") == 3):
            return self._err("Not found", 404)
        try:
            sid = int(p.split("/")[-1])
        except ValueError:
            return self._err("Invalid id")
        conn = init_db(self.db_path)
        conn.execute("DELETE FROM files   WHERE scan_id=?", (sid,))
        conn.execute("DELETE FROM folders WHERE scan_id=?", (sid,))
        conn.execute("DELETE FROM scans   WHERE id=?",      (sid,))
        conn.commit()
        conn.close()
        self._json({"ok": True})

    # ── BROWSE ────────────────────────────────────────────────────────────────
    def _browse(self, raw: str):
        if not raw:
            return self._drives()
        try:
            path = Path(raw)
            if not path.exists():
                return self._err(f"Not found: {raw}", 404)
        except Exception as exc:
            return self._err(str(exc))
        try:
            parent = str(path.parent) if path.parent != path else None
        except Exception:
            parent = None
        items = []
        try:
            with os.scandir(path) as it:
                for e in sorted(it, key=lambda x: x.name.lower()):
                    try:
                        if e.is_dir(follow_symlinks=False) and not e.is_symlink():
                            items.append({"name": e.name, "path": str(Path(e.path))})
                    except Exception:
                        pass
        except PermissionError:
            pass
        self._json({"path": str(path), "parent": parent, "items": items})

    def _drives(self):
        drives = []
        if os.name == "nt":
            for d in string.ascii_uppercase:
                if os.path.exists(f"{d}:\\"):
                    drives.append(f"{d}:\\")
        else:
            drives = ["/"]
            for base in ["/home", "/mnt", "/media", "/Volumes"]:
                if os.path.isdir(base):
                    try:
                        drives += [str(Path(base) / d) for d in os.listdir(base)
                                   if os.path.isdir(Path(base) / d)]
                    except Exception:
                        pass
        self._json({"drives": drives})


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def find_free_port(start: int) -> int:
    import socket
    for port in range(start, start + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start


def main():
    import tempfile
    os.system("cls" if os.name == "nt" else "clear")
    start_time = datetime.now()
    script_dir = Path(__file__).parent

    # Prefer output next to the script; fall back to a temp dir if the
    # filesystem doesn't support SQLite (e.g. some network / SMB mounts).
    def _make_output_dir(base: Path) -> Path:
        d = base / "disk_analyzer_output"
        try:
            d.mkdir(exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            return d
        except Exception:
            return None

    output_dir = _make_output_dir(script_dir)
    if output_dir is None:
        fallback = Path(tempfile.gettempdir()) / "disk_analyzer_output"
        fallback.mkdir(exist_ok=True)
        output_dir = fallback

    log_path = output_dir / f"disk_scanner_{start_time.strftime('%Y%m%d_%H%M%S')}.log"
    log_file = open(log_path, "w", encoding="utf-8")

    def log(msg=""):
        print(msg)
        log_file.write(msg + "\n")
        log_file.flush()

    log("=" * 60)
    log(f"  {SCRIPT_NAME}  v{VERSION}")
    log(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    db_path = output_dir / DB_NAME
    log(f"\nDatabase : {db_path}")
    log(f"UI file  : {script_dir / UI_FILE}")

    Handler.db_path = db_path
    init_db(db_path).close()   # ensure schema is current

    port = find_free_port(PORT)
    url  = f"http://localhost:{port}"
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log(f"\nERROR: Cannot start server: {exc}")
        log_file.close()
        sys.exit(1)

    log(f"Server   : {url}")
    log("Opening browser… (press Ctrl+C to stop)\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    rpt = output_dir / f"disk_scanner_report_{start_time.strftime('%Y%m%d_%H%M%S')}.txt"
    with open(rpt, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write(f"  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}\n")
        f.write("=" * 60 + "\n")
        f.write(f"  Start    : {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"  Server   : {url}\n")
        f.write(f"  Database : {db_path}\n")
        f.write("=" * 60 + "\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("\nServer stopped.")
    finally:
        log_file.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
