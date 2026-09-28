import base64
import hashlib
import hmac
import json
import logging
import re
from urllib.parse import parse_qsl
from xml.parsers.expat import ExpatError

import boto3
import xmltodict
from botocore.exceptions import BotoCoreError, ClientError

from config import CHANNEL_ID_PATTERN, CONFIG, FEED_URL_PATTERN
from subscriptions import load_channel_ids

VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_URL = "https://www.youtube.com/channel/"

logger = logging.getLogger()

SNS = boto3.client("sns")


def raw_body(event: dict) -> bytes:
    body = event.get("body") or ""
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()


def response(status: int, body: str = "") -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/plain"},
        "body": body,
    }


def verify_subscription(event: dict) -> dict:
    params = dict(parse_qsl(event.get("rawQueryString", "")))
    mode = params.get("hub.mode")
    match = FEED_URL_PATTERN.fullmatch(params.get("hub.topic", ""))
    channel_id = match and match[1]
    if (
        params.get("hub.challenge")
        and channel_id
        and mode in ("subscribe", "unsubscribe")
        and (channel_id in load_channel_ids()) == (mode == "subscribe")
    ):
        logger.info(
            "Accepted %s for %s, lease %s",
            mode,
            channel_id,
            params.get("hub.lease_seconds"),
        )
        return response(200, params["hub.challenge"])
    logger.warning("Refused %r for %s", mode, channel_id)
    return response(404)


def valid_signature(body: bytes, signature: str | None) -> bool:
    if signature is None:
        return False
    digest = hmac.new(CONFIG.hub_secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(f"sha1={digest}".encode(), signature.encode())


def parse_feed(body: bytes) -> dict:
    doc = xmltodict.parse(
        body, disable_entities=True, force_list=("entry", "at:deleted-entry")
    )
    if "feed" not in doc or not isinstance(doc["feed"], dict | None):
        raise ValueError("root element is not an Atom feed")
    return doc["feed"] or {}


def field(node: object, *path: str) -> str | None:
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) else None


def build_messages(feed: dict, channel_ids: list[str]) -> list[dict]:
    candidates = [
        {
            "event": "upsert",
            "video_id": field(entry, "yt:videoId"),
            "channel_id": field(entry, "yt:channelId"),
            "title": field(entry, "title"),
            "published": field(entry, "published"),
            "updated": field(entry, "updated"),
        }
        for entry in feed.get("entry", [])
    ] + [
        {
            "event": "delete",
            "video_id": (field(entry, "@ref") or "").removeprefix("yt:video:"),
            "channel_id": (field(entry, "at:by", "uri") or "").removeprefix(
                CHANNEL_URL
            ),
            "deleted_at": field(entry, "@when"),
        }
        for entry in feed.get("at:deleted-entry", [])
    ]
    messages = []
    for candidate in candidates:
        event = candidate["event"]
        video_id, channel_id = (
            candidate["video_id"] or "",
            candidate["channel_id"] or "",
        )
        if not (
            VIDEO_ID_PATTERN.fullmatch(video_id)
            and CHANNEL_ID_PATTERN.fullmatch(channel_id)
        ):
            logger.warning(
                "Skipping %s with invalid IDs %r %r", event, video_id, channel_id
            )
        elif channel_id not in channel_ids:
            logger.warning(
                "Skipping %s of %s from unlisted %s", event, video_id, channel_id
            )
        else:
            messages.append(
                {"schema_version": 1, "source": "pubsubhubbub"}
                | {k: v for k, v in candidate.items() if v is not None}
            )
    return messages


def write_to_topic(message: dict) -> None:
    SNS.publish(TopicArn=CONFIG.topic_arn, Message=json.dumps(message))


def notification(event: dict) -> dict:
    body = raw_body(event)
    signature = (event.get("headers") or {}).get("x-hub-signature")
    if not valid_signature(body, signature):
        logger.warning(
            "Discarding notification with %s signature",
            "missing" if signature is None else "invalid",
        )
        return response(202)

    try:
        feed = parse_feed(body)
    except (ExpatError, ValueError) as e:
        logger.warning("Rejecting unparseable notification: %s", e)
        return response(400)

    for message in build_messages(feed, load_channel_ids()):
        try:
            write_to_topic(message)
        except (BotoCoreError, ClientError) as e:
            logger.error(
                "Failed to publish %s of %s: %s",
                message["event"],
                message["video_id"],
                e,
            )
            return response(500)
        logger.info("Published %s of %s", message["event"], message["video_id"])
    return response(204)
