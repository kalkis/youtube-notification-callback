import json

import pytest
from botocore.exceptions import ClientError
from conftest import (
    CHANNEL_ID,
    FIXTURES,
    OTHER_CHANNEL_ID,
    channel_ids,
    http_event,
    published,
    set_channel_ids,
    signed_event,
    verification_event,
)

import app
import notifications

NEW_ENTRY = (FIXTURES / "new_entry.xml").read_bytes()
DELETED_ENTRY = (FIXTURES / "deleted_entry.xml").read_bytes()
MALFORMED = (FIXTURES / "malformed.xml").read_bytes()
TITLE = '"can you just.......text me?"'


def handle(event):
    return app.lambda_handler(event, None)


def entry(video_id, channel_id, title=TITLE):
    return (
        f"<entry><yt:videoId>{video_id}</yt:videoId>"
        f"<yt:channelId>{channel_id}</yt:channelId><title>{title}</title></entry>"
    )


def feed(*entries):
    return (
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        f'xmlns="http://www.w3.org/2005/Atom">{"".join(entries)}</feed>'
    ).encode()


@pytest.mark.parametrize(
    ("mode", "listed", "status"),
    [
        ("subscribe", True, 200),
        ("subscribe", False, 404),
        ("unsubscribe", False, 200),
        ("unsubscribe", True, 404),
    ],
)
def test_verification_against_channel_list(mode, listed, status):
    set_channel_ids([CHANNEL_ID] if listed else [OTHER_CHANNEL_ID])
    result = handle(verification_event(mode, CHANNEL_ID))
    assert result["statusCode"] == status
    if status == 200:
        assert result["body"] == "challenge-123"
        assert result["headers"]["Content-Type"] == "text/plain"


def test_verification_reads_channel_list_on_every_request():
    assert (
        handle(verification_event("subscribe", OTHER_CHANNEL_ID))["statusCode"] == 404
    )
    set_channel_ids([CHANNEL_ID, OTHER_CHANNEL_ID])
    assert (
        handle(verification_event("subscribe", OTHER_CHANNEL_ID))["statusCode"] == 200
    )


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"hub.mode": "subscribe", "hub.challenge": "c"},
        {
            "hub.mode": "subscribe",
            "hub.topic": f"https://example.com/feed?channel_id={CHANNEL_ID}",
            "hub.challenge": "c",
        },
        {
            "hub.mode": "subscribe",
            "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={CHANNEL_ID}&x=1",
            "hub.challenge": "c",
        },
        {
            "hub.mode": "denied",
            "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={CHANNEL_ID}",
            "hub.challenge": "c",
        },
        {
            "hub.mode": "subscribe",
            "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={CHANNEL_ID}",
        },
    ],
)
def test_verification_refuses_invalid_requests(query):
    assert handle(http_event("GET", query))["statusCode"] == 404


def test_new_entry_publishes_upsert():
    assert handle(signed_event(NEW_ENTRY))["statusCode"] == 204
    assert published() == [
        {
            "schema_version": 1,
            "source": "pubsubhubbub",
            "event": "upsert",
            "video_id": "-AwCEM5K-Ec",
            "channel_id": CHANNEL_ID,
            "title": TITLE,
            "published": "2026-09-22T00:00:14+00:00",
            "updated": "2026-09-22T17:37:38.214885301+00:00",
        }
    ]


def test_deleted_entry_publishes_delete():
    assert handle(signed_event(DELETED_ENTRY))["statusCode"] == 204
    assert published() == [
        {
            "schema_version": 1,
            "source": "pubsubhubbub",
            "event": "delete",
            "video_id": "NJGHBuZzHsI",
            "channel_id": CHANNEL_ID,
            "deleted_at": "2026-09-25T08:00:00.583719+00:00",
        }
    ]


def test_base64_body_is_decoded():
    assert handle(signed_event(NEW_ENTRY, encoded=True))["statusCode"] == 204
    assert len(published()) == 1


def test_multiple_entries_are_filtered():
    body = feed(
        entry("aaaaaaaaaaa", CHANNEL_ID),
        entry("bbbbbbbbbbb", OTHER_CHANNEL_ID),
        entry("too-short", CHANNEL_ID),
        entry("ccccccccccc", "UCnot-a-channel"),
        entry("ddddddddddd", CHANNEL_ID),
    )
    assert handle(signed_event(body))["statusCode"] == 204
    assert [m["video_id"] for m in published()] == ["aaaaaaaaaaa", "ddddddddddd"]


def test_empty_feed_publishes_nothing():
    assert handle(signed_event(feed()))["statusCode"] == 204
    assert published() == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-hub-signature": "sha1=0000"},
        {"x-hub-signature": "sha256=abc"},
    ],
)
def test_bad_signature_is_accepted_and_discarded(headers):
    event = http_event("POST", body=NEW_ENTRY, headers=headers)
    assert handle(event)["statusCode"] == 202
    assert published() == []


def test_signature_with_wrong_secret_is_discarded():
    assert handle(signed_event(NEW_ENTRY, secret="wrong"))["statusCode"] == 202
    assert published() == []


@pytest.mark.parametrize("body", [MALFORMED, b"<html><body/></html>", b"not xml"])
def test_unparseable_body_returns_400(body):
    assert handle(signed_event(body))["statusCode"] == 400
    assert published() == []


def test_entities_are_not_expanded():
    body = b'<!DOCTYPE feed [<!ENTITY x "expanded">]>' + feed(
        entry("aaaaaaaaaaa", CHANNEL_ID, "&x;")
    )
    assert handle(signed_event(body))["statusCode"] == 400
    assert published() == []


def test_sns_failure_returns_500(monkeypatch):
    def fail(**kwargs):
        raise ClientError({"Error": {"Code": "NotFound"}}, "Publish")

    monkeypatch.setattr(notifications.SNS, "publish", fail)
    assert handle(signed_event(NEW_ENTRY))["statusCode"] == 500


def test_logs_never_contain_titles(caplog):
    set_channel_ids([OTHER_CHANNEL_ID])
    handle(signed_event(NEW_ENTRY))
    set_channel_ids([CHANNEL_ID])
    handle(signed_event(NEW_ENTRY))
    handle(
        signed_event(feed(entry("bad", CHANNEL_ID), entry("aaaaaaaaaaa", CHANNEL_ID)))
    )
    handle(http_event("POST", body=NEW_ENTRY, headers={"x-hub-signature": "sha1=0"}))
    assert caplog.records
    assert "text me" not in caplog.text


def test_unsupported_method_returns_400():
    assert handle(http_event("PUT"))["statusCode"] == 400


ACTION = {"action": "subscribe", "channel_id": OTHER_CHANNEL_ID}


@pytest.mark.parametrize(
    ("event", "status"),
    [
        (http_event("POST", body=json.dumps(ACTION).encode()), 202),
        (signed_event(json.dumps(ACTION).encode()), 400),
        (http_event("GET", ACTION), 404),
        (http_event("PUT") | ACTION, 400),
    ],
)
def test_function_url_request_cannot_reach_action(hub, event, status):
    assert handle(event)["statusCode"] == status
    assert hub.requests == []
    assert channel_ids() == [CHANNEL_ID]
