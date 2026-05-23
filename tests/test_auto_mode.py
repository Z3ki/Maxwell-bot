from datetime import datetime, timedelta, timezone

from bot import _recent_auto_reply_count


def test_recent_auto_reply_count_counts_only_bot_names_in_window():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    memory = [
        {"author": "Maxwell", "content": "old", "timestamp": (now - timedelta(minutes=20)).isoformat()},
        {"author": "Alice", "content": "hi", "timestamp": (now - timedelta(minutes=2)).isoformat()},
        {"author": "Maxwell", "content": "one", "timestamp": (now - timedelta(minutes=1)).isoformat()},
        {"author": "Bot", "content": "two", "timestamp": now.isoformat()},
    ]

    assert _recent_auto_reply_count(memory, {"Maxwell", "Bot"}, 10, now) == 2


def test_recent_auto_reply_count_handles_missing_timestamps():
    memory = [
        {"author": "Maxwell", "content": "missing timestamp"},
        {"author": "Alice", "content": "hi"},
    ]

    assert _recent_auto_reply_count(memory, {"Maxwell"}, 10, datetime.now(timezone.utc)) == 1
