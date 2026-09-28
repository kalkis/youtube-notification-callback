# youtube-notification-callback

AWS Lambda (container image) that receives YouTube [PubSubHubbub/WebSub](https://developers.google.com/youtube/v3/guides/push_notifications) notifications through a Function URL and publishes one JSON message per new, updated or deleted video to an SNS topic. It also subscribes to the hub for a list of channels kept in SSM.

```
YouTube hub ──GET/POST──▶ Function URL ──▶ this Lambda ──▶ SNS ──▶ SQS ──▶ youtube-metadata-collector
                          aws lambda invoke / EventBridge Scheduler ──▲ (subscribe, unsubscribe, resubscribe)
```

The consumer is [`youtube-metadata-collector`](https://github.com/kalkis/youtube-metadata-collector), and the infrastructure (topic, parameters, Function URL, schedule, alarms) is in [`tf-youtube-cloud-backup`](https://github.com/kalkis/tf-youtube-cloud-backup).

## Managing channels

The channel list parameter is changed only through the function's invoke actions, so adding or removing a channel needs no redeploy. Terraform seeds it on the first apply and ignores it afterwards. Each action returns the list after the change, as `{"channel_ids": [...]}`.

Replace the placeholders with the Terraform outputs `callback_function_name` and `channel_ids_parameter_name`.

```sh
# Follow a channel: adds it to the list, then asks the hub to subscribe
aws lambda invoke --function-name <callback_function_name> --cli-binary-format raw-in-base64-out \
  --payload '{"action":"subscribe","channel_id":"<channel_id>"}' /dev/stdout

# Stop following: removes it from the list (notifications are dropped from then on), then asks the hub to unsubscribe
aws lambda invoke --function-name <callback_function_name> --cli-binary-format raw-in-base64-out \
  --payload '{"action":"unsubscribe","channel_id":"<channel_id>"}' /dev/stdout

# Renew every subscription in the list (EventBridge Scheduler also runs this every 4 days)
aws lambda invoke --function-name <callback_function_name> --cli-binary-format raw-in-base64-out \
  --payload '{"action":"resubscribe"}' /dev/stdout

# Show the current list
aws ssm get-parameter --name <channel_ids_parameter_name> --query Parameter.Value --output text
```

- An invalid or missing `channel_id` fails before anything changes.
- The list is written **before** the hub request, so the hub's verification `GET` finds it. If the hub request fails, the action raises; a subscribed channel stays in the list and the next `resubscribe` retries it.
- `unsubscribe` is sent even for a channel not in the list, which clears leftover subscriptions.
- `resubscribe` tries every channel, logs each result and raises at the end if any failed, which fires the `Errors` alarm.
- Updates are read-modify-write without locking. Run actions one at a time.
- Don't edit the parameter by hand. An invalid value fails every invocation until it's fixed.

To fetch a video without a notification, send a manual job to the collector's queue; see the collector's [Usage](https://github.com/kalkis/youtube-metadata-collector#usage).

## Configuration

Set by Terraform as environment variables. Everything is validated when the module loads, and the handler fails immediately if anything is missing or invalid.

| Variable             | Required | Default                                      | Description                                                                                                                                  |
| -------------------- | -------- | -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `TOPIC_ARN`          | yes      |                                              | SNS topic that messages are published to                                                                                                     |
| `HUB_SECRET_PARAM`   | yes      |                                              | SSM SecureString name holding the HMAC secret (1–199 bytes). Read once per cold start.                                                       |
| `CHANNEL_IDS_PARAM`  | yes      |                                              | SSM String name holding the followed channels as a JSON array, e.g. `["UCPdaxSov0mgwh77JvjQO2jQ"]`. Read on every invocation, never cached. |
| `CALLBACK_URL_PARAM` | yes      |                                              | SSM String name holding this function's own Function URL. Read when a hub request is sent.                                                   |
| `HUB_URL`            | no       | `https://pubsubhubbub.appspot.com/subscribe` | Hub endpoint, must be `https`                                                                                                                |
| `LEASE_SECONDS`      | no       | `432000` (5 days)                            | Requested subscription lease                                                                                                                 |
| `LOG_LEVEL`          | no       | `INFO`                                       | Python logging level                                                                                                                         |

The function's role needs `sns:Publish` on the topic, `ssm:GetParameter` on all three parameters (with decrypt for the secret) and `ssm:PutParameter` on the channel list.

## Development

Requires [uv](https://docs.astral.sh/uv/). Python 3.13 comes from `.python-version`.

```sh
uv sync --locked
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy
docker build --platform linux/amd64 -t youtube-notification-callback .
```

The only runtime dependency is `xmltodict`. boto3 is provided by the Lambda runtime (it is only in the dev group, with `moto` for tests). The hub payloads in `tests/fixtures` are anonymised.

## Behaviour

| Request                                                                    | Response                         |
| -------------------------------------------------------------------------- | -------------------------------- |
| `GET` hub verification, `subscribe` for a channel **in** the list          | `200` with `hub.challenge`       |
| `GET` hub verification, `unsubscribe` for a channel **not in** the list    | `200` with `hub.challenge`       |
| Any other `GET`                                                            | `404` (refuses the subscription) |
| `POST` with a missing or invalid `X-Hub-Signature` (HMAC-SHA1 of the body) | `202`, message discarded         |
| `POST` whose body isn't an Atom feed                                       | `400`                            |
| `POST` that is published to SNS                                            | `204`                            |
| `POST` where SNS fails                                                     | `500`, so the hub retries        |
| Any other HTTP method                                                      | `400`                            |

Entries with invalid IDs, or from channels not in the list, are skipped with a warning. Nothing is de-duplicated, because the collector's upserts are idempotent. Logs contain video and channel IDs only, never titles.

Actions (`subscribe`, `unsubscribe`, `resubscribe`) are only routed for direct invocations, which IAM restricts to principals with `lambda:InvokeFunction`. Function URL requests always carry a `requestContext`, so an `action` body sent to the public URL never reaches them.

## Message contract

This contract is shared with [`youtube-metadata-collector`](https://github.com/kalkis/youtube-metadata-collector#message-contract), which validates every message against it. Every message is a UTF-8 JSON object, delivered to SQS with raw message delivery, so hub notifications and manual jobs look the same.

```json
{"schema_version": 1, "source": "pubsubhubbub", "event": "upsert", "video_id": "dQw4w9WgXcQ",
 "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw", "title": "Video title",
 "published": "2026-09-24T10:00:00+00:00", "updated": "2026-09-24T10:00:05+00:00"}

{"schema_version": 1, "source": "pubsubhubbub", "event": "delete", "video_id": "dQw4w9WgXcQ",
 "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw", "deleted_at": "2026-09-25T08:00:00+00:00"}
```

| Field                                         | Required | Validation                                                                   |
| --------------------------------------------- | -------- | ---------------------------------------------------------------------------- |
| `schema_version`                              | yes      | Integer, must equal `1`                                                      |
| `source`                                      | yes      | `"pubsubhubbub"` or `"manual"`                                               |
| `event`                                       | yes      | `"upsert"` (fetch and store) or `"delete"` (remove the item)                 |
| `video_id`                                    | yes      | `^[A-Za-z0-9_-]{11}$`                                                        |
| `channel_id`                                  | no       | `^UC[A-Za-z0-9_-]{22}$`                                                      |
| `title`, `published`, `updated`, `deleted_at` | no       | Strings, for debugging only. The collector stores only what the API returns. |

This function always sets `channel_id` and uses `source: "pubsubhubbub"`. A feed `entry` becomes an `upsert` and an `at:deleted-entry` becomes a `delete`. Optional fields missing from the feed are left out. `"manual"` is for jobs sent straight to the queue with `aws sqs send-message`.

## Breaking changes

This version replaces the original handler, and isn't compatible with it:

- **The `TOPIC_NAME` build arg and env var are gone.** The image holds no configuration; set `TOPIC_ARN` and the other variables above at runtime.
- **The topic is no longer created by the function** (`create_topic`). It must already exist, and the role no longer needs `sns:CreateTopic`.
- **The message format changed** to the contract above. `video_name` is now `title`; `channel_name`, `channel_link` and `youtube_link` are gone; and every message has `schema_version`, `source` and `event`. Subscribers to the old format must be updated.
- **Signatures are enforced.** Notifications without a valid `X-Hub-Signature` are discarded, so subscriptions made without the hub secret stop delivering. Subscribe through the actions above.
- **Hub verification is answered**, and only for channels in the list. Deleted entries now produce `delete` messages.
- Python 3.9 and `requirements.txt` are replaced by Python 3.13 and uv.

## License

[MIT](LICENSE)
