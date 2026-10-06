"""Юнит-тесты оплаты по чеку (receipt_payments.py): Redis не поднимаем —
используется in-memory запасной вариант, бот подменяется заглушкой.

    python -m tests.test_receipt_payments
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import payments
import receipt_payments as rp


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def get_chat(self, chat_id):
        raise RuntimeError("no network")


def _setup():
    config.PAYMENT_MODE = "receipt"
    config.OWNER_CHAT_ID = "999"
    config.PAYMENT_REQUISITES = "Карта 0000"
    rp._memory_wait.clear()
    billing_client = rp.billing
    billing_client._redis_init_failed = True
    billing_client._redis_client = None


def test_payload_codes_and_prices():
    assert rp.code_for_payload(payments.PAID_FILE_EXPORT_PAYLOAD) == "pf"
    assert rp.code_for_payload(payments.PAID_BUDGET_EXPORT_PAYLOAD) == "bf"
    assert rp.code_for_payload("nope") is None
    assert rp.price_kgs("pf") == 200 and rp.price_kgs("bf") == 90


def test_invoice_in_receipt_mode_asks_for_receipt_and_notifies_owner():
    _setup()
    bot = FakeBot()
    asyncio.run(payments.send_budget_export_invoice(bot, 42))
    to_user = [t for c, t in bot.sent if c == 42][0]
    assert "90 сом" in to_user and "Карта 0000" in to_user and "чека" in to_user
    assert any(c == "999" and "запросил оплату" in t for c, t in bot.sent)
    assert asyncio.run(rp.get_waiting(42)) == "bf"


def test_missing_requisites_warn_owner():
    _setup()
    config.PAYMENT_REQUISITES = ""
    bot = FakeBot()
    asyncio.run(payments.send_file_export_invoice(bot, 7))
    assert any("PAYMENT_REQUISITES не задан" in t for c, t in bot.sent if c == "999")


def test_clear_waiting_and_double_decision_guard():
    _setup()
    asyncio.run(rp.set_waiting(5, "pf"))
    asyncio.run(rp.clear_waiting(5))
    assert asyncio.run(rp.get_waiting(5)) is None
    assert asyncio.run(rp.claim_decision(1, 10)) is True
    assert asyncio.run(rp.claim_decision(1, 10)) is False


def test_is_owner():
    _setup()
    assert rp.is_owner(999) and not rp.is_owner(1)


if __name__ == "__main__":
    test_payload_codes_and_prices()
    test_invoice_in_receipt_mode_asks_for_receipt_and_notifies_owner()
    test_missing_requisites_warn_owner()
    test_clear_waiting_and_double_decision_guard()
    test_is_owner()
    print("All receipt payment tests passed.")
