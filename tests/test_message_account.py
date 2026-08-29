from types import SimpleNamespace

from wechat_bridge_collector.wechat_source import WeChatSource


def test_message_event_carries_owning_work_wechat_account_id():
    source = object.__new__(WeChatSource)
    source.db_dir = "/managed-wechat/sales-account/db_storage"
    source.config = SimpleNamespace(include_outgoing=True, include_text=True)

    candidate = source._build_candidate(
        (1, 1, 1_787_900_000, 0, "hello", None),
        "message/message_0.db",
        "Msg_00000000000000000000000000000000",
        "customer-contact",
        {"customer-contact": "客户"},
        {},
    )

    assert candidate is not None
    assert candidate.payload["accountId"] == "sales-account"
