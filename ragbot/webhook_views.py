import logging

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ragbot.tasks import webhook_received_task

logger = logging.getLogger(__name__)

# Secret token set in settings.py — must match what you registered with Drive
WEBHOOK_TOKEN = getattr(settings, "DRIVE_WEBHOOK_TOKEN", "")


@csrf_exempt
@require_POST
def drive_webhook(request):
    """
    Receive Google Drive push notification.

    Drive sends these headers:
      X-Goog-Channel-Token   — your secret token (verify this)
      X-Goog-Resource-ID     — the watched resource
      X-Goog-Resource-State  — "sync" (initial), "change", "add", "remove", etc.
      X-Goog-Channel-ID      — channel UUID you registered
    """
    # ── Verify token ─────────────────────────────────────────────────────
    token = request.headers.get("X-Goog-Channel-Token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        logger.warning("Drive webhook: invalid token received.")
        return HttpResponse(status=403)

    resource_state = request.headers.get("X-Goog-Resource-State", "")
    resource_id    = request.headers.get("X-Goog-Resource-ID", "")
    channel_id     = request.headers.get("X-Goog-Channel-ID", "")

    logger.info(
        f"Drive webhook: state={resource_state} "
        f"resource={resource_id} channel={channel_id}"
    )

    # "sync" is the initial handshake — no actual change
    if resource_state == "sync":
        return HttpResponse(status=200)

    # Enqueue lightweight task — returns 200 immediately
    folder_id = getattr(settings, "GDRIVE_DEFAULT_FOLDER_ID", "")
    webhook_received_task.delay(
        folder_id=folder_id,
        resource_id=resource_id,
        resource_state=resource_state,
    )

    return HttpResponse(status=200)
