"""The orchestrator/dream ids must encode the RUN DATE, not today's date.

Regression for the Phase 3 bug: a run on a far-future (or any non-today) date
must build dream_run_id/orchestrator_run_id whose YYYYMMDD equals the run_date,
or the Worker rejects the start (run_date != id date).
"""
import json

from orchestrator import config


def test_ids_encode_run_date_not_today(make_orch):
    rd = "2099-12-29"
    orch = make_orch()
    assert orch.run(mode="shadow", run_date=rd) == 0
    doc = json.loads((config.LEDGER_DIR / f"{rd}.json").read_text())
    # dga_YYYYMMDD_hex and pksn_YYYYMMDD_HHMMSS_hex must carry 20991229
    assert doc["dream_run_id"].startswith("dga_20991229_")
    assert doc["orchestrator_run_id"].startswith("pksn_20991229_")
    assert doc["run_date"] == rd
