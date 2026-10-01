"""Stdlib HTTP server for the governed plant agent demo (ops control center).

  python -m aiot_agent.web                         (local, http://127.0.0.1:8765/)
  python -m aiot_agent.web --host 0.0.0.0 --port 7860   (container / hosted demo)

Public-demo hardening:

* Every visitor gets an isolated session (single-agent report, Agent Staff
  session, approvals and audit trail). Sessions are keyed by a random id that
  the server issues (cookie plus an ``X-Demo-Session`` header the page keeps in
  sessionStorage, so it also works inside an iframe where cookies are blocked).
* Sessions live in a bounded LRU (default 200) with idle expiry (default 30
  minutes). Evicted sessions delete their temp audit directory.
* Audit logs are written to a per-session temp dir; the UI and API only show
  the file name, never an absolute server path.
* Rule-based mock model only. No API key is read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..agent import ConditionMonitoringAgent
from ..ops_report import SCENARIO_LABELS, ops_report_dict
from ..tools.data import SCENARIOS
from ..staff import (
    STAFF_SCENARIO_ORDER,
    StaffSession,
    org_chart,
    policy_panel,
    run_staff_scenario,
    staff_scenario_list,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

SESSION_COOKIE = "aiot_demo_sid"
SESSION_HEADER = "X-Demo-Session"
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_SESSIONS = _env_int("AIOT_MAX_SESSIONS", 200)
SESSION_TTL_SECONDS = _env_int("AIOT_SESSION_TTL_SECONDS", 30 * 60)
MAX_BODY_BYTES = 16 * 1024


# ---------------------------------------------------------------------------
# Per-visitor sessions
# ---------------------------------------------------------------------------


class VisitorSession:
    """Everything one visitor can see or change. Never shared across visitors."""

    def __init__(self, sid: str, base_dir_factory: Callable[[], Path], now: float) -> None:
        self.sid = sid
        self.created = now
        self.last_seen = now
        self.lock = threading.RLock()
        self.last_report: Optional[Dict[str, Any]] = None
        self.last_scenario: Optional[str] = None
        self.staff: Optional[StaffSession] = None
        self._base_dir_factory = base_dir_factory
        self._dir: Optional[Path] = None
        self.closed = False

    def audit_dir(self) -> Path:
        """Per-session temp dir for audit JSONL files (created on first use)."""
        if self._dir is None or not self._dir.exists():
            self._dir = Path(tempfile.mkdtemp(prefix="s-", dir=str(self._base_dir_factory())))
        return self._dir

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.staff = None
            self.last_report = None
            if self._dir is not None:
                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None


class SessionStore:
    """Bounded LRU of visitor sessions with idle expiry."""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        ttl_seconds: float = SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        base_dir: Optional[Path] = None,
    ) -> None:
        self.max_sessions = int(max_sessions)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, VisitorSession]" = OrderedDict()
        self._base_dir = base_dir

    # temp root shared by this store; each session gets its own subdir
    def _base(self) -> Path:
        if self._base_dir is None or not self._base_dir.exists():
            self._base_dir = Path(tempfile.mkdtemp(prefix="aiot-demo-"))
        return self._base_dir

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _expired(self, sess: VisitorSession, now: float) -> bool:
        return (now - sess.last_seen) > self.ttl_seconds

    def _purge_locked(self, now: float) -> List[VisitorSession]:
        evicted = []
        for sid in list(self._sessions.keys()):
            if self._expired(self._sessions[sid], now):
                evicted.append(self._sessions.pop(sid))
        while len(self._sessions) > self.max_sessions:
            _, sess = self._sessions.popitem(last=False)
            evicted.append(sess)
        return evicted

    def get(self, sid: Optional[str]) -> Optional[VisitorSession]:
        """Return a live session for ``sid`` (and mark it used), or None."""
        if not sid or not _SID_RE.match(sid):
            return None
        now = self._clock()
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is not None and self._expired(sess, now):
                self._sessions.pop(sid, None)
                evicted = [sess]
                sess = None
            else:
                evicted = []
                if sess is not None:
                    sess.last_seen = now
                    self._sessions.move_to_end(sid)
        for e in evicted:
            e.close()
        return sess

    def get_or_create(self, sid: Optional[str]) -> Tuple[VisitorSession, bool]:
        sess = self.get(sid)
        if sess is not None:
            return sess, False
        now = self._clock()
        new_sid = secrets.token_urlsafe(32)
        sess = VisitorSession(new_sid, self._base, now)
        with self._lock:
            self._sessions[new_sid] = sess
            evicted = self._purge_locked(now)
        for e in evicted:
            e.close()
        return sess, True

    def clear(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()


SESSIONS = SessionStore()


# Stable demo order: plant stories first, then OT / connectivity pack.
_SCENARIO_ORDER = (
    "conflict",
    "true_positive",
    "tool_error_storm",
    "stale_data",
    "social_engineering",
    "alert_fatigue",
    "parts_unavailable",
    "conflicting_work_orders",
    "dead_network",
    "unauthorized_plc_write",
    "maximo_sap_disagree",
    "soft_vs_hard_hold",
)


def _allowed_scenarios() -> tuple:
    known = set(SCENARIOS.keys())
    ordered = [sid for sid in _SCENARIO_ORDER if sid in known]
    extras = [sid for sid in SCENARIOS.keys() if sid not in _SCENARIO_ORDER]
    return tuple(ordered + extras)


def _dual_control_decisions(tool_name: str, approved: bool) -> Dict[str, bool]:
    """Fail-closed dual-control: both operator and supervisor must match the UI decision."""
    return {
        "{0}:operator".format(tool_name): bool(approved),
        "{0}:supervisor".format(tool_name): bool(approved),
    }


def _run_agent(
    visitor: VisitorSession,
    scenario: str,
    *,
    approval_decisions: Optional[Dict[str, bool]] = None,
    interactive: bool = False,
    explicit_deny: bool = False,
    explicit_approve: bool = False,
) -> Dict[str, Any]:
    """Run the single agent for one visitor. Audit goes to that visitor's temp dir."""
    with visitor.lock:
        audit_path = visitor.audit_dir() / "audit_web_{0}.jsonl".format(scenario)
        audit_path.write_text("", encoding="utf-8")  # fresh trail for this run
        agent = ConditionMonitoringAgent(
            scenario=scenario,
            audit_path=audit_path,
            interactive=interactive,
            force_mock_llm=True,
            approval_decisions=approval_decisions,
        )
        result = agent.run()
        report = ops_report_dict(
            result,
            audit_path,
            interactive=interactive,
            explicit_deny=explicit_deny,
            explicit_approve=explicit_approve,
        )
        visitor.last_report = report
        visitor.last_scenario = scenario
        return report


def _run_staff(visitor: VisitorSession, scenario: str) -> Dict[str, Any]:
    with visitor.lock:
        audit_path = visitor.audit_dir() / "audit_staff_{0}.jsonl".format(scenario)
        session = run_staff_scenario(scenario, audit_path=audit_path)
        visitor.staff = session
        return session.to_dict()


def _decide_staff(visitor: VisitorSession, approval_id: str, role: str, approved: bool) -> Dict[str, Any]:
    with visitor.lock:
        session = visitor.staff
        if session is None:
            return {"ok": False, "error": "No Agent Staff scenario running. Run one first."}
        outcome = session.decide(approval_id, role, approved)
        return {"ok": True, "result": outcome, "state": session.to_dict()}


def _staff_state(visitor: Optional[VisitorSession]) -> Optional[Dict[str, Any]]:
    if visitor is None:
        return None
    with visitor.lock:
        return visitor.staff.to_dict() if visitor.staff is not None else None


class ControlCenterHandler(BaseHTTPRequestHandler):
    server_version = "PlantAgentDemo/0.2"
    sys_version = ""
    store: SessionStore = SESSIONS

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s\n" % (fmt % args))

    # ---------------- sessions ----------------
    def _cookie_sid(self) -> Optional[str]:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                return value
        return None

    def _candidate_sids(self) -> List[str]:
        out = []
        for sid in (self.headers.get(SESSION_HEADER), self._cookie_sid()):
            if sid and _SID_RE.match(sid) and sid not in out:
                out.append(sid)
        return out

    def _lookup_session(self) -> Optional[VisitorSession]:
        for sid in self._candidate_sids():
            sess = self.store.get(sid)
            if sess is not None:
                return sess
        return None

    def _session_for_write(self) -> VisitorSession:
        sess = self._lookup_session()
        if sess is None:
            sess, _ = self.store.get_or_create(None)
        self._visitor = sess
        return sess

    def _behind_https_proxy(self) -> bool:
        # X-Forwarded-Proto may be a comma separated list when several proxies are
        # chained ("https, http"); the first entry is the client facing scheme.
        raw = self.headers.get("X-Forwarded-Proto") or ""
        first = raw.split(",")[0].strip().lower()
        return first == "https"

    def _session_headers(self) -> None:
        sess = getattr(self, "_visitor", None)
        if sess is None:
            return
        self.send_header(SESSION_HEADER, sess.sid)
        if self._cookie_sid() != sess.sid:
            attrs = "Path=/; HttpOnly; Max-Age={0}".format(int(self.store.ttl_seconds))
            if self._behind_https_proxy():
                # Hosted behind an HTTPS proxy (for example Render). The app itself
                # sees plain http, so trust the proxy header only to pick cookie flags.
                # SameSite=None lets the cookie work if the page is shown in an iframe.
                attrs += "; SameSite=None; Secure; Partitioned"
            else:
                attrs += "; SameSite=Lax"
            self.send_header("Set-Cookie", "{0}={1}; {2}".format(SESSION_COOKIE, sess.sid, attrs))

    # ---------------- responses ----------------
    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self._session_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _internal_error(self, exc: Exception) -> None:
        # Log details server-side; never echo exception text (could carry paths).
        sys.stderr.write("internal error: {0}: {1}\n".format(type(exc).__name__, exc))
        self._send_json({"ok": False, "error": "Internal error. Please run the scenario again."}, 500)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._common_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._visitor = None
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/api/status", "/api/last"):
            sess = self._lookup_session()
            self._visitor = sess
            report = None
            if sess is not None:
                with sess.lock:
                    report = sess.last_report
            if report is None:
                self._send_json({"ok": True, "report": None, "message": "No run yet"})
            else:
                self._send_json({"ok": True, "report": report})
            return

        if path in ("/api/health", "/healthz"):
            self._send_json({"ok": True, "service": "plant-agent-demo"})
            return

        if path == "/api/scenarios":
            scenarios = [
                {"id": sid, "label": SCENARIO_LABELS.get(sid, sid)}
                for sid in _allowed_scenarios()
            ]
            self._send_json({"ok": True, "scenarios": scenarios})
            return

        if path == "/api/staff/org":
            self._send_json(
                {"ok": True, "org": org_chart(), "scenarios": staff_scenario_list()}
            )
            return

        if path == "/api/staff/policy":
            # The signed control policy (owner, sign-offs, fingerprint, status, lanes).
            self._send_json({"ok": True, "policy": policy_panel()})
            return

        if path == "/api/staff/state":
            sess = self._lookup_session()
            self._visitor = sess
            self._send_json({"ok": True, "state": _staff_state(sess)})
            return

        if path.startswith("/api/"):
            self._send_json({"ok": False, "error": "Not found"}, 404)
            return

        if path == "/favicon.ico":
            path = "/favicon.svg"

        # Static files
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        root = STATIC_DIR.resolve()
        try:
            candidate = (root / rel).resolve()
        except (OSError, ValueError):
            self._send_json({"error": "Not found"}, 404)
            return
        if candidate != root and root not in candidate.parents:
            self._send_json({"error": "Not found"}, 404)
            return
        if not candidate.is_file():
            self._send_json({"error": "Not found"}, 404)
            return
        data = candidate.read_bytes()
        ctype = "text/plain; charset=utf-8"
        if candidate.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif candidate.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif candidate.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif candidate.suffix == ".svg":
            ctype = "image/svg+xml"
        elif candidate.suffix == ".png":
            ctype = "image/png"
        self._send_bytes(data, ctype)

    def do_POST(self) -> None:  # noqa: N802
        self._visitor = None
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json()
        if body is None:
            self._send_json({"ok": False, "error": "Request body too large or invalid"}, 413)
            return

        if path == "/api/run":
            scenario = body.get("scenario") or "conflict"
            if scenario not in _allowed_scenarios():
                self._send_json(
                    {
                        "ok": False,
                        "error": "Unknown scenario",
                        "allowed": list(_allowed_scenarios()),
                    },
                    400,
                )
                return
            visitor = self._session_for_write()
            try:
                report = _run_agent(visitor, scenario)
                self._send_json({"ok": True, "report": report})
            except Exception as exc:  # noqa: BLE001
                self._internal_error(exc)
            return

        if path == "/api/resolve":
            # Explicit human Approve / Deny from the control center, never auto
            approved = body.get("approved") is True
            tool_name = body.get("tool_name") or "recommend_line_shutdown"
            if not isinstance(tool_name, str) or len(tool_name) > 64:
                self._send_json({"ok": False, "error": "Invalid tool name"}, 400)
                return
            visitor = self._session_for_write()
            scenario = body.get("scenario") or visitor.last_scenario or "true_positive"
            if scenario not in _allowed_scenarios():
                self._send_json(
                    {
                        "ok": False,
                        "error": "Unknown scenario",
                        "allowed": list(_allowed_scenarios()),
                    },
                    400,
                )
                return
            try:
                # Dual-control for shutdown: both operator + supervisor must agree
                # (both True on approve, both False on deny). Fail-closed otherwise.
                decisions = _dual_control_decisions(tool_name, approved)
                report = _run_agent(
                    visitor,
                    scenario,
                    approval_decisions=decisions,
                    explicit_approve=approved,
                    explicit_deny=not approved,
                )
                self._send_json({"ok": True, "report": report})
            except Exception as exc:  # noqa: BLE001
                self._internal_error(exc)
            return

        if path == "/api/staff/run":
            scenario = body.get("scenario") or STAFF_SCENARIO_ORDER[0]
            if scenario not in STAFF_SCENARIO_ORDER:
                self._send_json(
                    {"ok": False, "error": "Unknown staff scenario", "allowed": list(STAFF_SCENARIO_ORDER)},
                    400,
                )
                return
            visitor = self._session_for_write()
            try:
                self._send_json({"ok": True, "state": _run_staff(visitor, scenario)})
            except Exception as exc:  # noqa: BLE001
                self._internal_error(exc)
            return

        if path == "/api/staff/decide":
            # Explicit human decision from the Agent Staff view; never automatic.
            approval_id = str(body.get("approval_id") or "")[:64]
            role = str(body.get("role") or "")[:64]
            approved = body.get("approved") is True
            # A decision only ever applies to this visitor's own staff session.
            visitor = self._lookup_session()
            self._visitor = visitor
            if visitor is None:
                self._send_json(
                    {"ok": False, "error": "No Agent Staff scenario running. Run one first."}, 409
                )
                return
            try:
                payload = _decide_staff(visitor, approval_id, role, approved)
                self._send_json(payload, 200 if payload.get("ok") else 409)
            except Exception as exc:  # noqa: BLE001
                self._internal_error(exc)
            return

        self._send_json({"ok": False, "error": "Not found"}, 404)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), ControlCenterHandler)
    print("Governed Plant Agent Staff Demo (simulation, mock data, rule-based)", flush=True)
    print("  Listening on http://{0}:{1}/".format(host, port), flush=True)
    print("  Agent Staff view: /#staff", flush=True)
    print("  Sessions: per visitor, max {0}, idle expiry {1}s".format(
        SESSIONS.max_sessions, int(SESSIONS.ttl_seconds)), flush=True)
    print("  Thesis: The model provides intelligence. The architecture provides control.", flush=True)

    def _on_sigterm(signum: int, frame: Any) -> None:
        # Containers (Render, Docker) stop with SIGTERM; PID 1 ignores it by default.
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, AttributeError):
        pass  # not in the main thread or no SIGTERM on this platform
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
        SESSIONS.clear()


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Governed plant agent demo web UI (simulation)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args(argv)
    serve(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
