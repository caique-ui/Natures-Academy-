from django.urls import path
from . import views
from ragbot.webhook_views import drive_webhook

urlpatterns = [
    # ── Main chat UI ─────────────────────────────────────────────────────
    path("", views.chat_view, name="chat"),

    # ── Message endpoints ────────────────────────────────────────────────
    path("send/",        views.send_message,        name="send_message"),
    path("send-stream/", views.send_message_stream, name="send_message_stream"),
    path("reset/",       views.reset_chat,          name="reset_chat"),

    # ── Conversation management API ──────────────────────────────────────
    path("api/conversations/",             views.conversation_list,   name="conversation_list"),
    path("api/conversations/<uuid:conv_id>/", views.conversation_detail, name="conversation_detail"),

    # ── Auth ─────────────────────────────────────────────────────────────
    path("auth/register/", views.register_view, name="register"),
    path("auth/login/",    views.login_view,    name="login"),
    path("auth/logout/",   views.logout_view,   name="logout"),

    # ── Existing ─────────────────────────────────────────────────────────
    path("reindex/",                          views.reindex,    name="reindex"),
    path("webhooks/drive/notify/",            drive_webhook,    name="drive_webhook"),

    # ── Folders ───────────────────────────────────────────────────────────
    path("api/folders/",                          views.folder_list,   name="folder_list"),
    path("api/folders/<uuid:folder_id>/",         views.folder_detail, name="folder_detail"),

    # ── Sources page (staff only) ─────────────────────────────────────────
    path("sources/", views.sources_view, name="sources"),
]