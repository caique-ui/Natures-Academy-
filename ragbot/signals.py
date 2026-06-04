# ragbot/signals.py
"""
When an anonymous user logs in, transfer any conversations they started
during their session to their User account so history is not lost.
"""
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def claim_anonymous_conversations(sender, request, user, **kwargs):
    """
    Move session-keyed conversations to the newly authenticated user.
    Safe to call even if the session has no conversations.
    """
    from ragbot.models import Conversation  # avoid circular import at module level

    session_key = request.session.session_key
    if not session_key:
        return

    claimed = Conversation.objects.filter(
        session_key=session_key,
        user__isnull=True,
    ).update(user=user)

    if claimed:
        logger.info(
            "Claimed %d anonymous conversation(s) for user %s (session %s)",
            claimed, user.username, session_key[:8],
        )
