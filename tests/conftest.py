import base64
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

import boto3
import pytest
from moto import mock_aws

CHANNEL_ID = "UCdj0goPwahmOx77QJvvj2SQ"
OTHER_CHANNEL_ID = "UCuAXFkgsw1L7xaCfnd5JJOw"
HUB_SECRET = "test-hub-secret"
CALLBACK_URL = "https://abc123.lambda-url.eu-west-1.on.aws/"
FEED_URL = "https://www.youtube.com/xml/feeds/videos.xml?channel_id="
FIXTURES = Path(__file__).parent / "fixtures"

os.environ.pop("AWS_PROFILE", None)
os.environ["AWS_DEFAULT_REGION"] = "eu-west-1"

MOCK = mock_aws()
MOCK.start()

SSM = boto3.client("ssm")
SSM.put_parameter(Name="/test/hub-secret", Value=HUB_SECRET, Type="SecureString")
TOPIC_ARN = boto3.client("sns").create_topic(Name="notifications")["TopicArn"]
SQS = boto3.client("sqs")
QUEUE_URL = SQS.create_queue(QueueName="notifications")["QueueUrl"]
boto3.client("sns").subscribe(
    TopicArn=TOPIC_ARN,
    Protocol="sqs",
    Endpoint=SQS.get_queue_attributes(QueueUrl=QUEUE_URL, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"],
    Attributes={"RawMessageDelivery": "true"},
)

os.environ |= {
    "TOPIC_ARN": TOPIC_ARN,
    "HUB_SECRET_PARAM": "/test/hub-secret",
    "CHANNEL_IDS_PARAM": "/test/channel-ids",
    "CALLBACK_URL_PARAM": "/test/callback-url",
    "LOG_LEVEL": "DEBUG",
}


def pytest_unconfigure(config):
    MOCK.stop()


def set_channel_ids(value):
    SSM.put_parameter(
        Name="/test/channel-ids",
        Value=value if isinstance(value, str) else json.dumps(value),
        Type="String",
        Overwrite=True,
    )


def channel_ids():
    return json.loads(SSM.get_parameter(Name="/test/channel-ids")["Parameter"]["Value"])


def published():
    messages = SQS.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=10)
    return [json.loads(m["Body"]) for m in messages.get("Messages", [])]


def http_event(method, query=None, body=b"", headers=None, encoded=False):
    return {
        "version": "2.0",
        "rawQueryString": urlencode(query or {}),
        "headers": headers or {},
        "requestContext": {"http": {"method": method}},
        "body": base64.b64encode(body).decode() if encoded else body.decode(),
        "isBase64Encoded": encoded,
    }


def verification_event(mode, channel_id, challenge="challenge-123"):
    return http_event(
        "GET",
        {
            "hub.mode": mode,
            "hub.topic": FEED_URL + channel_id,
            "hub.challenge": challenge,
            "hub.lease_seconds": "432000",
        },
    )


def signed_event(body: bytes, secret=HUB_SECRET, encoded=False):
    digest = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return http_event(
        "POST",
        body=body,
        headers={"x-hub-signature": f"sha1={digest}"},
        encoded=encoded,
    )


class FakeHub:
    """Records hub requests and, like the real hub, verifies accepted ones."""

    def __init__(self):
        self.requests = []
        self.verifications = []
        self.results = {}

    def urlopen(self, request, timeout):
        import app

        form = dict(parse_qsl(request.data.decode()))
        self.requests.append(form)
        channel_id = form["hub.topic"].removeprefix(FEED_URL)
        result = self.results.get(channel_id, 202)
        if isinstance(result, Exception):
            raise result
        if result == 202:
            event = verification_event(form["hub.mode"], channel_id)
            self.verifications.append(app.lambda_handler(event, None)["statusCode"])
        return contextlib.nullcontext(SimpleNamespace(status=result))


@pytest.fixture(autouse=True)
def reset_state():
    set_channel_ids([CHANNEL_ID])
    SSM.put_parameter(
        Name="/test/callback-url", Value=CALLBACK_URL, Type="String", Overwrite=True
    )
    SQS.purge_queue(QueueUrl=QUEUE_URL)


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr("urllib.request.urlopen", fake.urlopen)
    return fake
