# ragbot/views.py — conversational RAG edition
#
# What changed from the original:
#  - send_message()        now loads/creates a Conversation and saves Messages
#  - send_message_stream() same, plus streams via conversation_rag.py
#  - reset_chat()          now deletes the current Conversation (not just session)
#  - NEW: conversation_list()  — GET  /api/conversations/
#  - NEW: conversation_detail() — GET/DELETE /api/conversations/<uuid>/
#  - NEW: auth views: register, login_view, logout_view
#  - chat_view() passes conversation list + current conversation to template
#
# Nothing in existing vectorstore_db.py / models.py / tasks.py changes.

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .conversation_rag import conversational_rag_stream, conversational_rag_answer
from .forms import ChatForm
from .models import Conversation, Message
from .vectorstore_db import get_db_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_or_create_conversation(request, conv_id: str | None) -> Conversation:
    """
    Load an existing conversation (if conv_id given and ownership checks pass)
    or create a new one tied to the current user / session.
    """
    # Ensure Django has a session key (creates one if needed)
    if not request.session.session_key:
        request.session.create()
    session_key = request.session.session_key

    if conv_id:
        try:
            conv = Conversation.objects.get(pk=conv_id)
            # Ownership check
            if conv.user:
                if not request.user.is_authenticated or conv.user != request.user:
                    raise PermissionError
            else:
                if conv.session_key != session_key:
                    raise PermissionError
            return conv
        except (Conversation.DoesNotExist, PermissionError):
            pass  # Fall through to create a new one

    return Conversation.objects.create(
        user=request.user if request.user.is_authenticated else None,
        session_key=session_key,
    )


def _conversations_for_request(request) -> list[dict]:
    """Return serialised conversation list for the sidebar."""
    if request.user.is_authenticated:
        qs = Conversation.objects.filter(user=request.user)
    else:
        session_key = request.session.session_key
        if not session_key:
            return []
        qs = Conversation.objects.filter(session_key=session_key, user__isnull=True)

    return [
        {"id": str(c.id), "title": c.title, "updated_at": c.updated_at.isoformat()}
        for c in qs[:50]
    ]


# ---------------------------------------------------------------------------
# main chat view
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def chat_view(request):
    """Render the chat interface."""
    # Ensure session exists so anonymous users get a key immediately
    if not request.session.session_key:
        request.session.create()

    form          = Conversation.objects.none()  # unused, kept for template compat
    form          = ChatForm()
    conversations = _conversations_for_request(request)

    # Load a specific conversation if ?conv=<uuid> is in the URL
    active_conv_id = request.GET.get("conv")
    active_messages = []
    if active_conv_id:
        try:
            conv = Conversation.objects.get(pk=active_conv_id)
            can_view = (
                (conv.user and request.user.is_authenticated and conv.user == request.user)
                or
                (not conv.user and conv.session_key == request.session.session_key)
            )
            if can_view:
                active_messages = list(conv.messages.order_by("created_at").values(
                    "role", "content", "source_chunks"
                ))
        except Conversation.DoesNotExist:
            active_conv_id = None

    return render(request, "chat/index.html", {
        "form":           form,
        "conversations":  conversations,
        "active_conv_id": active_conv_id,
        "active_messages": active_messages,
    })


# ---------------------------------------------------------------------------
# streaming send (primary path)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@csrf_exempt
def send_message_stream(request):
    """
    Streaming conversational RAG endpoint.

    Request body (JSON):
        message         str   — user's message
        conversation_id str   — UUID of existing conversation (optional)

    SSE events emitted:
        {type: "status",       message: str}
        {type: "clear_status"}
        {type: "content",      content: str}
        {type: "done",         full_response, sources, conversation_id,
                               conversation_title, rewritten_query}
        {type: "error",        error: str}
    """
    try:
        data    = json.loads(request.body)
        user_msg = data.get("message", "").strip()
        conv_id  = data.get("conversation_id")

        if not user_msg:
            return JsonResponse({"error": "Message cannot be empty"}, status=400)

        conv = _get_or_create_conversation(request, conv_id)

        # Auto-title: use first user message (truncated)
        if conv.title == "New conversation":
            conv.title = user_msg[:60]
            conv.save(update_fields=["title"])

        # Persist user message immediately (before streaming starts)
        Message.objects.create(
            conversation=conv,
            role=Message.ROLE_USER,
            content=user_msg,
        )

        def generate():
            import json as _json

            full_response = ""
            sources       = []
            rewritten     = user_msg

            for chunk in conversational_rag_stream(user_msg, conv):
                # chunk is already a formatted "data: {...}\n\n" string
                yield chunk

                # Parse the last event to capture full_response / sources
                if chunk.startswith("data: "):
                    try:
                        payload = _json.loads(chunk[6:])
                        if payload.get("type") == "done":
                            full_response = payload.get("full_response", "")
                            sources       = payload.get("sources", [])
                            rewritten     = payload.get("rewritten_query", user_msg)
                    except Exception:
                        pass

            # Persist assistant message and update conversation timestamp
            try:
                Message.objects.create(
                    conversation=conv,
                    role=Message.ROLE_ASSISTANT,
                    content=full_response,
                    source_chunks=sources,
                )
                conv.save()  # bumps updated_at

                # Send final metadata event
                yield f"data: {_json.dumps({'type': 'conversation_meta', 'conversation_id': str(conv.id), 'conversation_title': conv.title})}\n\n"
            except Exception as exc:
                logger.error("Failed to persist assistant message: %s", exc)

        response = StreamingHttpResponse(generate(), content_type="text/event-stream")
        response["Cache-Control"]    = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    except Exception as exc:
        logger.error("send_message_stream error: %s", exc, exc_info=True)
        return JsonResponse({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# non-streaming send (fallback)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@csrf_exempt
def send_message(request):
    """Synchronous fallback for environments where SSE is unavailable."""
    try:
        data     = json.loads(request.body)
        user_msg = data.get("message", "").strip()
        conv_id  = data.get("conversation_id")

        if not user_msg:
            return JsonResponse({"error": "Message cannot be empty"}, status=400)

        conv = _get_or_create_conversation(request, conv_id)

        if conv.title == "New conversation":
            conv.title = user_msg[:60]
            conv.save(update_fields=["title"])

        Message.objects.create(conversation=conv, role=Message.ROLE_USER, content=user_msg)

        result = conversational_rag_answer(user_msg, conv)

        Message.objects.create(
            conversation=conv,
            role=Message.ROLE_ASSISTANT,
            content=result["answer"],
            source_chunks=result["sources"],
        )
        conv.save()

        return JsonResponse({
            "success":          True,
            "user_message":     user_msg,
            "assistant_message": result["answer"],
            "sources":          result["sources"],
            "conversation_id":  str(conv.id),
            "conversation_title": conv.title,
        })

    except Exception as exc:
        logger.error("send_message error: %s", exc, exc_info=True)
        return JsonResponse({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# conversation management API
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def conversation_list(request):
    """GET /api/conversations/ — returns sidebar list."""
    return JsonResponse({"conversations": _conversations_for_request(request)})


@require_http_methods(["GET", "DELETE", "PATCH"])
@csrf_exempt
def conversation_detail(request, conv_id):
    """
    GET    /api/conversations/<uuid>/  — full message history
    DELETE /api/conversations/<uuid>/  — delete conversation
    PATCH  /api/conversations/<uuid>/  — rename (body: {"title": "..."})
    """
    try:
        conv = Conversation.objects.get(pk=conv_id)
    except Conversation.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    # Ownership
    session_key = request.session.session_key or ""
    is_owner = (
        (conv.user and request.user.is_authenticated and conv.user == request.user)
        or
        (not conv.user and conv.session_key == session_key)
    )
    if not is_owner:
        return JsonResponse({"error": "Forbidden"}, status=403)

    if request.method == "DELETE":
        conv.delete()
        return JsonResponse({"ok": True})

    if request.method == "PATCH":
        try:
            body  = json.loads(request.body)
            title = body.get("title", "").strip()
            if title:
                conv.title = title[:200]
                conv.save(update_fields=["title"])
        except (json.JSONDecodeError, KeyError):
            return JsonResponse({"error": "Invalid body"}, status=400)
        return JsonResponse({"id": str(conv.id), "title": conv.title})

    # GET — return messages
    msgs = list(conv.messages.order_by("created_at").values(
        "role", "content", "source_chunks", "created_at"
    ))
    return JsonResponse({
        "id":       str(conv.id),
        "title":    conv.title,
        "messages": msgs,
    })


# ---------------------------------------------------------------------------
# utility actions
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@csrf_exempt
def reset_chat(request):
    """
    Start a fresh conversation.  Deletes the conversation referenced by
    `conversation_id` in the request body (if owned by this user/session).
    """
    try:
        data    = json.loads(request.body) if request.body else {}
        conv_id = data.get("conversation_id")
    except json.JSONDecodeError:
        conv_id = None

    if conv_id:
        session_key = request.session.session_key or ""
        qs = Conversation.objects.filter(pk=conv_id)
        if request.user.is_authenticated:
            qs = qs.filter(user=request.user)
        else:
            qs = qs.filter(session_key=session_key, user__isnull=True)
        qs.delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.body:
        return JsonResponse({"success": True})
    return redirect("chat")


@require_http_methods(["POST"])
@csrf_exempt
def reindex(request):
    django_messages.info(request, "Use: python manage.py ingest_gdrive --folder-id <FOLDER_ID>")
    return redirect("chat")


# ---------------------------------------------------------------------------
# auth views
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def register_view(request):
    print(request)
    if request.user.is_authenticated:
        return redirect("chat")

    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("chat")
    else:
        form = UserCreationForm()

    return render(request, "chat/register.html", {"form": form})


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("chat")

    next_url = request.GET.get("next", "chat")

    if request.method == "POST":
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            # signal in signals.py will claim anon conversations automatically
            return redirect(next_url)
    else:
        form = AuthenticationForm()

    return render(request, "chat/login.html", {"form": form, "next": next_url})


@require_http_methods(["POST"])
@csrf_exempt
def logout_view(request):
    logout(request)
    return redirect("chat")