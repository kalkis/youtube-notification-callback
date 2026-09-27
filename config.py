import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import boto3

TOPIC_ARN_PATTERN = re.compile(
    r"arn:aws[a-z-]*:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}"
)
CHANNEL_ID_PATTERN = re.compile(r"UC[A-Za-z0-9_-]{22}")
FEED_URL = "https://www.youtube.com/xml/feeds/videos.xml?channel_id="
FEED_URL_PATTERN = re.compile(re.escape(FEED_URL) + f"({CHANNEL_ID_PATTERN.pattern})")

logger = logging.getLogger()


class ConfigError(Exception):
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
CONFIG = load_config(ssm=SSM)
logger.setLevel(CONFIG.log_level)
