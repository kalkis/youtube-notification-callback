import os

import pytest
from conftest import HUB_SECRET, SSM

from config import ConfigError, load_config

ENV = {
    name: os.environ[name]
    for name in (
        "TOPIC_ARN",
        "HUB_SECRET_PARAM",
        "CHANNEL_IDS_PARAM",
        "CALLBACK_URL_PARAM",
    )
}


def test_defaults():
    config = load_config(ENV, SSM)
    assert config.hub_secret == HUB_SECRET
    assert HUB_SECRET not in repr(config)
    assert config.hub_url == "https://pubsubhubbub.appspot.com/subscribe"
    assert config.lease_seconds == 432000
    assert config.log_level == "INFO"


@pytest.mark.parametrize(
    "overrides",
    [
        {"TOPIC_ARN": ""},
        {"TOPIC_ARN": "notifications"},
        {"HUB_SECRET_PARAM": " "},
        {"CHANNEL_IDS_PARAM": ""},
        {"CALLBACK_URL_PARAM": ""},
        {"HUB_URL": "http://pubsubhubbub.appspot.com/subscribe"},
        {"LEASE_SECONDS": "five days"},
        {"LEASE_SECONDS": "0"},
        {"LOG_LEVEL": "LOUD"},
    ],
)
def test_invalid_env_fails(overrides):
    with pytest.raises(ConfigError):
        load_config(ENV | overrides, SSM)


@pytest.mark.parametrize("secret", [" " * 200])
def test_invalid_secret_fails(secret):
    SSM.put_parameter(
        Name="/test/bad-secret", Value=secret, Type="SecureString", Overwrite=True
    )
    with pytest.raises(ConfigError):
        load_config(ENV | {"HUB_SECRET_PARAM": "/test/bad-secret"}, SSM)
