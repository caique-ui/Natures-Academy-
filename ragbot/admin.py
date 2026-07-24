from django.contrib import admin
from .models import IndexVersion, SourceDocument, DriveSync, DriveSyncEvent, ScrapedURL

@admin.register(IndexVersion)
class IndexVersionAdmin(admin.ModelAdmin):
    list_display  = ["version_number", "folder_id", "status", "is_active",
                     "chunks_indexed", "files_processed", "created_at"]
    list_filter   = ["status", "is_active"]
    readonly_fields = ["created_at", "completed_at"]

@admin.register(DriveSync)
class DriveSyncAdmin(admin.ModelAdmin):
    list_display  = ["folder_id", "last_checked_at", "last_change_at",
                     "last_indexed_at", "is_indexing", "debounce_seconds"]
    readonly_fields = ["page_token", "folder_fingerprint", "pending_file_ids",
                       "created_at", "updated_at"]

@admin.register(DriveSyncEvent)
class DriveSyncEventAdmin(admin.ModelAdmin):
    list_display  = ["created_at", "folder_id", "event_type", "detail"]
    list_filter   = ["event_type"]
    readonly_fields = ["created_at"]

@admin.register(SourceDocument)
class SourceDocumentAdmin(admin.ModelAdmin):
    list_display  = ["drive_file_name", "mime_type", "chunk_count", "version"]
    list_filter   = ["mime_type"]

@admin.register(ScrapedURL)
class ScrapedURLAdmin(admin.ModelAdmin):
    list_display  = ["url", "title", "description"]
    list_filter   = ["title"]