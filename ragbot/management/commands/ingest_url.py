from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from ragbot.tasks import scrape_web_source_task

class Command(BaseCommand):
    help = "Index a PDF file from remote URL into the RAG vector store"

    def handle(self, *args, **options):
        scrape_web_source_task()