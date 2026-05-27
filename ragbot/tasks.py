"""
ragbot/tasks.py

Celery tasks for automated Drive sync.

Task graph:
  check_drive_changes_task   → runs frequently (e.g. every hour)
                               detects what changed, records it, schedules indexing
  debounced_index_task       → scheduled by check task after debounce window
                               runs the actual ingestion if changes still pending
  webhook_received_task      → triggered by Drive push notification
                               records the event and schedules debounced_index_task

All tasks are idempotent — safe to run multiple times.
"""

from __future__ import annotations

import logging
from typing import Optional

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


def _get_drive_service(max_retries: int = 3):
    """
    Build an authenticated Drive service using ADC — identical to
    gdrive_ingest._creds_with_retry() so both use the same auth path.
    Works locally (gcloud auth application-default login) and on GCP
    (attached service account).
    """
    import socket
    import time
    from google.auth import default
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    for attempt in range(max_retries):
        try:
            socket.setdefaulttimeout(30)
            credentials, _ = default(scopes=SCOPES)
            credentials.refresh(Request())
            return build("drive", "v3", credentials=credentials, cache_discovery=False)
        except socket.timeout:
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 5)
            else:
                raise
        except Exception as exc:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise


# ---------------------------------------------------------------------------
# Task 1: Check for changes (run frequently — no heavy work)
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def check_drive_changes_task(self, folder_id: Optional[str] = None):
    """
    1. Call Drive Changes API with stored pageToken
    2. Record changed file IDs in DriveSync.pending_file_ids
    3. If changes found, schedule debounced_index_task
    """
    from ragbot.models import DriveSync, DriveSyncEvent
    from ragbot.drive_sync import (
        fetch_drive_changes,
        get_initial_page_token,
    )

    fid = folder_id or settings.GDRIVE_DEFAULT_FOLDER_ID

    try:
        drive = _get_drive_service()

        sync, created = DriveSync.objects.get_or_create(
            folder_id=fid,
            defaults={"folder_name": fid},
        )

        # First time — grab a start token and return; next run will catch real changes
        if not sync.page_token:
            token = get_initial_page_token(drive)
            sync.page_token = token
            sync.last_checked_at = timezone.now()
            sync.save(update_fields=["page_token", "last_checked_at"])
            logger.info(f"[{fid}] Initialised page token. Next check will detect changes.")
            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.CHECK,
                detail={"action": "initialised_token"},
            )
            return {"status": "initialised"}

        new_token, changed_ids = fetch_drive_changes(drive, sync.page_token, fid)

        # Merge with any previously pending IDs
        existing = set(sync.pending_file_ids)
        existing.update(changed_ids)
        pending = list(existing)

        sync.page_token      = new_token
        sync.last_checked_at = timezone.now()

        if changed_ids:
            sync.pending_file_ids = pending
            sync.last_change_at   = timezone.now()
            sync.save(update_fields=[
                "page_token", "last_checked_at",
                "pending_file_ids", "last_change_at",
            ])

            logger.info(f"[{fid}] {len(changed_ids)} changed files detected. "
                        f"Scheduling index after debounce ({sync.debounce_seconds}s).")

            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.CHECK,
                detail={"changed_ids": changed_ids, "total_pending": len(pending)},
            )

            # Schedule the index to run after the debounce window
            debounced_index_task.apply_async(
                kwargs={"folder_id": fid},
                countdown=sync.debounce_seconds,
            )
        else:
            sync.save(update_fields=["page_token", "last_checked_at"])
            logger.info(f"[{fid}] No changes detected.")
            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.INDEX_SKIP,
                detail={"reason": "no_changes_from_api"},
            )

        return {"status": "checked", "changed": len(changed_ids)}

    except Exception as exc:
        logger.error(f"[{fid}] check_drive_changes_task failed: {exc}", exc_info=True)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Task 2: Debounced index (the heavy work)
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def debounced_index_task(self, folder_id: Optional[str] = None):
    """
    Run ingestion only if:
      - debounce window has passed
      - no other worker is indexing
      - folder fingerprint actually changed
    """
    from ragbot.models import DriveSync, DriveSyncEvent
    from ragbot.drive_sync import compute_folder_fingerprint, should_reindex
    from ragbot.vectorstore_db import DBVectorStore, invalidate_db_store_cache
    from ragbot.gdrive_ingest import iter_drive_files, fetch_text_for_file
    from ragbot.textsplit import chunk_text
    import gc

    fid = folder_id or settings.GDRIVE_DEFAULT_FOLDER_ID

    try:
        sync = DriveSync.objects.get(folder_id=fid)
    except DriveSync.DoesNotExist:
        logger.warning(f"[{fid}] No DriveSync record found.")
        return {"status": "no_sync_record"}

    # ── Compute current fingerprint ──────────────────────────────────────
    try:
        drive = _get_drive_service()
        current_fingerprint, _ = compute_folder_fingerprint(drive, fid)
    except Exception as exc:
        logger.error(f"[{fid}] Failed to compute fingerprint: {exc}")
        raise self.retry(exc=exc)

    # ── Decision: should we index? ───────────────────────────────────────
    do_index, reason = should_reindex(sync, current_fingerprint)

    if not do_index:
        logger.info(f"[{fid}] Skipping index: {reason}")
        DriveSyncEvent.objects.create(
            folder_id=fid,
            event_type=(
                DriveSyncEvent.EventType.DEBOUNCE   if "debounce"  in reason else
                DriveSyncEvent.EventType.LOCK_BUSY  if "lock_busy" in reason else
                DriveSyncEvent.EventType.INDEX_SKIP
            ),
            detail={"reason": reason},
        )

        # If debounce is still active, reschedule for after the window
        if "debounce" in reason and sync.last_change_at:
            remaining = sync.debounce_seconds - (
                timezone.now() - sync.last_change_at
            ).total_seconds()
            debounced_index_task.apply_async(
                kwargs={"folder_id": fid},
                countdown=max(int(remaining) + 10, 30),
            )

        return {"status": "skipped", "reason": reason}

    # ── Acquire lock ─────────────────────────────────────────────────────
    if not sync.acquire_index_lock():
        logger.info(f"[{fid}] Another worker holds the lock. Exiting.")
        DriveSyncEvent.objects.create(
            folder_id=fid,
            event_type=DriveSyncEvent.EventType.LOCK_BUSY,
            detail={},
        )
        return {"status": "skipped", "reason": "lock_busy"}

    DriveSyncEvent.objects.create(
        folder_id=fid,
        event_type=DriveSyncEvent.EventType.INDEX_START,
        detail={"pending_files": sync.pending_file_ids, "fingerprint": current_fingerprint},
    )

    # ── Run ingestion ────────────────────────────────────────────────────
    store = DBVectorStore(
        folder_id=fid,
        embed_model=settings.OPENAI_EMBED_MODEL,
        chunk_max_chars=getattr(settings, "CHUNK_MAX_CHARS", 1500),
        chunk_overlap=getattr(settings, "CHUNK_OVERLAP", 150),
    )

    try:
        version = store.begin_version(folder_name=sync.folder_name)
        added = skipped = 0

        for f in iter_drive_files(fid):
            text, meta = fetch_text_for_file(f)
            if not text.strip():
                skipped += 1
                continue

            chunks = chunk_text(
                text,
                max_chars=getattr(settings, "CHUNK_MAX_CHARS", 1500),
                overlap=getattr(settings, "CHUNK_OVERLAP", 150),
            )
            metadatas = [
                {"source_id": meta["id"], "source_name": meta["name"],
                 "mime": meta["mime"], "chunk": i, "text": c}
                for i, c in enumerate(chunks)
            ]
            store.add_texts(chunks, metadatas)
            added += 1
            gc.collect()

        if not store._pending_chunks:
            store.mark_failed("No chunks produced")
            raise ValueError("No text extracted from any file.")

        store.save(activate=True)
        invalidate_db_store_cache(fid)

        # ── Update DriveSync state ────────────────────────────────────────
        DriveSync.objects.filter(pk=sync.pk).update(
            folder_fingerprint=current_fingerprint,
            pending_file_ids=[],
            last_indexed_at=timezone.now(),
        )

        DriveSyncEvent.objects.create(
            folder_id=fid,
            event_type=DriveSyncEvent.EventType.INDEX_DONE,
            detail={
                "version": version.version_number,
                "files_added": added,
                "files_skipped": skipped,
                "chunks": version.chunks_indexed,
                "fingerprint": current_fingerprint,
            },
        )
        logger.info(f"[{fid}] ✅ Index v{version.version_number} complete. "
                    f"{added} files, {version.chunks_indexed} chunks.")
        return {"status": "indexed", "version": version.version_number}

    except Exception as exc:
        store.mark_failed(str(exc))
        DriveSyncEvent.objects.create(
            folder_id=fid,
            event_type=DriveSyncEvent.EventType.INDEX_FAIL,
            detail={"error": str(exc)},
        )
        logger.error(f"[{fid}] Indexing failed: {exc}", exc_info=True)
        raise self.retry(exc=exc)

    finally:
        sync.release_index_lock()


# ---------------------------------------------------------------------------
# Task 3: Triggered by Drive push webhook (lightweight)
# ---------------------------------------------------------------------------

@shared_task
def webhook_received_task(folder_id: str, resource_id: str, resource_state: str):
    """
    Called immediately when a Drive push notification arrives.
    Does NOT index — just records the event and triggers the check task,
    which will schedule debounced indexing.
    """
    from ragbot.models import DriveSync, DriveSyncEvent

    logger.info(f"[{folder_id}] Webhook: state={resource_state} resource={resource_id}")

    sync, _ = DriveSync.objects.get_or_create(
        folder_id=folder_id,
        defaults={"folder_name": folder_id},
    )
    sync.last_change_at = timezone.now()
    if folder_id not in (sync.pending_file_ids or []):
        sync.pending_file_ids = list(sync.pending_file_ids or []) + [resource_id]
    sync.save(update_fields=["last_change_at", "pending_file_ids"])

    DriveSyncEvent.objects.create(
        folder_id=folder_id,
        event_type=DriveSyncEvent.EventType.WEBHOOK,
        detail={"resource_id": resource_id, "resource_state": resource_state},
    )

    # Schedule the debounced index (will no-op if debounce window isn't over yet)
    debounced_index_task.apply_async(
        kwargs={"folder_id": folder_id},
        countdown=sync.debounce_seconds,
    )