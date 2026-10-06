"""Юнит-тесты telemetry.py (in-memory, без Redis).

    python -m tests.test_telemetry
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import billing
import telemetry


def _reset():
    billing._redis_init_failed = True
    billing._redis_client = None
    telemetry._memory.clear()


def test_stage_of():
    assert telemetry.stage_of({}) == "1 организация"
    assert telemetry.stage_of({"org_info": "x"}) == "2 донор"
    full = {k: "x" for k, _ in telemetry.STAGE_FIELDS}
    assert telemetry.stage_of(full) == "6 документ"


def test_log_and_summarize_funnel():
    _reset()
    run = asyncio.run
    run(telemetry.log_event(1, "project_started", flow="grant"))
    run(telemetry.log_event(1, "field_saved", field="org_info", text="a" * 500))
    run(telemetry.log_event(2, "project_started", flow="grant"))
    run(telemetry.log_event(1, "doc_delivered"))
    run(telemetry.log_event(1, "payment_requested", what="бюджет"))
    run(telemetry.log_event(1, "quick_reply", text="1. Пришлю ссылку"))
    run(telemetry.log_event(1, "user_msg", stage="2 донор", text="привет"))
    events = run(telemetry.load_all())
    assert len(events[1]["text"]) == 300  # длинный текст обрезается
    out = telemetry.summarize(events)
    assert "пользователей: 2" in out and "начали проект: 2" in out
    assert "1 организация: 1" in out and "получили документ: 1" in out
    assert "запросили оплату: 1" in out and "2 донор: 1" in out
    assert telemetry.summarize([]) == "Событий пока нет."


if __name__ == "__main__":
    test_stage_of()
    test_log_and_summarize_funnel()
    print("All telemetry tests passed.")
