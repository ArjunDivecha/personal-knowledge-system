"""Phase 2: HttpDreamClient + engine integration with a Worker-backed client."""
import json

import pytest

from orchestrator import config
from orchestrator import dream as D


class FakeTransport:
    """Records requests and returns scripted (status_code, payload) responses."""
    def __init__(self):
        self.calls = []
        self.start_response = (202, {"accepted": True, "state": "accepted",
                                     "executed_mode": "shadow",
                                     "dream_run_id": "dga_20260616_ab12cd34"})
        self.status_response = (404, {})

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        if url.endswith("/start") or "/start" in url:
            return self.start_response
        return self.status_response


def _client(transport, **kw):
    return D.HttpDreamClient(base_url="https://mcp.test", token="optok",
                             transport=transport, clock=lambda: 1781650800.0, **kw)


def test_start_sends_operator_bearer_token():
    t = FakeTransport()
    c = _client(t)
    c.start(dream_run_id="dga_20260616_ab12cd34",
            orchestrator_run_id="pksn_20260616_230000_ab12cd34",
            run_date="2026-06-16", mode="shadow", fencing_token=17)
    call = t.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "https://mcp.test/ops/dream/scheduled_governed/start"
    assert call["headers"]["Authorization"] == "Bearer optok"
    # body carries the cron + scheduled_time the Worker requires
    assert call["body"]["run_id"] == "dga_20260616_ab12cd34"
    assert call["body"]["cron"] == D.DEFAULT_ORCH_CRON
    assert call["body"]["scheduled_time"] == 1781650800000


def test_status_maps_404_to_none():
    t = FakeTransport()
    t.status_response = (404, {"error": "not_found"})
    assert _client(t).status("dga_20260616_ab12cd34") is None


def test_status_preserves_executed_mode_and_applied_count():
    t = FakeTransport()
    t.status_response = (200, {"state": "terminal", "status": "completed_shadow",
                               "executed_mode": "shadow", "applied_count": 0})
    st = _client(t).status("dga_20260616_ab12cd34")
    assert st["executed_mode"] == "shadow" and st["applied_count"] == 0


def test_non_2xx_status_raises_unless_terminal_rejection():
    t = FakeTransport()
    t.status_response = (500, {"error": "boom"})
    with pytest.raises(D.DreamClientError):
        _client(t).status("dga_20260616_ab12cd34")
    # a terminal rejection payload is returned, not raised
    t.status_response = (409, {"status": "rejected_superseded"})
    assert _client(t).status("dga_20260616_ab12cd34")["status"] == "rejected_superseded"


def test_start_returns_rejection_payload_without_raising():
    t = FakeTransport()
    t.start_response = (403, {"accepted": False, "error": "rejected_live_disabled"})
    out = _client(t).start(dream_run_id="dga_20260616_ab12cd34",
                           orchestrator_run_id="pksn_20260616_230000_ab12cd34",
                           run_date="2026-06-16", mode="live", fencing_token=17)
    assert out["error"] == "rejected_live_disabled"


class _ScriptedHttpDream:
    """A DreamClient that emulates the Worker's start/status over a fake store,
    used to drive the engine end-to-end without network."""
    def __init__(self):
        self._statuses = {}

    def start(self, *, dream_run_id, orchestrator_run_id, run_date, mode, fencing_token):
        self._statuses[dream_run_id] = {
            "schema_version": 1, "state": "terminal", "status": "completed_shadow",
            "requested_mode": mode, "executed_mode": "shadow",
            "dream_run_id": dream_run_id, "orchestrator_run_id": orchestrator_run_id,
            "run_date": run_date, "applied_count": 0, "held_count": 0}
        return {"accepted": True, "state": "accepted", "executed_mode": "shadow",
                "dream_run_id": dream_run_id}

    def status(self, dream_run_id):
        return self._statuses.get(dream_run_id)


def test_engine_completes_with_http_backed_dream_client(make_orch):
    orch = make_orch(dream_client=_ScriptedHttpDream())
    assert orch.run(mode="shadow", run_date="2026-06-16") == 0
    doc = json.loads((config.LEDGER_DIR / "2026-06-16.json").read_text())
    assert doc["status"] == "completed"
    assert doc["stages"]["DREAM_VERIFY"]["status"] == "completed"


class _RejectingDream:
    def start(self, *, dream_run_id, orchestrator_run_id, run_date, mode, fencing_token):
        self.id = dream_run_id
        return {"accepted": True, "state": "accepted", "executed_mode": "shadow"}

    def status(self, dream_run_id):
        # Worker terminally rejected this run (e.g. superseded).
        return {"state": "terminal", "status": "rejected_superseded",
                "executed_mode": "shadow", "applied_count": 0,
                "dream_run_id": dream_run_id}


def test_rejected_worker_status_becomes_failed_terminal(make_orch):
    orch = make_orch(dream_client=_RejectingDream())
    assert orch.run(mode="shadow", run_date="2026-06-16") == 1
    doc = json.loads((config.LEDGER_DIR / "2026-06-16.json").read_text())
    assert doc["stages"]["DREAM_VERIFY"]["status"] == "failed_terminal"
    assert doc["status"] == "failed_terminal"


class _StartRejectedDream:
    def start(self, *, dream_run_id, orchestrator_run_id, run_date, mode, fencing_token):
        return {"accepted": False, "error": "date_locked",
                "blocked_by": {"dream_run_id": "dga_20260616_deadbeef"}}

    def status(self, dream_run_id):
        return None


def test_worker_start_rejection_stops_at_dream_start(make_orch):
    orch = make_orch(dream_client=_StartRejectedDream())
    assert orch.run(mode="shadow", run_date="2026-06-16") == 1
    doc = json.loads((config.LEDGER_DIR / "2026-06-16.json").read_text())
    assert doc["status"] == "failed_terminal"
    assert doc["stages"]["DREAM_START"]["status"] == "failed_terminal"
    assert doc["stages"]["DREAM_START"]["counts"]["reason"] == "date_locked"
    assert "DREAM_WAIT" not in doc["stages"]
