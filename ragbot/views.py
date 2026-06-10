# ragbot/views.py — with folders support + sources page

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .conversation_rag import conversational_rag_stream, conversational_rag_answer
from .forms import ChatForm
from .models import Conversation, Folder, IndexVersion, Message, SourceDocument
from .vectorstore_db import get_db_store, invalidate_db_store_cache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_or_create_conversation(request, conv_id, folder_id=None):
    if not request.session.session_key:
        request.session.create()
    session_key = request.session.session_key

    if conv_id:
        try:
            conv = Conversation.objects.get(pk=conv_id)
            if conv.user:
                if not request.user.is_authenticated or conv.user != request.user:
                    raise PermissionError
            else:
                if conv.session_key != session_key:
                    raise PermissionError
            return conv
        except (Conversation.DoesNotExist, PermissionError):
            pass

    # Resolve folder if provided (authenticated users only)
    folder = None
    if folder_id and request.user.is_authenticated:
        try:
            folder = Folder.objects.get(pk=folder_id, user=request.user)
        except Exception:
            folder = None

    return Conversation.objects.create(
        user=request.user if request.user.is_authenticated else None,
        session_key=session_key,
        folder=folder,
    )


def _sidebar_data(request) -> dict:
    """
    Return everything the sidebar needs: folders with their conversations
    nested inside, plus unfiled conversations.
    """
    if not request.user.is_authenticated:
        session_key = request.session.session_key
        if not session_key:
            return {"folders": [], "unfiled": []}
        convs = Conversation.objects.filter(
            session_key=session_key, user__isnull=True
        ).order_by("-updated_at")[:50]
        return {
            "folders": [],
            "unfiled": [_conv_dict(c) for c in convs],
        }

    # Authenticated: folders + conversations per folder + unfiled
    folders = Folder.objects.filter(user=request.user).prefetch_related(
        "conversations"
    )
    folder_list = []
    for f in folders:
        folder_list.append({
            "id":    str(f.id),
            "name":  f.name,
            "conversations": [
                _conv_dict(c)
                for c in f.conversations.order_by("-updated_at")[:50]
            ],
        })

    unfiled = Conversation.objects.filter(
        user=request.user, folder__isnull=True
    ).order_by("-updated_at")[:50]

    return {
        "folders": folder_list,
        "unfiled": [_conv_dict(c) for c in unfiled],
    }


def _conv_dict(c: Conversation) -> dict:
    return {
        "id":         str(c.id),
        "title":      c.title,
        "folder_id":  str(c.folder_id) if c.folder_id else None,
        "updated_at": c.updated_at.isoformat(),
    }


def _owns_conversation(request, conv: Conversation) -> bool:
    session_key = request.session.session_key or ""
    if conv.user:
        return request.user.is_authenticated and conv.user == request.user
    return conv.session_key == session_key


def _owns_folder(request, folder: Folder) -> bool:
    return request.user.is_authenticated and folder.user == request.user


# ---------------------------------------------------------------------------
# main chat view
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def chat_view(request):
    if not request.session.session_key:
        request.session.create()

    sidebar   = _sidebar_data(request)
    form      = ChatForm()

    active_conv_id  = request.GET.get("conv")
    active_messages = []
    if active_conv_id:
        try:
            conv = Conversation.objects.get(pk=active_conv_id)
            if _owns_conversation(request, conv):
                active_messages = list(conv.messages.order_by("created_at").values(
                    "role", "content", "source_chunks"
                ))
            else:
                active_conv_id = None
        except Conversation.DoesNotExist:
            active_conv_id = None

    return render(request, "chat/index.html", {
        "form":            form,
        "sidebar":         sidebar,
        "active_conv_id":  active_conv_id,
        "active_messages": active_messages,
    })


# ---------------------------------------------------------------------------
# streaming send
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@csrf_exempt
def send_message_stream(request):
    try:
        data      = json.loads(request.body)
        user_msg  = data.get("message", "").strip()
        conv_id   = data.get("conversation_id")
        folder_id = data.get("folder_id")

        if not user_msg:
            return JsonResponse({"error": "Message cannot be empty"}, status=400)

        conv = _get_or_create_conversation(request, conv_id, folder_id)

        if conv.title == "New conversation":
            conv.title = user_msg[:60]
            conv.save(update_fields=["title"])

        Message.objects.create(conversation=conv, role=Message.ROLE_USER, content=user_msg)

        def generate():
            import json as _json
            full_response = ""
            sources       = []

            for chunk in conversational_rag_stream(user_msg, conv):
                yield chunk
                if chunk.startswith("data: "):
                    try:
                        payload = _json.loads(chunk[6:])
                        if payload.get("type") == "done":
                            full_response = payload.get("full_response", "")
                            sources       = payload.get("sources", [])
                    except Exception:
                        pass

            try:
                Message.objects.create(
                    conversation=conv,
                    role=Message.ROLE_ASSISTANT,
                    content=full_response,
                    source_chunks=sources,
                )
                conv.save()
                yield f"data: {_json.dumps({'type': 'conversation_meta', 'conversation_id': str(conv.id), 'conversation_title': conv.title, 'folder_id': str(conv.folder_id) if conv.folder_id else None})}\n\n"
            except Exception as exc:
                logger.error("Failed to persist assistant message: %s", exc)

        response = StreamingHttpResponse(generate(), content_type="text/event-stream")
        response["Cache-Control"]     = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    except Exception as exc:
        logger.error("send_message_stream error: %s", exc, exc_info=True)
        return JsonResponse({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# non-streaming fallback
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@csrf_exempt
def send_message(request):
    try:
        data      = json.loads(request.body)
        user_msg  = data.get("message", "").strip()
        conv_id   = data.get("conversation_id")
        folder_id = data.get("folder_id")

        if not user_msg:
            return JsonResponse({"error": "Message cannot be empty"}, status=400)

        conv = _get_or_create_conversation(request, conv_id, folder_id)

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
            "success":             True,
            "user_message":        user_msg,
            "assistant_message":   result["answer"],
            "sources":             result["sources"],
            "conversation_id":     str(conv.id),
            "conversation_title":  conv.title,
            "folder_id":           str(conv.folder_id) if conv.folder_id else None,
        })

    except Exception as exc:
        logger.error("send_message error: %s", exc, exc_info=True)
        return JsonResponse({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# conversation API
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def conversation_list(request):
    return JsonResponse(_sidebar_data(request))


@require_http_methods(["GET", "DELETE", "PATCH"])
@csrf_exempt
def conversation_detail(request, conv_id):
    try:
        conv = Conversation.objects.get(pk=conv_id)
    except Conversation.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    if not _owns_conversation(request, conv):
        return JsonResponse({"error": "Forbidden"}, status=403)

    if request.method == "DELETE":
        conv.delete()
        return JsonResponse({"ok": True})

    if request.method == "PATCH":
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid body"}, status=400)

        if "title" in body and body["title"].strip():
            conv.title = body["title"].strip()[:200]

        if "folder_id" in body:
            fid = body["folder_id"]
            if fid is None:
                conv.folder = None
            else:
                try:
                    folder = Folder.objects.get(pk=fid, user=request.user)
                    conv.folder = folder
                except Folder.DoesNotExist:
                    return JsonResponse({"error": "Folder not found"}, status=404)

        conv.save()
        return JsonResponse(_conv_dict(conv))

    # GET
    msgs = list(conv.messages.order_by("created_at").values(
        "role", "content", "source_chunks", "created_at"
    ))
    return JsonResponse({"id": str(conv.id), "title": conv.title, "messages": msgs})


# ---------------------------------------------------------------------------
# folder API
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
@csrf_exempt
def folder_list(request):
    """GET list / POST create"""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    if request.method == "POST":
        try:
            body = json.loads(request.body)
            name = body.get("name", "").strip()[:100]
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid body"}, status=400)

        if not name:
            return JsonResponse({"error": "Name required"}, status=400)

        folder, created = Folder.objects.get_or_create(user=request.user, name=name)
        if not created:
            return JsonResponse({"error": "A folder with that name already exists"}, status=409)

        return JsonResponse({"id": str(folder.id), "name": folder.name}, status=201)

    folders = Folder.objects.filter(user=request.user).values("id", "name")
    return JsonResponse({"folders": [
        {"id": str(f["id"]), "name": f["name"]} for f in folders
    ]})


@require_http_methods(["DELETE", "PATCH"])
@csrf_exempt
def folder_detail(request, folder_id):
    """PATCH rename / DELETE (conversations move to unfiled)"""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    try:
        folder = Folder.objects.get(pk=folder_id, user=request.user)
    except Folder.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    if request.method == "DELETE":
        folder.delete()
        return JsonResponse({"ok": True})

    if request.method == "PATCH":
        try:
            body = json.loads(request.body)
            name = body.get("name", "").strip()[:100]
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid body"}, status=400)

        if not name:
            return JsonResponse({"error": "Name required"}, status=400)

        folder.name = name
        try:
            folder.save()
        except Exception:
            return JsonResponse({"error": "A folder with that name already exists"}, status=409)

        return JsonResponse({"id": str(folder.id), "name": folder.name})


# ---------------------------------------------------------------------------
# sources page (staff only)
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def sources_view(request):
    """
    /sources/ — lists every IndexVersion and its SourceDocuments.
    Staff-only. Supports activating a version via POST.
    """
    if not request.user.is_staff:
        return HttpResponseForbidden("Staff access only.")

    if request.method == "POST":
        vid = request.POST.get("activate_version_id")
        if vid:
            try:
                v = IndexVersion.objects.get(pk=vid, status=IndexVersion.Status.COMPLETED)
                v.activate()
                invalidate_db_store_cache(v.folder_id)
                django_messages.success(
                    request,
                    f'Version v{v.version_number} of "{v.folder_name or v.folder_id}" activated.'
                )
            except IndexVersion.DoesNotExist:
                django_messages.error(request, "Version not found or not completed.")
        return redirect("sources")

    # All completed versions, prefetch their documents for the table
    versions = (
        IndexVersion.objects
        .filter(status=IndexVersion.Status.COMPLETED)
        .prefetch_related("documents")
        .order_by("folder_id", "-version_number")
    )

    return render(request, "chat/sources.html", {"versions": versions})


# ---------------------------------------------------------------------------
# utility
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@csrf_exempt
def reset_chat(request):
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
            login(request, form.get_user())
            return redirect(next_url)
    else:
        form = AuthenticationForm()
    return render(request, "chat/login.html", {"form": form, "next": next_url})


@require_http_methods(["POST"])
@csrf_exempt
def logout_view(request):
    logout(request)
    return redirect("chat")