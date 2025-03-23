import logging

import requests
from celery import shared_task
from django.utils import timezone

from bots.models import WebhookDeliveryAttempt, WebhookDeliveryAttemptStatus, WebhookTriggerTypes
from bots.webhook_utils import sign_payload

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    retry_backoff=True,  # Enable exponential backoff
    max_retries=5,
)
def deliver_webhook(self, delivery_id):
    """
    Deliver a webhook to its destination.
    """
    try:
        delivery = WebhookDeliveryAttempt.objects.get(id=delivery_id)
    except WebhookDeliveryAttempt.DoesNotExist:
        logger.error(f"Webhook delivery attempt {delivery_id} not found")
        return

    subscription = delivery.webhook_subscription

    # If the subscription is no longer active, mark as failed and return
    if not subscription.is_active:
        delivery.status = "failed"
        delivery.error_message = "Webhook subscription is no longer active"
        delivery.save()
        return

    # Prepare the webhook payload
    webhook_data = {
        "idempotency_key": str(delivery.idempotency_key),
        "bot_id": delivery.bot.object_id if delivery.bot else None,
        "trigger": WebhookTriggerTypes.trigger_type_to_api_code(delivery.webhook_event_type),
        "data": delivery.payload,
    }

    # Sign the payload
    active_secret = subscription.secrets.filter(is_active=True).order_by('-created_at').first()
    signature = sign_payload(webhook_data, active_secret.get_secret())

    # Increment attempt counter
    delivery.attempt_count += 1
    delivery.last_attempt_at = timezone.now()

    # Send the webhook
    try:
        response = requests.post(
            subscription.url,
            json=webhook_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Attendee-Webhook/1.0",
                "X-Webhook-Signature": signature,
            },
            timeout=10,  # 10-second timeout
        )

        # Update the delivery attempt with the response
        delivery.response_status_code = response.status_code

        # Limit response body storage to prevent DB issues with large responses
        response_body = response.text[:10000]
        delivery.response_body_list.append(response_body)

        # Check if the delivery was successful (2xx status code)
        if 200 <= response.status_code < 300:
            delivery.status = WebhookDeliveryAttemptStatus.SUCCESS
            delivery.succeeded_at = timezone.now()
            delivery.save()
            return

        # If we got here, the delivery failed with a non-2xx status code
        delivery.status = WebhookDeliveryAttemptStatus.FAILURE

    except requests.RequestException:
        # Handle network errors, timeouts, etc.
        delivery.status = WebhookDeliveryAttemptStatus.FAILURE

    delivery.save()

    # Check if this was the last retry attempt
    if delivery.attempt_count >= self.max_retries and delivery.status == WebhookDeliveryAttemptStatus.FAILURE:
        logger.error(f"Webhook delivery failed after {delivery.attempt_count} attempts. " + f"Webhook ID: {delivery.id}, URL: {subscription.url}, " + f"Event: {delivery.webhook_event_type}, Status: {delivery.status}")
