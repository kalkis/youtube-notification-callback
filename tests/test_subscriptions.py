import urllib.error
from email.message import Message

import pytest
from conftest import (
    CALLBACK_URL,
    CHANNEL_ID,
    FEED_URL,
    HUB_SECRET,
    OTHER_CHANNEL_ID,
    SSM,
    FakeHub,
    channel_ids,
    set_channel_ids,
)

import app
from config import ConfigError
from subscriptions import HubError


def invoke(action: str, **fields: object) -> dict:
    return app.lambda_handler({"action": action} | fields, None)


def test_resubscribe_sends_subscribe_for_every_channel(hub: FakeHub) -> None:
    set_channel_ids([CHANNEL_ID, OTHER_CHANNEL_ID])
    assert invoke("resubscribe") == {"channel_ids": [CHANNEL_ID, OTHER_CHANNEL_ID]}
    assert hub.requests == [
        {
            "hub.mode": "subscribe",
            "hub.topic": FEED_URL + channel_id,
            "hub.callback": CALLBACK_URL,
            "hub.verify": "async",
            "hub.lease_seconds": "432000",
            "hub.secret": HUB_SECRET,
        }
        for channel_id in (CHANNEL_ID, OTHER_CHANNEL_ID)
    ]
    assert hub.verifications == [200, 200]


def test_resubscribe_with_empty_list(hub: FakeHub) -> None:
    set_channel_ids([])
    assert invoke("resubscribe") == {"channel_ids": []}
    assert hub.requests == []


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("unreachable"),
        urllib.error.HTTPError(
            "https://pubsubhubbub.appspot.com", 500, "error", Message(), None
        ),
        204,
    ],
)
def test_resubscribe_tries_every_channel_then_raises(
    hub: FakeHub, caplog: pytest.LogCaptureFixture, failure: Exception | int
) -> None:
    set_channel_ids([CHANNEL_ID, OTHER_CHANNEL_ID])
    hub.results[CHANNEL_ID] = failure
    with pytest.raises(HubError, match=CHANNEL_ID):
        invoke("resubscribe")
    assert len(hub.requests) == 2
    assert f"Resubscribed {OTHER_CHANNEL_ID}" in caplog.text
    assert HUB_SECRET not in caplog.text


def test_subscribe_saves_channel_before_hub_verifies(hub: FakeHub) -> None:
    assert invoke("subscribe", channel_id=OTHER_CHANNEL_ID) == {
        "channel_ids": [CHANNEL_ID, OTHER_CHANNEL_ID]
    }
    assert channel_ids() == [CHANNEL_ID, OTHER_CHANNEL_ID]
    assert [r["hub.mode"] for r in hub.requests] == ["subscribe"]
    assert hub.verifications == [200]


def test_subscribe_listed_channel_is_not_duplicated(hub: FakeHub) -> None:
    assert invoke("subscribe", channel_id=CHANNEL_ID) == {"channel_ids": [CHANNEL_ID]}
    assert channel_ids() == [CHANNEL_ID]
    assert hub.verifications == [200]


def test_subscribe_keeps_channel_when_hub_fails(hub: FakeHub) -> None:
    hub.results[OTHER_CHANNEL_ID] = urllib.error.URLError("unreachable")
    with pytest.raises(HubError):
        invoke("subscribe", channel_id=OTHER_CHANNEL_ID)
    assert channel_ids() == [CHANNEL_ID, OTHER_CHANNEL_ID]


def test_unsubscribe_removes_channel_before_hub_verifies(hub: FakeHub) -> None:
    assert invoke("unsubscribe", channel_id=CHANNEL_ID) == {"channel_ids": []}
    assert channel_ids() == []
    assert hub.requests == [
        {
            "hub.mode": "unsubscribe",
            "hub.topic": FEED_URL + CHANNEL_ID,
            "hub.callback": CALLBACK_URL,
            "hub.verify": "async",
        }
    ]
    assert hub.verifications == [200]


def test_unsubscribe_unlisted_channel_still_sends_request(hub: FakeHub) -> None:
    assert invoke("unsubscribe", channel_id=OTHER_CHANNEL_ID) == {
        "channel_ids": [CHANNEL_ID]
    }
    assert channel_ids() == [CHANNEL_ID]
    assert hub.verifications == [200]


@pytest.mark.parametrize("action", ["subscribe", "unsubscribe"])
@pytest.mark.parametrize(
    "fields",
    [{}, {"channel_id": None}, {"channel_id": 1}, {"channel_id": "UCshort"}],
)
def test_invalid_channel_id_changes_nothing(
    hub: FakeHub, action: str, fields: dict
) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        invoke(action, **fields)
    assert channel_ids() == [CHANNEL_ID]
    assert hub.requests == []


@pytest.mark.parametrize(
    "value",
    ["not json", '{"a": 1}', '"UCdj0goPwahmOx77QJvvj2SQ"', '["UCshort"]', "[1]"],
)
@pytest.mark.parametrize(
    "action", [("resubscribe", {}), ("subscribe", {"channel_id": OTHER_CHANNEL_ID})]
)
def test_malformed_channel_list_fails(
    hub: FakeHub, value: str, action: tuple[str, dict]
) -> None:
    set_channel_ids(value)
    with pytest.raises(ConfigError):
        invoke(action[0], **action[1])
    assert hub.requests == []


def test_invalid_callback_url_fails(hub: FakeHub) -> None:
    SSM.put_parameter(
        Name="/test/callback-url",
        Value="http://insecure",
        Type="String",
        Overwrite=True,
    )
    with pytest.raises(ConfigError):
        invoke("resubscribe")
    assert hub.requests == []


def test_actions_log_ids_but_not_secret(
    hub: FakeHub, caplog: pytest.LogCaptureFixture
) -> None:
    invoke("subscribe", channel_id=OTHER_CHANNEL_ID)
    invoke("unsubscribe", channel_id=OTHER_CHANNEL_ID)
    assert OTHER_CHANNEL_ID in caplog.text
    assert HUB_SECRET not in caplog.text


@pytest.mark.parametrize("event", [{}, {"action": "delete"}, {"action": None}])
def test_unknown_action_raises(hub: FakeHub, event: dict) -> None:
    with pytest.raises(ValueError, match="unknown action"):
        app.lambda_handler(event, None)
    assert hub.requests == []


@pytest.mark.parametrize("event", [None, [], "resubscribe"])
def test_non_object_event_raises(event: object) -> None:
    with pytest.raises(TypeError):
        app.lambda_handler(event, None)
