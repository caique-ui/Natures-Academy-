# ragbot/urls.py
from django.urls import path
from .views import chat_view, reset_chat, reindex

urlpatterns = [
    path("", chat_view, name="chat"),
    path("reset/", reset_chat, name="reset_chat"),
    path("reindex/", reindex, name="reindex"),
]
