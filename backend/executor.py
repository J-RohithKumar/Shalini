
# backend/executor.py
import os
import sys
import subprocess
import time
import shutil
import platform
import signal
from typing import Tuple, Optional, Dict, Any
from pathlib import Path
from datetime import datetime
from uuid import uuid4
import re

# --- Limits & platform ---
TIMEOUT_SECONDS = 7                 # wall timeout for spawned processes
MAX_OUTPUT_BYTES = 1_000_000        # cap stdout/stderr returned to client
CPU_SECONDS = 4                     # POSIX RLIMIT_CPU for child process
MAX_MEM_MB = 256                    # POSIX RLIMIT_AS (address space) cap
FILE_SIZE_MB = 10                   # POSIX RLIMIT_FSIZE (max file size)

IS_WINDOWS = platform.system().lower().startswith("win")

# ---------------------------------------------------------------------
# Resource limits for child processes (POSIX only)
# ---------------------------------------------------------------------
def _limit_resources():
    """Apply POSIX resource limits inside the child before exec."""
    if IS_WINDOWS:
        return
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
        max_bytes = MAX_MEM_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_MB * 1024 * 1024,
                                                  FILE_SIZE_MB * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        # Additional hardening
        if hasattr(resource, "RLIMIT_NOFILE"):
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        # If resource isn't available or fails, continue without hard limits.
        pass


def _which(cmd: str) -> Optional[str]:
    from shutil import which
    return which(cmd)


# ---------------------------------------------------------------------
# One-shot process runner (used by /api/run)
# ---------------------------------------------------------------------
def _run(cmd, cwd, env, stdin_bytes, timeout):
    """
    Run a process with sandbox limits. On timeout, kill *entire* process group.
    Returns (exit_code, stdout_bytes, stderr_bytes).
    """
    creationflags = 0
    start_new_session = False
    preexec_fn = None
    close_fds = True

    if IS_WINDOWS:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        preexec_fn = None
        start_new_session = False
    else:
        preexec_fn = _limit_resources
        start_new_session = True

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=preexec_fn,
        env=env,
        start_new_session=start_new_session,
        creationflags=creationflags,
        close_fds=close_fds,
    )

    try:
        out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        # Attempt graceful group termination, then hard kill
        try:
            if IS_WINDOWS:
                try:
                    proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
                    time.sleep(0.1)
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        capture_output=True, check=False
                    )
                except Exception:
                    pass
            else:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        finally:
            return 124, b"", b"Execution timed out"


# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------
def _list_classes(classes_dir: Path) -> str:
    if not classes_dir.exists():
        return f"(no classes dir: {classes_dir})"
    lines = [f"Classes in {classes_dir}:"]
    any_found = False
    for p in sorted(classes_dir.rglob("*.class")):
        any_found = True
        try:
            st = p.stat()
            sz = st.st_size
            ts = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
            rel = p.relative_to(classes_dir)
            lines.append(f"  {rel}  [{sz} bytes, mtime={ts}]")
        except Exception as e:
            lines.append(f"  {p}  [stat error: {e}]")
    if not any_found:
        lines.append("  (NONE)")
    return "\n".join(lines)


def _make_workspace(language: str) -> Path:
    """
    Create a run workspace under the project folder instead of %TEMP%.
    Layout:
      <project_root>/.run/<lang>_<uuid>/
    """
    project_root = Path(__file__).resolve().parents[1]
    run_root = project_root / ".run"
    run_root.mkdir(parents=True, exist_ok=True)
    ws = run_root / f"{language}_{uuid4().hex[:8]}"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _detect_java_package_and_class(code: str) -> Tuple[Optional[str], str]:
    """
    Return (package_name or None, public_class_name or 'Main' fallback).
    """
    pkg = None
    m_pkg = re.search(r'^\s*package\s+([a-zA-Z_][\w\.]*)\s*;', code, flags=re.MULTILINE)
    if m_pkg:
        pkg = m_pkg.group(1).strip()

    m_pub = re.search(r'^\s*public\s+class\s+([A-Za-z_]\w*)\b', code, flags=re.MULTILINE)
    if m_pub:
        cls = m_pub.group(1)
        return pkg, cls

    if re.search(r'\bclass\s+Main\b', code):
        return pkg, "Main"

    return pkg, "Main"


def _build_pg_dsn() -> Optional[str]:
    """
    Prefer PG_DSN if provided. Otherwise build a DSN from separate env vars:
      PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
    Returns None if not enough info is present.
    """
    dsn = os.getenv("PG_DSN")
    if dsn and dsn.strip():
        return dsn.strip()

    host = os.getenv("PG_HOST")
    port = os.getenv("PG_PORT") or "5432"
    db   = os.getenv("PG_DB")
    user = os.getenv("PG_USER")
    pwd  = os.getenv("PG_PASSWORD")

    # Need at least host, db, user, password to build a useful DSN
    if all([host, db, user, pwd]):
        return f"dbname={db} user={user} password={pwd} host={host} port={port}"

    return None


# ---------------------------------------------------------------------
# One-shot executor (used by /api/run)
# ---------------------------------------------------------------------
def run_code(language: str, code: str, stdin_input: Optional[str] = None) -> Tuple[bool, str, str, int, int]:
    language = (language or "").strip().lower()
    # Only python/java/javascript/postgres now.
    if language not in {"python", "java", "javascript", "postgres", "postgresql"}:
        return False, "", f"Unsupported language: {language}", 127, 0

    workdir = _make_workspace(language)
    start = time.time()

    try:
        # Clean env that can inject JVM args/classpath
        env = os.environ.copy()
        for var in ("CLASSPATH", "_JAVA_OPTIONS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS"):
            env.pop(var, None)
        env.setdefault("PYTHONWARNINGS", "ignore")

        compile_cmd = None
        run_attempts = []
        stdout_data = b""
        stderr_data = b""
        exit_code = 0

        # ---------- PYTHON ----------
        if language == "python":
            script = workdir / "main.py"
            script.write_text(code, encoding="utf-8")
            pyexe = sys.executable if sys.executable else "python3"
            # Add -u (unbuffered) so prompts print before input wait
            run_attempts = [([pyexe, "-u", "-B", str(script)], str(workdir))]

        # ---------- JAVASCRIPT ----------
        elif language == "javascript":
            if not _which("node"):
                return False, "", "Node.js (node) not found on PATH", 127, 0
            script = workdir / "main.js"
            script.write_text(code, encoding="utf-8")
            run_attempts = [(["node", "--max-old-space-size=128", str(script)], str(workdir))]

        # ---------- JAVA ----------
        elif language == "java":
            javac_path = _which("javac")
            java_path = _which("java")
            if not javac_path:
                return False, "", "Java compiler (javac) not found on PATH", 127, 0
            if not java_path:
                return False, "", "Java runtime (java) not found on PATH", 127, 0

            jdk_bin = Path(javac_path).parent
            java_bin = jdk_bin / ("java.exe" if IS_WINDOWS else "java")
            if not java_bin.exists():
                java_bin = Path(java_path)

            package_name, class_name = _detect_java_package_and_class(code)

            src_java = workdir / f"{class_name}.java"
            src_java.write_text(code, encoding="utf-8")

            classes_a = workdir / "classes_a"
            classes_b = workdir / "classes_b"
            classes_a.mkdir(parents=True, exist_ok=True)

            javac_bin = str(jdk_bin / ("javac.exe" if IS_WINDOWS else "javac"))
            compile_cmd = [javac_bin, "-encoding", "UTF-8", "-d", str(classes_a), str(src_java)]

            main_class = f"{package_name}.{class_name}" if package_name else class_name
            expected_class = classes_a / (main_class.replace(".", "/") + ".class")

            # Run attempts: try from classes_b with "." cp, then from workdir with classes_b cp
            run_attempts = [
                ([str(java_bin), "-Xmx128m", "-cp", ".", main_class], str(classes_b)),
                ([str(java_bin), "-Xmx128m", "-cp", str(classes_b), main_class], str(workdir)),
            ]

        # ---------- SQL (PostgreSQL/EDB) ----------
        elif language in {"postgres", "postgresql"}:
            try:
                import psycopg2
            except Exception as e:
                runtime_ms = int((time.time() - start) * 1000)
                return False, "", f"PostgreSQL driver not available: {e}. Install psycopg2-binary.", 127, runtime_ms

            import csv
            from io import StringIO

            sql_text = (code or "").strip()
            if not sql_text:
                runtime_ms = int((time.time() - start) * 1000)
                return False, "", "No SQL provided", 1, runtime_ms

            dsn = _build_pg_dsn()
            if not dsn:
                runtime_ms = int((time.time() - start) * 1000)
                return False, "", "Postgres connection not configured. Set PG_DSN or PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD.", 1, runtime_ms

            try:
                conn = psycopg2.connect(dsn)
                conn.autocommit = True
                cur = conn.cursor()

                # Enforce a server-side statement timeout for safety (ms)
                try:
                    cur.execute(f"SET statement_timeout = {int(TIMEOUT_SECONDS * 1000)};")
                except Exception:
                    # Some servers/permissions may not allow setting; continue without it
                    pass

                # Naive split by ';' (good for simple snippets)
                statements = [s.strip() for s in sql_text.split(";") if s.strip()]
                if not statements:
                    runtime_ms = int((time.time() - start) * 1000)
                    return False, "", "No SQL statements found", 1, runtime_ms

                # Execute all but last (DDL/DML)
                for stmt in statements[:-1]:
                    cur.execute(stmt)

                # Handle last statement: SELECT => CSV; else => rows affected
                last = statements[-1]
                if last.lower().startswith("select"):
                    cur.execute(last)
                    cols = [d[0] for d in (cur.description or [])]
                    rows = cur.fetchall()

                    buf = StringIO()
                    writer = csv.writer(buf)
                    if cols:
                        writer.writerow(cols)
                    for r in rows:
                        writer.writerow(r)
                    out_bytes = buf.getvalue().encode("utf-8")

                    stdout_data = out_bytes[:MAX_OUTPUT_BYTES]
                    if len(out_bytes) > MAX_OUTPUT_BYTES:
                        stdout_data += b"\n...[truncated]"
                    stderr_data = b""
                    exit_code = 0
                else:
                    cur.execute(last)
                    affected = cur.rowcount if cur.rowcount not in (-1, None) else 0
                    msg = f"OK. Rows affected: {affected}"
                    stdout_data = msg.encode("utf-8")
                    stderr_data = b""
                    exit_code = 0

            except Exception as e:
                stdout_data = b""
                stderr_data = str(e).encode("utf-8")
                exit_code = 1
            finally:
                try:
                    cur.close()
                    conn.close()
                except Exception:
                    pass

            runtime_ms = int((time.time() - start) * 1000)
            ok = (exit_code == 0)
            return ok, stdout_data.decode("utf-8", errors="ignore"), stderr_data.decode("utf-8", errors="ignore"), exit_code, runtime_ms

        # ---------- Compile (Java only) ----------
        if compile_cmd:
            c_code, c_out, c_err = _run(compile_cmd, str(workdir), env, b"", TIMEOUT_SECONDS)
            if c_code != 0:
                return False, c_out.decode(errors="ignore"), c_err.decode(errors="ignore"), c_code, int((time.time() - start) * 1000)

            if IS_WINDOWS:
                time.sleep(0.15)

            if not expected_class.exists():
                listing = _list_classes(classes_a)
                hint = f"[compile ok] but expected class not found: {expected_class}\n\n{listing}"
                return False, c_out.decode(errors="ignore"), hint, 1, int((time.time() - start) * 1000)

            classes_b = workdir / "classes_b"
            if classes_b.exists():
                shutil.rmtree(classes_b, ignore_errors=True)
            shutil.copytree(classes_a, classes_b)

        # ---------- Run ----------
        stdin_bytes = (stdin_input or "").encode("utf-8")
        attempts_log = []
        success = False

        for cmd, cwd in run_attempts:
            rc, out, err = _run(cmd, cwd, env, stdin_bytes, TIMEOUT_SECONDS)
            attempts_log.append({
                "cmd": " ".join(cmd),
                "cwd": cwd,
                "exit": rc,
                "stderr": (err or b"").decode(errors="ignore"),
            })
            if rc == 0:
                stdout_data = out[:MAX_OUTPUT_BYTES]
                stderr_data = err[:MAX_OUTPUT_BYTES]
                if len(out) > MAX_OUTPUT_BYTES:
                    stdout_data += b"\n...[truncated]"
                if len(err) > MAX_OUTPUT_BYTES:
                    stderr_data += b"\n...[truncated]"
                exit_code = rc
                success = True
                break
            else:
                msg = (err or b"").decode(errors="ignore")
                if ("ClassNotFoundException" not in msg) and ("Could not find or load main class" not in msg):
                    stdout_data = out[:MAX_OUTPUT_BYTES]
                    stderr_data = err[:MAX_OUTPUT_BYTES]
                    if len(out) > MAX_OUTPUT_BYTES:
                        stdout_data += b"\n...[truncated]"
                    if len(err) > MAX_OUTPUT_BYTES:
                        stderr_data += b"\n...[truncated]"
                    exit_code = rc
                    success = False
                    break

        if language == "java" and not success:
            listing_a = _list_classes(workdir / "classes_a")
            listing_b = _list_classes(workdir / "classes_b")
            detail = ["Java run attempts (with local workspace):"]
            for a in attempts_log:
                detail.append(
                    f"Attempt:\n"
                    f"  cmd: {a['cmd']}\n"
                    f"  cwd: {a['cwd']}\n"
                    f"  exit: {a['exit']}\n"
                    f"  stderr:\n{a['stderr']}\n"
                )
            detail.append(listing_a)
            detail.append(listing_b)
            stderr_data = "\n".join(detail).encode("utf-8")
            exit_code = attempts_log[-1]["exit"] if attempts_log else 1

        runtime_ms = int((time.time() - start) * 1000)
        ok = (exit_code == 0)
        return ok, stdout_data.decode("utf-8", errors="ignore"), stderr_data.decode("utf-8", errors="ignore"), exit_code, runtime_ms

    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


# =====================================================================
# Interactive session runner (used for true prompt→wait→input flows)
# =====================================================================
from threading import Thread, Lock
from queue import Queue, Empty

_sessions: Dict[str, "InteractiveSession"] = {}
_sessions_lock = Lock()


class InteractiveSession:
    """
    Maintains a live process with stdin/stdout/stderr pipes, reading output on
    background threads and allowing incremental stdin writes. Cleans up workspace
    on kill. Intended for Python/JavaScript/Java.
    """
    def __init__(self, language: str, cmd, cwd: str, env: Dict[str, str], workdir: Path):
        self.language = language
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.workdir = workdir
        self.exit_code: Optional[int] = None
        self.start_ts = time.time()
        self.last_activity = time.time()

        creationflags = 0
        start_new_session = False
        preexec_fn = None
        close_fds = True

        if IS_WINDOWS:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            preexec_fn = None
            start_new_session = False
        else:
            preexec_fn = _limit_resources
            start_new_session = True

        # Unbuffered pipes
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=preexec_fn,
            env=env,
            start_new_session=start_new_session,
            creationflags=creationflags,
            close_fds=close_fds,
            bufsize=0,
        )

        self.out_q: Queue = Queue()
        self.err_q: Queue = Queue()

        self._start_reader_threads()
        self._start_exit_waiter()
        self._start_idle_watchdog()

    def _start_reader_threads(self):
        def pump(stream, q):
            try:
                while True:
                    chunk = stream.read(1 << 14)  # 16KB
                    if not chunk:
                        break
                    q.put(chunk)
                    self.last_activity = time.time()
            except Exception:
                pass
            finally:
                # EOF marker
                q.put(None)

        Thread(target=pump, args=(self.proc.stdout, self.out_q), daemon=True).start()
        Thread(target=pump, args=(self.proc.stderr, self.err_q), daemon=True).start()

    def _start_exit_waiter(self):
        def wait_exit():
            try:
                rc = self.proc.wait()
                self.exit_code = rc
            except Exception:
                self.exit_code = self.exit_code or 1
        Thread(target=wait_exit, daemon=True).start()

    def _start_idle_watchdog(self):
        # Kills the session if totally idle for > TIMEOUT_SECONDS
        def watch():
            while self.exit_code is None:
                time.sleep(0.5)
                if time.time() - self.last_activity > TIMEOUT_SECONDS:
                    self.kill()
                    break
        Thread(target=watch, daemon=True).start()

    def write_stdin(self, data: str):
        if self.proc.stdin and self.exit_code is None:
            self.proc.stdin.write(data.encode("utf-8"))
            self.proc.stdin.flush()
            self.last_activity = time.time()

    def _drain_queue(self, q: Queue, cap: int) -> bytes:
        buf = bytearray()
        try:
            while len(buf) < cap:
                chunk = q.get_nowait()
                if chunk is None:
                    # EOF reached
                    break
                buf.extend(chunk)
        except Empty:
            pass
        return bytes(buf[:cap])

    def read_chunks(self) -> Dict[str, Any]:
        out_bytes = self._drain_queue(self.out_q, MAX_OUTPUT_BYTES)
        err_bytes = self._drain_queue(self.err_q, MAX_OUTPUT_BYTES)
        self.last_activity = time.time() if (out_bytes or err_bytes) else self.last_activity
        return {
            "stdout": out_bytes.decode("utf-8", errors="ignore"),
            "stderr": err_bytes.decode("utf-8", errors="ignore"),
            "exit_code": self.exit_code,
        }

    def kill(self):
        try:
            if IS_WINDOWS:
                try:
                    self.proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
                    time.sleep(0.1)
                except Exception:
                    pass
                try:
                    self.proc.kill()
                except Exception:
                    pass
                try:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                                   capture_output=True, check=False)
                except Exception:
                    pass
            else:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
        finally:
            self.exit_code = self.exit_code or 137
            try:
                shutil.rmtree(self.workdir, ignore_errors=True)
            except Exception:
                pass


# ---------------------------------------------------------------------
# Interactive API helpers (to be used by run_service.py)
# ---------------------------------------------------------------------
def start_interactive(language: str, code: str) -> str:
    """
    Start an interactive session and return session_id.
    Supports: python, javascript, java
    """
    language = (language or "").strip().lower()
    if language not in {"python", "java", "javascript"}:
        raise ValueError("Interactive supported only for python/java/javascript")

    workdir = _make_workspace(language)
    env = os.environ.copy()
    for var in ("CLASSPATH", "_JAVA_OPTIONS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS"):
        env.pop(var, None)
    env.setdefault("PYTHONWARNINGS", "ignore")

    if language == "python":
        script = workdir / "main.py"
        script.write_text(code, encoding="utf-8")
        pyexe = sys.executable if sys.executable else "python3"
        cmd = [pyexe, "-u", "-B", str(script)]  # -u => unbuffered I/O
        cwd = str(workdir)

    elif language == "javascript":
        if not _which("node"):
            raise RuntimeError("Node.js (node) not found on PATH")
        script = workdir / "main.js"
        script.write_text(code, encoding="utf-8")
        cmd = ["node", "--max-old-space-size=128", str(script)]
        cwd = str(workdir)

    elif language == "java":
        javac_path = _which("javac")
        java_path = _which("java")
        if not javac_path or not java_path:
            raise RuntimeError("Java (javac/java) not found on PATH")

        jdk_bin = Path(javac_path).parent
        java_bin = jdk_bin / ("java.exe" if IS_WINDOWS else "java")
        if not java_bin.exists():
            java_bin = Path(java_path)

        package_name, class_name = _detect_java_package_and_class(code)
        src_java = workdir / f"{class_name}.java"
        src_java.write_text(code, encoding="utf-8")

        classes_dir = workdir / "classes"
        classes_dir.mkdir(parents=True, exist_ok=True)
        javac_bin = str(jdk_bin / ("javac.exe" if IS_WINDOWS else "javac"))
        c_rc, c_out, c_err = _run(
            [javac_bin, "-encoding", "UTF-8", "-d", str(classes_dir), str(src_java)],
            str(workdir),
            env,
            b"",
            TIMEOUT_SECONDS
        )
        if c_rc != 0:
            raise RuntimeError((c_err or b"").decode("utf-8", errors="ignore") or "javac failed")

        main_class = f"{package_name}.{class_name}" if package_name else class_name
        cmd = [str(java_bin), "-Xmx128m", "-cp", str(classes_dir), main_class]
        cwd = str(workdir)

    sess = InteractiveSession(language, cmd, cwd, env, workdir)
    sess_id = uuid4().hex[:12]
    with _sessions_lock:
        _sessions[sess_id] = sess
    return sess_id


def send_stdin(sess_id: str, data: str) -> bool:
    """
    Send a chunk of stdin to a session.
    Example data: "10\n"
    """
    with _sessions_lock:
        sess = _sessions.get(sess_id)
    if not sess:
        raise KeyError("session not found")
    sess.write_stdin(data)
    return True


def poll_output(sess_id: str) -> Dict[str, Any]:
    """
    Non-blocking poll: returns any accumulated stdout/stderr and exit_code if finished.
    """
    with _sessions_lock:
        sess = _sessions.get(sess_id)
    if not sess:
        raise KeyError("session not found")
    return sess.read_chunks()


def stop_session(sess_id: str) -> bool:
    """
    Kill a session and cleanup its workspace.
    """
    with _sessions_lock:
        sess = _sessions.pop(sess_id, None)
    if sess:
        sess.kill()
        return True
    return False


__all__ = [
    "run_code",
    "start_interactive",
    "send_stdin",
    "poll_output",
    "stop_session",
]
