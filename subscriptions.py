import json
import logging
import urllib.request
from urllib.parse import urlencode

from config import CHANNEL_ID_PATTERN, CONFIG, FEED_URL, SSM, ConfigError, https_url

logger = logging.getLogger()


class HubError(Exception):
    pass


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
