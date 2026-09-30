import logging

import notifications
import subscriptions

logger = logging.getLogger()

HTTP_HANDLERS = {
    "GET": notifications.verify_subscription,
    "POST": notifications.notification,
}
ACTIONS = {
    "resubscribe": subscriptions.resubscribe,
    "subscribe": subscriptions.subscribe,
    "unsubscribe": subscriptions.unsubscribe,
}


def lambda_handler(event: object, context: object) -> dict:
    if not isinstance(event, dict):
        logger.error("Unsupported event type %s", type(event).__name__)
        raise TypeError("event must be a JSON object")

    if "requestContext" in event:
        method = (event["requestContext"].get("http") or {}).get("method", "")
        if handler := HTTP_HANDLERS.get(method):
            return handler(event)
        logger.error("Unsupported method %r", method)
        return notifications.response(400)

    action = event.get("action", "")
    if handler := ACTIONS.get(action):
        return handler(event)
    logger.error("Unknown action %r", action)
    raise ValueError(f"unknown action {action!r}")
