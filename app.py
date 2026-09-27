import base64
import hashlib
import hmac
import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse
from xml.parsers.expat import ExpatError

import boto3
import xmltodict
from botocore.exceptions import BotoCoreError, ClientError

TOPIC_ARN_PATTERN = re.compile(
    r"arn:aws[a-z-]*:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}"
)
CHANNEL_ID_PATTERN = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_URL = "https://www.youtube.com/channel/"
FEED_URL = "https://www.youtube.com/xml/feeds/videos.xml?channel_id="
FEED_URL_PATTERN = re.compile(re.escape(FEED_URL) + f"({CHANNEL_ID_PATTERN.pattern})")

logger = logging.getLogger()


class ConfigError(Exception):
    pass


class HubError(Exception):
    pass


def https_url(url) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


@dataclass(frozen=True)
class Config:
    topic_arn: str
    hub_secret: str = field(repr=False)
    channel_ids_param: str
    callback_url_param: str
    hub_url: str
    lease_seconds: int
    log_level: str


def load_config(env=os.environ, ssm=None) -> Config:
    def required(name):
        if not (value := env.get(name, "").strip()):
            raise ConfigError(f"{name} is required")
        return value

    topic_arn = required("TOPIC_ARN")
    if not TOPIC_ARN_PATTERN.fullmatch(topic_arn):
        raise ConfigError("TOPIC_ARN is not an SNS topic ARN")

    secret_param = required("HUB_SECRET_PARAM")
    channel_ids_param = required("CHANNEL_IDS_PARAM")
    callback_url_param = required("CALLBACK_URL_PARAM")

    hub_url = env.get("HUB_URL", "https://pubsubhubbub.appspot.com/subscribe")
    if not https_url(hub_url):
        raise ConfigError("HUB_URL must be an https URL")

    try:
        lease_seconds = int(env.get("LEASE_SECONDS", "432000"))
    except ValueError:
        raise ConfigError("LEASE_SECONDS must be an integer") from None
    if lease_seconds < 1:
        raise ConfigError("LEASE_SECONDS must be positive")

    log_level = env.get("LOG_LEVEL", "INFO").upper()
    if log_level not in logging.getLevelNamesMapping():
        raise ConfigError(f"LOG_LEVEL {log_level} is not a logging level")

    ssm = ssm or boto3.client("ssm")
    param = ssm.get_parameter(Name=secret_param, WithDecryption=True)["Parameter"]
    hub_secret = param["Value"]
    if not 0 < len(hub_secret.encode()) < 200:
        raise ConfigError("hub secret must be 1 to 199 bytes")

    return Config(
        topic_arn,
        hub_secret,
        channel_ids_param,
        callback_url_param,
        hub_url,
        lease_seconds,
        log_level,
    )


SSM = boto3.client("ssm")
SNS = boto3.client("sns")
CONFIG = load_config(ssm=SSM)
logger.setLevel(CONFIG.log_level)


def load_channel_ids() -> list[str]:
    value = SSM.get_parameter(Name=CONFIG.channel_ids_param)["Parameter"]["Value"]
    try:
        channel_ids = json.loads(value)
    except json.JSONDecodeError:
        channel_ids = None
    if not isinstance(channel_ids, list) or not all(
        isinstance(c, str) and CHANNEL_ID_PATTERN.fullmatch(c) for c in channel_ids
    ):
        raise ConfigError(
            f"{CONFIG.channel_ids_param} is not a JSON array of channel IDs"
        )
    return channel_ids


def raw_body(event) -> bytes:
    body = event.get("body") or ""
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()


def response(status, body=""):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/plain"},
        "body": body,
    }


def verify_subscription(event):
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


def field(node, *path) -> str | None:
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


def write_to_topic(message):
    SNS.publish(TopicArn=CONFIG.topic_arn, Message=json.dumps(message))


def save_channel_ids(channel_ids):
    SSM.put_parameter(
        Name=CONFIG.channel_ids_param,
        Value=json.dumps(channel_ids),
        Type="String",
        Overwrite=True,
    )


def load_callback_url() -> str:
    value = SSM.get_parameter(Name=CONFIG.callback_url_param)["Parameter"]["Value"]
    if not https_url(value):
        raise ConfigError(f"{CONFIG.callback_url_param} is not an https URL")
    return value


def hub_request(mode, channel_id, callback_url):
    form = {
        "hub.mode": mode,
        "hub.topic": FEED_URL + channel_id,
        "hub.callback": callback_url,
        "hub.verify": "async",
    }
    if mode == "subscribe":
        form |= {
            "hub.lease_seconds": CONFIG.lease_seconds,
            "hub.secret": CONFIG.hub_secret,
        }
    request = urllib.request.Request(CONFIG.hub_url, data=urlencode(form).encode())
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            status = reply.status
    except OSError as e:
        raise HubError(f"{mode} for {channel_id} failed: {e}") from e
    if status != 202:
        raise HubError(f"{mode} for {channel_id} got status {status}")


def resubscribe(event):
    channel_ids = load_channel_ids()
    callback_url = load_callback_url()
    failed = []
    for channel_id in channel_ids:
        try:
            hub_request("subscribe", channel_id, callback_url)
        except HubError as e:
            logger.error("Resubscribe failed: %s", e)
            failed.append(channel_id)
        else:
            logger.info("Resubscribed %s", channel_id)
    if failed:
        raise HubError(f"resubscribe failed for {', '.join(failed)}")
    return {"channel_ids": channel_ids}


def change_subscription(event, mode):
    channel_id = event.get("channel_id")
    if not isinstance(channel_id, str) or not CHANNEL_ID_PATTERN.fullmatch(channel_id):
        raise ValueError(f"invalid channel_id {channel_id!r}")
    channel_ids = load_channel_ids()
    callback_url = load_callback_url()
    wanted = mode == "subscribe"
    if (channel_id in channel_ids) != wanted:
        channel_ids = (
            channel_ids + [channel_id]
            if wanted
            else [c for c in channel_ids if c != channel_id]
        )
        save_channel_ids(channel_ids)
        logger.info(
            "Saved channel list %s %s", "with" if wanted else "without", channel_id
        )
    hub_request(mode, channel_id, callback_url)
    logger.info("Sent %s for %s", mode, channel_id)
    return {"channel_ids": channel_ids}


def subscribe(event):
    return change_subscription(event, "subscribe")


def unsubscribe(event):
    return change_subscription(event, "unsubscribe")


def lambda_handler(event, context):
    if event.get("requestContext", {}).get("http", {}).get("method") == "GET":
        return verify_subscription(event)

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
