import base64
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import boto3
import xmltodict

TOPIC_ARN_PATTERN = re.compile(
    r"arn:aws[a-z-]*:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}"
)

logger = logging.getLogger()


class ConfigError(Exception):
    pass


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
    parsed = urlparse(hub_url)
    if parsed.scheme != "https" or not parsed.netloc:
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


CONFIG = load_config()
logger.setLevel(CONFIG.log_level)


def lambda_handler(event, context):
    body = raw_body(event)
    signature = (event.get("headers") or {}).get("x-hub-signature")
    if not valid_signature(body, signature):
        logger.warning(
            "Discarding notification with %s signature",
            "missing" if signature is None else "invalid",
        )
        return response(202)
    xml = xmltodict.parse(body)
    info = extract_info(xml)
    write_to_topic(info)
    return {"status": 200, "info": json.dumps(info)}


def raw_body(event) -> bytes:
    body = event.get("body") or ""
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()


def valid_signature(body: bytes, signature: str | None) -> bool:
    if signature is None:
        return False
    digest = hmac.new(CONFIG.hub_secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(f"sha1={digest}".encode(), signature.encode())


def response(status, body=""):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/plain"},
        "body": body,
    }


def extract_info(xml):
    info = xml["feed"]["entry"]
    return {
        "channel_id": info["yt:channelId"],
        "channel_name": info["author"]["name"],
        "channel_link": info["author"]["uri"],
        "video_name": info["title"],
        "video_id": info["yt:videoId"],
        "youtube_link": info["link"]["@href"],
        "published": info["published"],
        "updated": info["updated"],
    }


def write_to_topic(info):
    boto3.client("sns").publish(TopicArn=CONFIG.topic_arn, Message=json.dumps(info))
