"""Phase 4: supervisory window -> target run date selection.

decide_target_date maps each launchd firing (Pacific) to the night it belongs
to: 23:20-23:59 -> today, 00:00-08:50 -> yesterday, else None (skip).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from orchestrator.engine import Orchestrator, decide_target_date

PAC = ZoneInfo("America/Los_Angeles")


def _pac(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=PAC)


def test_evening_window_targets_today():
    # 23:20 and 23:50 firings on the night of the 16th -> 2026-06-16
    assert decide_target_date(_pac(2026, 6, 16, 23, 20)) == _pac(2026, 6, 16, 0, 0).date()
    assert decide_target_date(_pac(2026, 6, 16, 23, 50)) == datetime(2026, 6, 16).date()
    assert decide_target_date(_pac(2026, 6, 16, 23, 59, 59)) == datetime(2026, 6, 16).date()


def test_overnight_window_targets_yesterday():
    # 00:20 .. 08:50 on the 17th still belong to the night of the 16th
    assert decide_target_date(_pac(2026, 6, 17, 0, 0)) == datetime(2026, 6, 16).date()
    assert decide_target_date(_pac(2026, 6, 17, 2, 20)) == datetime(2026, 6, 16).date()
    assert decide_target_date(_pac(2026, 6, 17, 8, 50)) == datetime(2026, 6, 16).date()


def test_outside_window_is_skip():
    assert decide_target_date(_pac(2026, 6, 17, 8, 51)) is None   # just after window
    assert decide_target_date(_pac(2026, 6, 16, 12, 0)) is None   # midday
    assert decide_target_date(_pac(2026, 6, 16, 23, 19, 59)) is None  # just before window
    assert decide_target_date(_pac(2026, 6, 16, 9, 0)) is None


def test_supervise_skips_outside_window(make_orch, capsys):
    code = make_orch().supervise(now=_pac(2026, 6, 16, 12, 0))
    assert code == 0
    assert "outside" in capsys.readouterr().out


def test_supervise_runs_target_date_inside_window(make_orch):
    # Inside the window, supervise delegates to resume -> run for a fresh date.
    code = make_orch().supervise(now=_pac(2099, 1, 1, 23, 30))
    assert code == 0
    from orchestrator import config
    import json
    doc = json.loads((config.LEDGER_DIR / "2099-01-01.json").read_text())
    assert doc["status"] == "completed"
    assert doc["dream_run_id"].startswith("dga_20990101_")


def test_supervise_marks_missed_after_cutoff_with_no_ledger(make_orch):
    # 08:50 the morning after, no ledger started: past the 08:45 cutoff -> missed,
    # not a fresh late start.
    import json
    from orchestrator import config
    code = make_orch().supervise(now=_pac(2099, 1, 2, 8, 50))
    assert code == 1
    doc = json.loads((config.LEDGER_DIR / "2099-01-01.json").read_text())
    assert doc["status"] == "failed_terminal"  # missed marker
    assert doc["stages"]["INIT"]["status"] == "failed_terminal"
