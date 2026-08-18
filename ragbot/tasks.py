"""
ragbot/tasks.py  — FULL UPDATED FILE
=====================================
Changes from original:
  1. scrape_web_source_task accepts optional bundle_id param,
     registers its IndexVersion with the NightlyBundle on completion.
  2. Two new tasks added at the bottom:
       - nightly_scrape_orchestrator_task  (replaces beat schedule entry)
       - nightly_activate_bundle_task      (fires after all domains finish)
  3. Everything else is unchanged.
"""

from __future__ import annotations

import hashlib
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
    import socket
    import time
    from google.auth import default, compute_engine
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    for attempt in range(max_retries):
        try:
            socket.setdefaulttimeout(30)
            if settings.APP_ENV == "local":
                credentials, _ = default(scopes=SCOPES)
            else:
                credentials = compute_engine.Credentials()
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
# Task 1: Check for Drive changes
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def check_drive_changes_task(self, folder_id: Optional[str] = None):
    from ragbot.models import DriveSync, DriveSyncEvent
    from ragbot.drive_sync import fetch_drive_changes, get_initial_page_token

    fid = folder_id or settings.GDRIVE_DEFAULT_FOLDER_ID

    try:
        drive = _get_drive_service()
        sync, created = DriveSync.objects.get_or_create(
            folder_id=fid,
            defaults={"folder_name": fid},
        )

        if not sync.page_token:
            token = get_initial_page_token(drive)
            sync.page_token = token
            sync.last_checked_at = timezone.now()
            sync.save(update_fields=["page_token", "last_checked_at"])
            logger.info(f"[{fid}] Initialised page token.")
            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.CHECK,
                detail={"action": "initialised_token"},
            )
            return {"status": "initialised"}

        new_token, changed_ids = fetch_drive_changes(drive, sync.page_token, fid)

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
            logger.info(f"[{fid}] {len(changed_ids)} changed files.")
            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.CHECK,
                detail={"changed_ids": changed_ids, "total_pending": len(pending)},
            )
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
# Task 2: Debounced Drive index
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def debounced_index_task(self, folder_id: Optional[str] = None):
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

    try:
        drive = _get_drive_service()
        current_fingerprint, _ = compute_folder_fingerprint(drive, fid)
    except Exception as exc:
        logger.error(f"[{fid}] Failed to compute fingerprint: {exc}")
        raise self.retry(exc=exc)

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
        if "debounce" in reason and sync.last_change_at:
            remaining = sync.debounce_seconds - (
                timezone.now() - sync.last_change_at
            ).total_seconds()
            debounced_index_task.apply_async(
                kwargs={"folder_id": fid},
                countdown=max(int(remaining) + 10, 30),
            )
        return {"status": "skipped", "reason": reason}

    if not sync.acquire_index_lock():
        logger.info(f"[{fid}] Another worker holds the lock.")
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
            chunks    = chunk_text(text, max_chars=getattr(settings, "CHUNK_MAX_CHARS", 1500),
                                   overlap=getattr(settings, "CHUNK_OVERLAP", 150))
            metadatas = [
                {
                    "source_id":   meta["id"],
                    "source_name": meta["name"],
                    "mime":        meta["mime"],
                    "source_url":  f"https://drive.google.com/file/d/{meta['id']}",
                    "source_type": "drive",
                    "chunk":       i,
                    "text":        c,
                }
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

        DriveSync.objects.filter(pk=sync.pk).update(
            folder_fingerprint=current_fingerprint,
            pending_file_ids=[],
            last_indexed_at=timezone.now(),
        )
        DriveSyncEvent.objects.create(
            folder_id=fid,
            event_type=DriveSyncEvent.EventType.INDEX_DONE,
            detail={
                "version":       version.version_number,
                "files_added":   added,
                "files_skipped": skipped,
                "chunks":        version.chunks_indexed,
                "fingerprint":   current_fingerprint,
            },
        )
        logger.info(f"[{fid}] ✅ Index v{version.version_number} complete.")
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
# Task 3: Drive push webhook
# ---------------------------------------------------------------------------

@shared_task
def webhook_received_task(folder_id: str, resource_id: str, resource_state: str):
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
    debounced_index_task.apply_async(
        kwargs={"folder_id": folder_id},
        countdown=sync.debounce_seconds,
    )


# ---------------------------------------------------------------------------
# Task 4: Scrape & index a single web source
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def scrape_web_source_task(self, root_url: str = None, bundle_id: int = None, mode: str = None):
    """
    Scrape a single root_url and index it.

    mode (optional):
        "sitemap" — discover sitemap, fetch all listed URLs (default from settings).
        "crawl"   — BFS crawl from root_url with no page limit.
        None      — uses settings.SCRAPING_MODE (default: "sitemap").

    bundle_id (optional):
        If provided, the resulting IndexVersion is registered with the
        NightlyBundle so the orchestrator can track progress and activate
        all domains together at the end of the night.
        If None (e.g. manual one-off trigger), the version is activated
        immediately on its own — same behaviour as before.

    Fan-out mode (root_url is None):
        Dispatches one sub-task per URL in settings.SCRAPING_URLS.
        Used for manual triggering outside the nightly orchestrator.

    Large-site handling (e.g. 15,000+ page sitemaps):
        Pages are streamed from the crawler in batches (settings.SCRAPE_BATCH_SIZE,
        default 100) and embedded+persisted to the DB as they arrive, instead of
        holding the entire site's text + embeddings in memory until the end.
        Each page is only marked scraped (ScrapedURL) *after* its batch is
        actually written to the DB — so a crash mid-crawl can't leave pages
        falsely marked "already scraped" with nothing actually persisted.
        Per-page content-hash comparison against ScrapedURL means unchanged
        pages are skipped (no re-embedding cost) without needing a whole-site
        fingerprint gate up front.
    """
    from ragbot.models import WebSyncState, NightlyBundle, ScrapedURL
    from ragbot.web_scraper import fetch_web_source, compute_web_fingerprint
    from ragbot.vectorstore_db import DBVectorStore, invalidate_db_store_cache
    from ragbot.textsplit import chunk_text
    import gc

    # ── Fan-out mode ─────────────────────────────────────────────────────
    if root_url is None:
        urls = getattr(settings, "SCRAPING_URLS", [])
        if not urls:
            logger.warning("SCRAPING_URLS is empty — nothing to scrape.")
            return {"status": "no_urls"}
        for url in urls:
            #scrape_web_source_task.delay(url)
            scrape_web_source_task(url)
            logger.info(f"Dispatched scrape task for: {url}")
        return {"status": "dispatched", "count": len(urls)}

    # ── Single URL mode ───────────────────────────────────────────────────
    url_hash  = hashlib.md5(root_url.encode()).hexdigest()[:12]
    folder_id = f"web:{url_hash}"

    sync, _ = WebSyncState.objects.get_or_create(
        root_url=root_url,
        defaults={
            "label":     root_url,
            "folder_id": folder_id,
        },
    )
    fid = sync.folder_id

    # Resolve bundle if provided
    bundle = None
    if bundle_id is not None:
        try:
            bundle = NightlyBundle.objects.get(pk=bundle_id)
        except NightlyBundle.DoesNotExist:
            logger.warning(f"[{fid}] bundle_id={bundle_id} not found — proceeding without bundle.")

    # ── Acquire lock ──────────────────────────────────────────────────────
    # Held for the ENTIRE crawl now (not just the final indexing step), since
    # pages are embedded+persisted incrementally as they're crawled rather
    # than all at once at the end. refresh_index_lock() is heartbeated once
    # per batch below so a long crawl (many hours for 15,000+ pages) doesn't
    # get treated as stale and picked up by a second worker.
    if not sync.acquire_index_lock():
        logger.info(f"[{fid}] Another worker holds the lock — exiting.")
        return {"status": "skipped", "reason": "lock_busy"}

    chunk_max_chars = getattr(settings, "CHUNK_MAX_CHARS", 1500)
    chunk_overlap   = getattr(settings, "CHUNK_OVERLAP", 150)
    batch_size      = getattr(settings, "SCRAPE_BATCH_SIZE", 100)
    force_recrawl   = getattr(settings, "SCRAPING_FORCE_RECRAWL", False)

    store = DBVectorStore(
        folder_id=fid,
        embed_model=settings.OPENAI_EMBED_MODEL,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
    )

    added             = 0   # pages actually (re-)embedded this run
    skipped_empty     = 0   # pages with no extractable text
    skipped_unchanged = 0   # pages whose content_hash matches last scrape

    try:
        version = store.begin_version(folder_name=sync.label or root_url)

        def handle_batch(batch_pages: list[dict]) -> None:
            nonlocal added, skipped_empty, skipped_unchanged

            # One batched lookup instead of one query per page.
            urls_in_batch   = [p["url"] for p in batch_pages]
            existing_hashes = dict(
                ScrapedURL.objects.filter(url__in=urls_in_batch)
                                  .values_list("url", "content_hash")
            )

            scraped_this_batch = []  # pages to mark_scraped AFTER the DB write below succeeds
            for page in batch_pages:
                text = page.get("text", "")
                if not text.strip():
                    skipped_empty += 1
                    continue

                content_hash = page.get("content_hash", "")
                if (not force_recrawl and content_hash
                        and existing_hashes.get(page["url"]) == content_hash):
                    # Unchanged since last scrape — skip re-embedding, but still
                    # refresh its ScrapedURL row so the 24h cache stays accurate.
                    skipped_unchanged += 1
                    scraped_this_batch.append(page)
                    continue

                chunks    = chunk_text(text, max_chars=chunk_max_chars, overlap=chunk_overlap)
                metadatas = [
                    {
                        "source_id":   page["url"],
                        "source_name": page.get("title", page["url"]),
                        "mime":        "text/html",
                        "source_url":  page["url"],
                        "source_type": "web",
                        "parent_url":  page.get("parent_url"),   # parent/child tracking
                        "chunk":       i,
                        "text":        c,
                    }
                    for i, c in enumerate(chunks)
                ]
                store.add_texts(chunks, metadatas)
                added += 1
                scraped_this_batch.append(page)

            # Persist this batch now — keeps memory bounded regardless of
            # total site size (15,000+ pages no longer held in memory at once).
            store.write_pending_batch()
            gc.collect()

            # Only mark URLs scraped AFTER the write above succeeded — if it
            # raised, we never reach here, so a mid-crawl crash can't leave a
            # page falsely marked "scraped" with nothing actually persisted.
            for page in scraped_this_batch:
                try:
                    ScrapedURL.mark_scraped(
                        page["url"],
                        content_hash=page.get("content_hash", ""),
                        version=version,
                    )
                except Exception:
                    pass

            sync.refresh_index_lock()

        scrape_mode = mode or getattr(settings, "SCRAPING_MODE", "sitemap")
        logger.info(f"[{fid}] Starting web scrape: {root_url} (mode={scrape_mode})")
        page_meta = fetch_web_source(
            root_url, mode=scrape_mode, force_recrawl=force_recrawl,
            on_batch=handle_batch, batch_size=batch_size, version=version,
        )

        if not page_meta:
            logger.warning(f"[{fid}] No pages scraped from {root_url}")
            store.mark_failed("No pages scraped")
            if bundle is not None:
                bundle.versions.add(version)
            WebSyncState.objects.filter(pk=sync.pk).update(last_checked_at=timezone.now())
            return {"status": "no_pages"}

        if added == 0:
            # Real crawl completed, but every page was unchanged (or empty) —
            # nothing new to index. Don't activate an empty version over the
            # perfectly good, already-active one from a previous run.
            logger.info(
                f"[{fid}] No changed pages to index — {skipped_unchanged} unchanged, "
                f"{skipped_empty} empty. Leaving existing active version in place."
            )
            version.delete()  # discard the unused RUNNING version row
            WebSyncState.objects.filter(pk=sync.pk).update(last_checked_at=timezone.now())
            return {"status": "skipped", "reason": "no_changed_pages"}

        # activate=True only when running outside the bundle (manual/one-off).
        # Inside the bundle, activation is handled by nightly_activate_bundle_task
        # after ALL domains finish — so individual versions are NOT activated here.
        activate_now = bundle is None
        store.finalize(activate=activate_now)
        invalidate_db_store_cache(fid)

        # ── Register version with bundle ──────────────────────────────────
        if bundle is not None:
            bundle.versions.add(version)
            logger.info(f"[{fid}] Registered v{version.version_number} with bundle {bundle.date}.")

        new_fingerprint = compute_web_fingerprint(page_meta)
        WebSyncState.objects.filter(pk=sync.pk).update(
            content_fingerprint=new_fingerprint,
            last_checked_at=timezone.now(),
            last_indexed_at=timezone.now(),
        )

        logger.info(
            f"[{fid}] ✅ Web index v{version.version_number} complete. "
            f"{added} pages embedded, {skipped_unchanged} unchanged, "
            f"{skipped_empty} empty, {version.chunks_indexed} chunks."
        )
        return {
            "status":  "indexed",
            "version": version.version_number,
            "pages":   added,
            "bundle":  bundle_id,
        }

    except Exception as exc:
        store.mark_failed(str(exc))
        # Register failed version with bundle too so orchestrator can track it
        if bundle is not None and store._pending_version:
            bundle.versions.add(store._pending_version)
        logger.error(f"[{fid}] Web indexing failed: {exc}", exc_info=True)
        raise self.retry(exc=exc)

    finally:
        sync.release_index_lock()


# ---------------------------------------------------------------------------
# Task 5: Nightly orchestrator — fires at midnight, creates bundle, fans out
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=1, default_retry_delay=60)
def nightly_scrape_orchestrator_task(self):
    """
    Entry point for the nightly Celery beat schedule.

    1. Creates (or retrieves) today's NightlyBundle.
    2. Dispatches one scrape_web_source_task per URL in SCRAPING_URLS,
       passing bundle_id so each task registers its version with the bundle.
    3. Schedules nightly_activate_bundle_task to run after an estimated
       crawl window (default: settings.NIGHTLY_CRAWL_WINDOW_HOURS, fallback 8h).

    Celery beat schedule (settings.py):
        CELERY_BEAT_SCHEDULE = {
            "nightly-web-scrape": {
                "task":     "ragbot.tasks.nightly_scrape_orchestrator_task",
                "schedule": crontab(hour=0, minute=0),
            },
        }
    """
    from ragbot.models import NightlyBundle
    from celery.schedules import crontab

    today  = timezone.now().date()
    bundle, created = NightlyBundle.objects.get_or_create(date=today)

    if not created and bundle.is_active:
        logger.info(f"Bundle for {today} already active — skipping orchestration.")
        return {"status": "already_active", "date": str(today)}

    urls = getattr(settings, "SCRAPING_URLS", [])
    if not urls:
        logger.warning("SCRAPING_URLS is empty — nothing to dispatch.")
        return {"status": "no_urls"}

    logger.info(f"🌙 Nightly bundle {today} created. Dispatching {len(urls)} domain tasks.")

    for url in urls:
        scrape_web_source_task.delay(url, bundle_id=bundle.pk)
        logger.info(f"  → Dispatched: {url}")

    # Schedule activation after the crawl window
    # Tune NIGHTLY_CRAWL_WINDOW_HOURS in settings based on how long your full
    # crawl takes. Default 8 hours is conservative for 9 domains.
    crawl_window_seconds = getattr(settings, "NIGHTLY_CRAWL_WINDOW_HOURS", 8) * 3600
    nightly_activate_bundle_task.apply_async(
        kwargs={"bundle_id": bundle.pk},
        countdown=crawl_window_seconds,
    )
    logger.info(
        f"  ⏰ Activation scheduled in {crawl_window_seconds // 3600}h "
        f"(bundle_id={bundle.pk})."
    )

    return {
        "status":    "dispatched",
        "date":      str(today),
        "bundle_id": bundle.pk,
        "domains":   len(urls),
    }


# ---------------------------------------------------------------------------
# Task 6: Nightly activation — runs after crawl window, activates the bundle
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def nightly_activate_bundle_task(self, bundle_id: int):
    """
    Activates tonight's NightlyBundle.

    Called automatically by nightly_scrape_orchestrator_task after the
    estimated crawl window. Can also be triggered manually:
        from ragbot.tasks import nightly_activate_bundle_task
        nightly_activate_bundle_task.delay(bundle_id=<id>)

    Behaviour:
      - If some domain tasks are still running, reschedules itself for
        30 minutes later (up to max_retries times).
      - Activates all completed versions (per-domain, safe — see NightlyBundle.activate()).
      - Logs a warning for any failed domains (their previous version stays active).
      - Deactivates the previous bundle's versions for domains that succeeded tonight.
    """
    from ragbot.models import NightlyBundle
    from ragbot.vectorstore_db import invalidate_db_store_cache

    try:
        bundle = NightlyBundle.objects.get(pk=bundle_id)
    except NightlyBundle.DoesNotExist:
        logger.error(f"NightlyBundle id={bundle_id} not found.")
        return {"status": "not_found"}

    # If tasks are still running, reschedule
    if bundle.pending_or_running_versions().exists():
        still_running = bundle.pending_or_running_versions().count()
        logger.info(
            f"Bundle {bundle.date}: {still_running} domain(s) still running. "
            f"Rescheduling activation in 30 min."
        )
        raise self.retry(countdown=1800)

    # Nothing completed at all — something went very wrong
    if bundle.completed_versions().count() == 0:
        logger.error(
            f"Bundle {bundle.date}: no completed versions — all domains failed. "
            f"Previous night's data remains active."
        )
        return {"status": "all_failed", "bundle_id": bundle_id}

    # Activate
    bundle.activate()

    # Invalidate vector store caches for activated domains
    for version in bundle.completed_versions():
        invalidate_db_store_cache(version.folder_id)

    summary = bundle.summary()
    logger.info(
        f"✅ Bundle {bundle.date} activated. "
        f"{summary['completed']} domains live, {summary['failed']} failed "
        f"(serving previous night's data for failed domains)."
    )

    # Warn about failed domains
    for version in bundle.failed_versions():
        logger.warning(
            f"  ⚠️  Domain {version.folder_id} failed tonight — "
            f"previous active version continues serving."
        )

    return {
        "status":    "activated",
        "bundle_id": bundle_id,
        "date":      str(bundle.date),
        **summary,
    }