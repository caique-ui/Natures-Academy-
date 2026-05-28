"""
ragbot/management/commands/sync_drive.py

One command that wraps all sync operations:

  python manage.py sync_drive                      # check + index if needed
  python manage.py sync_drive --check-only         # only check for changes
  python manage.py sync_drive --force              # index regardless of fingerprint
  python manage.py sync_drive --status             # show current sync state
  python manage.py sync_drive --register-watch     # register Drive push webhook
  python manage.py sync_drive --renew-watch        # renew expiring watch
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Manage automated Google Drive sync and indexing"

    def add_arguments(self, parser):
        parser.add_argument("--folder-id", default=None)
        parser.add_argument("--check-only",      action="store_true",
                            help="Detect changes but do not index")
        parser.add_argument("--force",           action="store_true",
                            help="Run ingestion even if fingerprint unchanged")
        parser.add_argument("--status",          action="store_true",
                            help="Print current sync state and exit")
        parser.add_argument("--register-watch",  action="store_true",
                            help="Register a Drive push notification channel")
        parser.add_argument("--renew-watch",     action="store_true",
                            help="Renew the push notification channel")
        parser.add_argument("--debounce",        type=int, default=None,
                            help="Override debounce seconds for this run")

    def handle(self, *args, **opts):
        fid = opts["folder_id"] or getattr(settings, "GDRIVE_DEFAULT_FOLDER_ID", None)
        if not fid:
            raise CommandError("Provide --folder-id or set GDRIVE_DEFAULT_FOLDER_ID")

        if opts["status"]:
            self._print_status(fid)
            return

        if opts["register_watch"]:
            self._register_watch(fid)
            return

        if opts["renew_watch"]:
            self._renew_watch(fid)
            return

        if opts["check_only"]:
            self._check(fid)
            return

        # Full sync: check then index if needed
        changed = self._check(fid)
        if changed or opts["force"]:
            self._index(fid, force=opts["force"],
                        debounce_override=opts.get("debounce"))
        else:
            self.stdout.write("No changes — skipping indexing.")

    # ── check ─────────────────────────────────────────────────────────────

    def _check(self, fid: str) -> bool:
        """Run change detection. Returns True if changes were found."""
        from ragbot.models import DriveSync, DriveSyncEvent
        from ragbot.drive_sync import fetch_drive_changes, get_initial_page_token

        drive = self._drive_service()

        sync, created = self._get_or_create_sync(fid, drive)

        if not sync.page_token:
            token = get_initial_page_token(drive)
            sync.page_token = token
            sync.last_checked_at = timezone.now()
            sync.save(update_fields=["page_token", "last_checked_at"])
            self.stdout.write("Initialised Drive page token. Run again to detect changes.")
            return False

        self.stdout.write(f"Checking for changes in folder {fid}…")
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
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(changed_ids)} changed file(s) detected "
                    f"({len(pending)} total pending)"
                )
            )
            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.CHECK,
                detail={"changed_ids": changed_ids},
            )
            return True
        else:
            sync.save(update_fields=["page_token", "last_checked_at"])
            self.stdout.write(self.style.SUCCESS("  No changes detected."))
            return False

    # ── index ─────────────────────────────────────────────────────────────

    def _index(self, fid: str, force: bool = False, debounce_override: int = None):
        from ragbot.models import DriveSync, DriveSyncEvent
        from ragbot.drive_sync import compute_folder_fingerprint, should_reindex
        from ragbot.vectorstore_db import DBVectorStore, invalidate_db_store_cache
        from ragbot.gdrive_ingest import iter_drive_files, fetch_text_for_file
        from ragbot.textsplit import chunk_text
        import gc

        sync = DriveSync.objects.get(folder_id=fid)

        if debounce_override is not None:
            sync.debounce_seconds = debounce_override

        drive = self._drive_service()
        self.stdout.write("Computing folder fingerprint…")
        current_fp, _ = compute_folder_fingerprint(drive, fid)
        self.stdout.write(f"  Fingerprint: {current_fp[:16]}…")

        if not force:
            do_index, reason = should_reindex(sync, current_fp)
            if not do_index:
                self.stdout.write(self.style.WARNING(f"Skipping index: {reason}"))
                DriveSyncEvent.objects.create(
                    folder_id=fid,
                    event_type=DriveSyncEvent.EventType.INDEX_SKIP,
                    detail={"reason": reason},
                )
                return
        else:
            self.stdout.write(self.style.HTTP_INFO("--force flag set, skipping checks."))

        if not sync.acquire_index_lock():
            self.stdout.write(self.style.ERROR("Another worker is indexing. Aborting."))
            return

        DriveSyncEvent.objects.create(
            folder_id=fid,
            event_type=DriveSyncEvent.EventType.INDEX_START,
            detail={"force": force, "fingerprint": current_fp},
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
                    self.stdout.write(
                        self.style.WARNING(f"  Skipping {meta['name']} (no text)")
                    )
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
                self.stdout.write(
                    self.style.SUCCESS(f"  {meta['name']}: {len(chunks)} chunks")
                )
                gc.collect()

            if not store._pending_chunks:
                store.mark_failed("No chunks produced")
                raise CommandError("No text extracted from any file.")

            store.save(activate=True)
            invalidate_db_store_cache(fid)

            DriveSync.objects.filter(pk=sync.pk).update(
                folder_fingerprint=current_fp,
                pending_file_ids=[],
                last_indexed_at=timezone.now(),
            )

            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.INDEX_DONE,
                detail={
                    "version": version.version_number,
                    "files": added,
                    "skipped": skipped,
                    "chunks": version.chunks_indexed,
                },
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"\n✅ v{version.version_number} active | "
                    f"{added} files | {version.chunks_indexed} chunks | "
                    f"{skipped} skipped"
                )
            )

        except Exception as exc:
            store.mark_failed(str(exc))
            DriveSyncEvent.objects.create(
                folder_id=fid,
                event_type=DriveSyncEvent.EventType.INDEX_FAIL,
                detail={"error": str(exc)},
            )
            raise
        finally:
            sync.release_index_lock()

    # ── status ────────────────────────────────────────────────────────────

    def _print_status(self, fid: str):
        from ragbot.models import DriveSync, DriveSyncEvent, IndexVersion

        try:
            sync = DriveSync.objects.get(folder_id=fid)
        except DriveSync.DoesNotExist:
            self.stdout.write("No DriveSync record found. Run without --status to initialise.")
            return

        self.stdout.write(f"\n{'─'*50}")
        self.stdout.write(f"Folder ID      : {sync.folder_id}")
        self.stdout.write(f"Page token     : {'set' if sync.page_token else 'NOT SET'}")
        self.stdout.write(f"Fingerprint    : {sync.folder_fingerprint[:16] + '…' if sync.folder_fingerprint else 'none'}")
        self.stdout.write(f"Pending changes: {len(sync.pending_file_ids)} files")
        self.stdout.write(f"Last checked   : {sync.last_checked_at or 'never'}")
        self.stdout.write(f"Last change    : {sync.last_change_at or 'never'}")
        self.stdout.write(f"Last indexed   : {sync.last_indexed_at or 'never'}")
        self.stdout.write(f"Debounce       : {sync.debounce_seconds}s")
        self.stdout.write(f"Lock held      : {'YES ⚠' if sync.is_indexing else 'no'}")

        active = IndexVersion.objects.filter(is_active=True).order_by("-created_at").first()
        if active:
            self.stdout.write(
                f"Active version : v{active.version_number} "
                f"({active.chunks_indexed} chunks, {active.files_processed} files)"
            )

        self.stdout.write(f"\nRecent events:")
        for ev in DriveSyncEvent.objects.filter(folder_id=fid)[:8]:
            self.stdout.write(f"  {ev.created_at:%H:%M:%S}  {ev.event_type:<15}  {ev.detail}")
        self.stdout.write(f"{'─'*50}\n")

    # ── Drive watch registration ──────────────────────────────────────────

    def _register_watch(self, fid: str):
        import uuid
        from ragbot.models import DriveSync

        webhook_url = getattr(settings, "DRIVE_WEBHOOK_URL", None)
        if not webhook_url:
            raise CommandError("Set DRIVE_WEBHOOK_URL in settings.py first.")

        drive    = self._drive_service()
        token    = getattr(settings, "DRIVE_WEBHOOK_TOKEN", str(uuid.uuid4()))
        channel  = str(uuid.uuid4())
        expiry   = int((timezone.now() + timedelta(days=7)).timestamp() * 1000)

        body = {
            "id":         channel,
            "type":       "web_hook",
            "address":    webhook_url,
            "token":      token,
            "expiration": expiry,
        }

        resp = drive.files().watch(fileId=fid, body=body).execute()
        self.stdout.write(self.style.SUCCESS(
            f"Watch registered:\n"
            f"  Channel ID : {resp['id']}\n"
            f"  Resource   : {resp['resourceId']}\n"
            f"  Expires    : {resp.get('expiration', 'unknown')}\n"
            f"\nAdd to settings.py:\n"
            f"  DRIVE_WEBHOOK_TOKEN = '{token}'\n"
            f"  DRIVE_CHANNEL_ID    = '{channel}'\n"
            f"  DRIVE_RESOURCE_ID   = '{resp['resourceId']}'"
        ))

        DriveSync.objects.update_or_create(
            folder_id=fid,
            defaults={"folder_name": fid},
        )

    def _renew_watch(self, fid: str):
        """Stop old channel and register a new one."""
        self._stop_watch()
        self._register_watch(fid)

    def _stop_watch(self):
        channel_id   = getattr(settings, "DRIVE_CHANNEL_ID", None)
        resource_id  = getattr(settings, "DRIVE_RESOURCE_ID", None)
        if not channel_id or not resource_id:
            self.stdout.write("No channel to stop (DRIVE_CHANNEL_ID/DRIVE_RESOURCE_ID not set).")
            return
        drive = self._drive_service()
        drive.channels().stop(
            body={"id": channel_id, "resourceId": resource_id}
        ).execute()
        self.stdout.write(f"Stopped channel {channel_id}")

    # ── helpers ───────────────────────────────────────────────────────────

    def _drive_service(self, max_retries: int = 3):
        """
        Identical auth path to gdrive_ingest._creds_with_retry():
        ADC works locally and on GCP without any extra settings.
        """
        import socket
        import time
        from google.auth import default, compute_engine
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        scopes = [
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ]
        for attempt in range(max_retries):
            try:
                socket.setdefaulttimeout(30)
                if settings.APP_ENV == "local":
                    print("Using local ADC credentials (gcloud auth application-default login)")
                    credentials, _ = default(scopes=scopes)
                else:
                    print("Using Compute Engine credentials (service account attached to VM/container)")
                    credentials = compute_engine.Credentials()
                credentials.refresh(Request())
                return build("drive", "v3", credentials=credentials, cache_discovery=False)
            except socket.timeout:
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 5
                    self.stdout.write(f"Connection timeout, retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    raise
            except Exception as exc:
                if attempt < max_retries - 1:
                    time.sleep(2)
                else:
                    raise

    def _get_or_create_sync(self, fid, drive):
        from ragbot.models import DriveSync
        return DriveSync.objects.get_or_create(
            folder_id=fid,
            defaults={"folder_name": fid},
        )