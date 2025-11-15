# ragbot/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('', views.chat_view, name='chat'),
    path('send/', views.send_message, name='send_message'),  # Keep existing
    path('send-stream/', views.send_message_stream, name='send_message_stream'),  # New streaming
    path('reset/', views.reset_chat, name='reset_chat'),
    path('reindex/', views.reindex, name='reindex'),
]