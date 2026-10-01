"""Public-demo hardening: per-visitor isolation, bounded sessions, no paths, no API key."""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from aiot_agent.web import server

ROOT = Path(__file__).resolve().parent.parent
STAFF = "co2_out_of_spec_hold"


class Client:
    """Tiny HTTP client with its own cookie jar (one browser per instance)."""

    def __init__(self, base: str, use_cookies: bool = True, use_header: bool = False) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)] if use_cookies else []
        self.opener = urllib.request.build_opener(*handlers)
        self.use_header = use_header
        self.sid = None
        self.last_headers = {}

    def _req(self, method, path, body=None, extra_headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.use_header and self.sid:
            req.add_header(server.SESSION_HEADER, self.sid)
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)
        try:
            resp = self.opener.open(req, timeout=10)
            status = resp.status
        except urllib.error.HTTPError as err:
            resp = err
            status = err.code
        raw = resp.read().decode("utf-8")
        self.last_headers = dict(resp.headers.items())
        sid = resp.headers.get(server.SESSION_HEADER)
        if sid:
            self.sid = sid
        return status, json.loads(raw)

    def get(self, path, **kw):
        return self._req("GET", path, **kw)

    def post(self, path, body, **kw):
        return self._req("POST", path, body or {}, **kw)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    store = server.SessionStore(max_sessions=50, ttl_seconds=600, base_dir=tmp_path)
    monkeypatch.setattr(server.ControlCenterHandler, "store", store)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlCenterHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:{0}".format(httpd.server_address[1]), store, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        store.clear()


def test_two_visitors_do_not_see_each_others_staff_approvals(live):
    base, store, _ = live
    a, b = Client(base), Client(base)

    st, d = a.post("/api/staff/run", {"scenario": STAFF})
    assert st == 200 and d["ok"]
    appr = d["state"]["approvals"][0]
    st, d = a.post("/api/staff/decide", {"approval_id": appr["approval_id"], "role": "qa_lead", "approved": True})
    assert st == 200 and d["state"]["approvals"][0]["progress"] == "1 of 2 approvals"

    # B has no session yet: sees nothing and cannot act on A's request.
    st, d = b.get("/api/staff/state")
    assert st == 200 and d["state"] is None
    st, d = b.post("/api/staff/decide", {"approval_id": appr["approval_id"], "role": "shift_lead", "approved": True})
    assert st == 409 and d["ok"] is False

    # B runs the same scenario: gets its own fresh request (same id, separate state).
    st, d = b.post("/api/staff/run", {"scenario": STAFF})
    assert d["state"]["approvals"][0]["progress"] == "0 of 2 approvals"
    st, d = b.post("/api/staff/decide", {"approval_id": appr["approval_id"], "role": "shift_lead", "approved": True})
    assert d["state"]["approvals"][0]["progress"] == "1 of 2 approvals"
    assert d["state"]["approvals"][0]["status"] == "pending"

    # A is unaffected by B's shift_lead signature: still only qa_lead, not executed.
    st, d = a.get("/api/staff/state")
    a_appr = d["state"]["approvals"][0]
    assert a_appr["status"] == "pending" and a_appr["progress"] == "1 of 2 approvals"
    granted = [r for r in d["state"]["audit_trail"] if r["type"] == "approval_granted"]
    assert len(granted) == 1  # only A's own QA Lead signature is in A's audit trail
    assert not any(r["type"] == "tool_executed" and "EXECUTED" in r["summary"] for r in d["state"]["audit_trail"])

    assert a.sid and b.sid and a.sid != b.sid
    assert len(store) == 2


def test_single_agent_tab_is_isolated(live):
    base, _, _ = live
    a, b = Client(base), Client(base)
    st, d = a.post("/api/run", {"scenario": "true_positive"})
    assert st == 200 and d["report"]["scenario"] == "true_positive"
    st, d = a.post("/api/resolve", {"scenario": "true_positive", "tool_name": "recommend_line_shutdown", "approved": True})
    assert st == 200 and d["report"]["status_code"] == "approved"

    st, d = b.get("/api/status")
    assert d["report"] is None
    st, d = a.get("/api/status")
    assert d["report"]["status_code"] == "approved"


def test_header_only_session_works_without_cookies(live):
    """Iframe case: third-party cookies blocked, session id travels in a header."""
    base, store, _ = live
    a = Client(base, use_cookies=False, use_header=True)
    other = Client(base, use_cookies=False, use_header=True)
    st, d = a.post("/api/staff/run", {"scenario": STAFF})
    assert a.sid
    st, d = a.get("/api/staff/state")
    assert d["state"]["scenario"] == STAFF
    st, d = other.get("/api/staff/state")
    assert d["state"] is None


def test_unknown_or_forged_session_id_gets_a_fresh_session(live):
    base, store, _ = live
    a = Client(base)
    a.post("/api/staff/run", {"scenario": STAFF})
    forged = Client(base, use_cookies=False, use_header=True)
    forged.sid = "x" * 43
    st, d = forged.get("/api/staff/state")
    assert d["state"] is None
    st, d = forged.post("/api/staff/run", {"scenario": "out_of_lane_guardrails"})
    assert forged.sid != "x" * 43 and forged.sid != a.sid
    st, d = a.get("/api/staff/state")
    assert d["state"]["scenario"] == STAFF


def test_session_cookie_attributes(live):
    base, _, _ = live
    a = Client(base)
    a.post("/api/staff/run", {"scenario": STAFF})
    cookie = a.last_headers.get("Set-Cookie", "")
    assert cookie.startswith(server.SESSION_COOKIE + "=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    b = Client(base)
    b.post("/api/staff/run", {"scenario": STAFF}, extra_headers={"X-Forwarded-Proto": "https"})
    cookie = b.last_headers.get("Set-Cookie", "")
    assert "SameSite=None" in cookie and "Secure" in cookie
    # Plain http seen internally (no proxy header): never mark the cookie Secure.
    assert "Secure" not in a.last_headers.get("Set-Cookie", "")
    # Chained proxies send a list; the first entry is the client facing scheme.
    c = Client(base)
    c.post("/api/staff/run", {"scenario": STAFF}, extra_headers={"X-Forwarded-Proto": "https, http"})
    assert "Secure" in c.last_headers.get("Set-Cookie", "")
    d = Client(base)
    d.post("/api/staff/run", {"scenario": STAFF}, extra_headers={"X-Forwarded-Proto": "http"})
    cookie = d.last_headers.get("Set-Cookie", "")
    assert "SameSite=Lax" in cookie and "Secure" not in cookie


def test_read_only_endpoints_do_not_create_sessions(live):
    base, store, _ = live
    c = Client(base)
    for path in ("/", "/api/health", "/healthz", "/api/scenarios", "/api/staff/org", "/api/staff/policy",
                 "/api/staff/state", "/api/status"):
        if path == "/":
            urllib.request.urlopen(base + path, timeout=5).read()
        else:
            c.get(path)
    assert len(store) == 0


def test_no_absolute_paths_in_api_output(live):
    base, _, tmp_path = live
    a = Client(base)
    _, staff = a.post("/api/staff/run", {"scenario": STAFF})
    _, single = a.post("/api/run", {"scenario": "conflict"})
    assert staff["state"]["audit_path"] == "audit_staff_{0}.jsonl".format(STAFF)
    assert single["report"]["audit_path"] == "audit_web_conflict.jsonl"
    blob = json.dumps(staff) + json.dumps(single)
    for needle in (str(tmp_path), "/tmp/", "/Users/", "/home/", str(ROOT)):
        assert needle not in blob, needle


def test_oversized_body_rejected(live):
    base, store, _ = live
    req = urllib.request.Request(base + "/api/staff/run", data=b"{" + b" " * (server.MAX_BODY_BYTES + 10) + b"}",
                                 method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 413
    assert len(store) == 0


# ---------------------------------------------------------------------------
# Bounded memory: LRU cap and idle expiry
# ---------------------------------------------------------------------------


def test_session_store_lru_cap_and_cleanup(tmp_path):
    store = server.SessionStore(max_sessions=3, ttl_seconds=600, base_dir=tmp_path)
    sessions = [store.get_or_create(None)[0] for _ in range(3)]
    dirs = [s.audit_dir() for s in sessions]
    assert all(d.exists() for d in dirs)
    store.get(sessions[0].sid)  # touch: sessions[1] is now least recently used
    store.get_or_create(None)
    assert len(store) == 3
    assert store.get(sessions[1].sid) is None
    assert not dirs[1].exists()
    assert store.get(sessions[0].sid) is sessions[0]
    for _ in range(10):
        store.get_or_create(None)
    assert len(store) == 3
    store.clear()
    assert len(store) == 0


def test_session_store_idle_expiry(tmp_path):
    now = [1000.0]
    store = server.SessionStore(max_sessions=10, ttl_seconds=60, clock=lambda: now[0], base_dir=tmp_path)
    s, _ = store.get_or_create(None)
    d = s.audit_dir()
    now[0] += 30
    assert store.get(s.sid) is s
    now[0] += 61
    assert store.get(s.sid) is None
    assert not d.exists()
    assert len(store) == 0


def test_session_ids_are_random_and_well_formed(tmp_path):
    store = server.SessionStore(max_sessions=100, ttl_seconds=60, base_dir=tmp_path)
    ids = {store.get_or_create(None)[0].sid for _ in range(50)}
    assert len(ids) == 50
    assert all(server._SID_RE.match(i) and len(i) >= 40 for i in ids)
    store.clear()


# ---------------------------------------------------------------------------
# Public build: mock model only, no API key path
# ---------------------------------------------------------------------------


def test_llm_is_always_mock_even_with_api_key(monkeypatch):
    from aiot_agent.llm import get_llm
    from aiot_agent.llm.mock import MockLLM

    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert isinstance(get_llm(force_mock=False), MockLLM)


def test_package_never_reads_api_keys():
    for f in (ROOT / "aiot_agent").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "API_KEY" not in text, f
        assert "import anthropic" not in text, f


def test_no_em_or_en_dashes_anywhere():
    exts = {".py", ".md", ".js", ".html", ".css", ".json", ".toml", ".txt"}
    for f in ROOT.rglob("*"):
        if not f.is_file() or f.suffix not in exts:
            continue
        if any(part in (".venv", "node_modules", ".git") or part.endswith(".egg-info") for part in f.parts):
            continue
        text = f.read_text(encoding="utf-8")
        assert "\u2014" not in text and "\u2013" not in text, f
