"""Conversation history helpers."""

from moveworks_capi.conversations import replies_after_input


def test_replies_after_input_stop_at_the_next_user_message():
    messages = [
        {"message_id": "a", "actor": "USER", "response_id": "r1", "created_at": "1"},
        {"message_id": "b", "actor": "ASSISTANT", "response_id": "r0", "created_at": "2"},
        {"message_id": "c", "actor": "USER", "response_id": "r2", "created_at": "3"},
        {"message_id": "d", "actor": "ASSISTANT", "response_id": "r2", "created_at": "4"},
    ]
    assert [m["message_id"] for m in replies_after_input(list(reversed(messages)), "r1")] == ["b"]
    assert replies_after_input(messages, "missing") == []
