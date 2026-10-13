import pytest

from tests.support.telegram import (
    BOT,
    callback_update,
    group_update,
    mention,
    read,
    telegram_message,
    telegram_update,
)


def test_private_message_identity_and_metadata():
    message = read(telegram_update())
    assert message.id == "telegram:99:30:100"
    assert message.conversation_id == "telegram:99:30"
    assert message.channel == "telegram" and message.role == "user"
    assert message.sender.id == "30" and message.sender.display_name == "Alice Liddell"
    assert message.text == "hello" and message.thread_id is None
    assert dict(message.metadata) == {
        "bot_id": 99, "chat_id": 30, "chat_type": "private",
        "message_id": 100, "message_thread_id": None,
    }


@pytest.mark.parametrize("token", ["@agentchat_bot", "@AgentChat_Bot"])
def test_group_mention_is_addressed_and_stripped(token):
    text = f"{token} what is up"
    message = read(group_update(text, entities=[mention(text, token)]))
    assert message.text == "what is up"
    assert message.conversation_id == "telegram:99:-1001"


def test_mention_after_astral_emoji_is_sliced_in_utf16_units():
    text = "😀 @agentchat_bot hi 日本"
    message = read(group_update(text))
    assert message.text == "😀  hi 日本"


def test_command_with_bot_suffix_keeps_the_command():
    text = "/status@agentchat_bot now"
    entity = mention(text, "/status@agentchat_bot", "bot_command")
    message = read(group_update(text, entities=[entity]))
    assert message.text == "/status now"


def test_text_mention_and_reply_to_bot_are_addressed():
    text = "Bot please"
    entity = mention(text, "Bot", "text_mention", user={"id": 99, "is_bot": True})
    assert read(group_update(text, entities=[entity])).text == "Bot please"
    reply = {"message_id": 90, "from": dict(BOT), "chat": {"id": -1001, "type": "supergroup"}}
    assert read(group_update("thanks", reply_to_message=reply)).text == "thanks"


@pytest.mark.parametrize(
    "update",
    [
        group_update("just chatting"),
        group_update("@other_bot hi", entities=[mention("@other_bot hi", "@other_bot")]),
        group_update(
            "/status@other_bot",
            entities=[mention("/status@other_bot", "/status@other_bot", "bot_command")],
        ),
        group_update("hi", entities=[{"type": "text_mention", "offset": 0, "length": 2,
                                      "user": {"id": 7}}]),
        group_update("@agentchat_bot hi", entities=[{"type": "mention", "offset": 3,
                                                     "length": 99}]),
        telegram_update(**{"from": {**BOT, "id": 77}}),
        telegram_update(**{"from": {"id": 30}}),
        telegram_update(chat={"id": -100, "type": "channel", "title": "News"}),
        telegram_update(message_id=0),
        telegram_update(text=None, new_chat_members=[{"id": 5, "is_bot": False}]),
        telegram_update(text="   "),
        {"update_id": 1},
        {"update_id": 1, "message": "hello"},
        {"update_id": 1, "edited_message": telegram_message()},
        None,
        [],
    ],
)
def test_unaddressed_bot_channel_service_and_malformed_updates_are_dropped(update):
    assert read(update) is None


def test_forum_topic_is_a_thread_but_reply_threads_are_not():
    topic = read(telegram_update(message_thread_id=7, is_topic_message=True))
    assert topic.thread_id == "7"
    assert topic.conversation_id == "telegram:99:30:7"
    assert topic.metadata["message_thread_id"] == 7
    reply_thread = read(telegram_update(message_thread_id=7))
    assert reply_thread.thread_id is None
    assert reply_thread.conversation_id == "telegram:99:30"
    assert reply_thread.metadata["message_thread_id"] is None


def test_attachments_use_the_largest_photo_and_document_details():
    photo = read(telegram_update(text=None, caption="look", photo=[
        {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
        {"file_id": "large", "file_unique_id": "l", "width": 800, "height": 800,
         "file_size": 5000},
    ]))
    assert photo.text == "look"
    assert [(a.id, a.name, a.media_type, a.size) for a in photo.attachments] == [
        ("large", None, "image/jpeg", 5000),
    ]
    document = {"file_id": "doc", "file_unique_id": "d", "file_name": "report.pdf",
                "mime_type": "application/pdf", "file_size": 12}
    message = read(telegram_update(text=None, document=document))
    assert message.text == ""
    assert [(a.id, a.name, a.media_type, a.size) for a in message.attachments] == [
        ("doc", "report.pdf", "application/pdf", 12),
    ]
    gif = {"file_id": "gif", "file_unique_id": "g", "file_name": "a.mp4", "mime_type": "video/mp4"}
    animated = read(telegram_update(text=None, animation=gif, document=gif))
    assert [a.id for a in animated.attachments] == ["gif"]
    voice = read(telegram_update(text=None, voice={"file_id": "v", "mime_type": "audio/ogg"}))
    assert voice.attachments[0].media_type == "audio/ogg"
    sticker = read(telegram_update(text=None, sticker={"file_id": "st", "type": "regular"}))
    assert sticker.attachments[0].name is None and sticker.attachments[0].media_type is None


def test_group_caption_mention_uses_caption_entities():
    caption = "@agentchat_bot what is this"
    message = read(group_update(
        text=None, entities=[], caption=caption,
        caption_entities=[mention(caption, "@agentchat_bot")],
        document={"file_id": "doc"},
    ))
    assert message.text == "what is this"
    assert message.attachments[0].id == "doc"


def test_callback_tap_becomes_the_button_label():
    message = read(callback_update())
    assert message.id == "telegram:99:callback:cb-1"
    assert message.conversation_id == "telegram:99:30"
    assert message.text == "Yes please"
    assert message.sender.id == "30"
    assert message.metadata["message_id"] == 55
    assert message.metadata["callback_query_id"] == "cb-1"
    assert message.metadata["callback_data"] == "yes"
    assert read(callback_update(data="unknown")).text == "unknown"
    group = callback_update()
    group["callback_query"]["message"]["chat"] = {"id": -1001, "type": "supergroup"}
    assert read(group).conversation_id == "telegram:99:-1001"


@pytest.mark.parametrize(
    "changes",
    [
        {"data": None},
        {"from": {**BOT, "id": 77}},
        {"message": {"message_id": 55, "date": 0, "chat": {"id": 30, "type": "private"}}},
        {"message": None},
        {"id": ""},
    ],
)
def test_unusable_callbacks_are_dropped(changes):
    update = callback_update()
    update["callback_query"].update(changes)
    assert read(update) is None
