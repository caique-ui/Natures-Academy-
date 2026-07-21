import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class IndexVersion(models.Model):
    """
    Represents a single ingestion run of a Google Drive folder.
    A new version is created each time `ingest_gdrive` runs successfully.
    Older versions are retained so you can roll back the active version.
    """

    class Status(models.TextChoices):
        PENDING   = "pending",   "Pending"
        RUNNING   = "running",   "Running"
        COMPLETED = "completed", "Completed"
        FAILED    = "failed",    "Failed"

    # Human-friendly auto-incrementing version per folder
    version_number = models.PositiveIntegerField(
        help_text="Monotonically increasing per folder_id"
    )
    folder_id = models.CharField(max_length=255, db_index=True)
    folder_name = models.CharField(max_length=512, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    is_active = models.BooleanField(
        default=False,
        help_text="Only one version per folder should be active at a time"
    )

    embed_model = models.CharField(max_length=100)
    chunk_max_chars = models.PositiveIntegerField(default=1500)
    chunk_overlap   = models.PositiveIntegerField(default=150)

    files_processed = models.PositiveIntegerField(default=0)
    files_skipped   = models.PositiveIntegerField(default=0)
    chunks_indexed  = models.PositiveIntegerField(default=0)

    error_message = models.TextField(blank=True)

    created_at   = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("folder_id", "version_number")]
        indexes = [
            models.Index(fields=["folder_id", "is_active"]),
        ]

    def __str__(self):
        return f"IndexVersion(folder={self.folder_id}, v{self.version_number}, {self.status})"

    @classmethod
    def next_version_number(cls, folder_id: str) -> int:
        """Return the next version number for a given folder."""
        last = (
            cls.objects
            .filter(folder_id=folder_id)
            .aggregate(models.Max("version_number"))
        )["version_number__max"]
        return (last or 0) + 1

    def activate(self):
        """
        Mark this version as active, deactivating any other active version
        for the same folder. Runs in a single transaction.
        """
        from django.db import transaction
        with transaction.atomic():
            IndexVersion.objects.filter(
                folder_id=self.folder_id, is_active=True
            ).exclude(pk=self.pk).update(is_active=False)
            self.is_active = True
            self.save(update_fields=["is_active"])


class SourceDocument(models.Model):
    """
    A single Drive file or scraped web page ingested as part of an IndexVersion.
    One SourceDocument → many DocumentChunks.
 
    Web sources form a tree: root pages have parent=None; every page discovered
    by following a link records the page it was found on as its parent.
    The tree is scoped to a single IndexVersion (rebuilt fresh each crawl run).
    """
    version = models.ForeignKey(
        IndexVersion, on_delete=models.CASCADE, related_name="documents"
    )
    drive_file_id   = models.CharField(max_length=2048)
    drive_file_name = models.CharField(max_length=512)
    mime_type       = models.CharField(max_length=255)
    char_count      = models.PositiveIntegerField(default=0)
    chunk_count     = models.PositiveIntegerField(default=0)
 
    SOURCE_DRIVE = "drive"
    SOURCE_WEB   = "web"
    SOURCE_CHOICES = [(SOURCE_DRIVE, "Google Drive"), (SOURCE_WEB, "Web")]
 
    source_type = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default="drive"
    )
    source_url = models.URLField(
        max_length=2048, blank=True,
        help_text="Drive share URL or scraped page URL"
    )
 
    # ── Parent / child relationship (web sources only) ────────────────────────
    # NULL  → this is a root page (the root_url passed to the scraper task)
    # SET   → this page was discovered as a link on the parent page
    # on_delete=SET_NULL so deleting a parent doesn't cascade-delete children;
    # they just become orphaned roots, which is the safest fallback.
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        help_text="The page whose crawl discovered this URL (web sources only)",
    )
 
    class Meta:
        indexes = [
            models.Index(fields=["version", "drive_file_id"]),
            models.Index(fields=["version", "parent"]),   # fast child lookups
        ]
 
    def __str__(self):
        return f"{self.drive_file_name} (v{self.version.version_number})"
 
    # ── Tree helpers ──────────────────────────────────────────────────────────
 
    def get_children(self) -> models.QuerySet:
        """Return all direct children of this page within the same version."""
        return self.children.filter(version=self.version)
 
    def get_ancestors(self) -> list["SourceDocument"]:
        """
        Walk up the parent chain and return ancestors root-first.
        Stops at the root (parent=None) or after 50 hops to guard against
        accidental cycles from redirect loops.
        """
        ancestors = []
        node = self
        seen = {self.pk}
        for _ in range(50):
            if node.parent_id is None:
                break
            node = node.parent
            if node.pk in seen:
                break          # cycle guard
            seen.add(node.pk)
            ancestors.append(node)
        ancestors.reverse()    # root → … → direct parent
        return ancestors
 
    def get_descendants(self) -> list["SourceDocument"]:
        """
        BFS over children within the same version.
        Returns a flat list in breadth-first order (children before grandchildren).
        Stops at 500 nodes to keep memory bounded.
        """
        from collections import deque
        result = []
        queue  = deque(self.get_children())
        seen   = {self.pk}
        while queue and len(result) < 500:
            node = queue.popleft()
            if node.pk in seen:
                continue
            seen.add(node.pk)
            result.append(node)
            queue.extend(node.get_children())
        return result
 
    def get_tree_path(self) -> str:
        """
        Human-readable URL breadcrumb, e.g.:
          https://example.gov.au → /regulations → /regulations/2024
        """
        ancestors = self.get_ancestors()
        parts = [a.source_url or a.drive_file_name for a in ancestors]
        parts.append(self.source_url or self.drive_file_name)
        return " → ".join(parts)

class DocumentChunk(models.Model):
    """
    A single text chunk with its embedding vector.

    The embedding is stored as a binary blob (raw float32 bytes) so the app
    works without the pgvector extension. If you install pgvector you can
    swap this field for pgvector.VectorField and gain ANN index support –
    see vectorstore_db.py for how to do that transparently.
    """
    version  = models.ForeignKey(
        IndexVersion, on_delete=models.CASCADE, related_name="chunks"
    )
    document = models.ForeignKey(
        SourceDocument, on_delete=models.CASCADE, related_name="chunks"
    )

    chunk_index = models.PositiveIntegerField(
        help_text="Position of this chunk within the source document"
    )
    text = models.TextField()

    # Raw IEEE-754 float32 bytes; reshape to (dim,) in Python with np.frombuffer
    embedding = models.BinaryField(
        help_text="numpy float32 array serialised as raw bytes"
    )

    class Meta:
        ordering = ["document", "chunk_index"]
        indexes = [
            models.Index(fields=["version"]),
            models.Index(fields=["document", "chunk_index"]),
        ]

    def __str__(self):
        return f"Chunk {self.chunk_index} of {self.document.drive_file_name}"


class DriveSync(models.Model):
    """
    Tracks Google Drive change state for a folder.
    One row per folder — updated after every check and every index run.

    Flow:
      check_drive_changes  → updates page_token, pending_file_ids, folder_fingerprint
      sync_drive_if_changed → reads pending_file_ids; if non-empty triggers ingest,
                              then clears pending_file_ids and sets last_indexed_at
    """

    folder_id   = models.CharField(max_length=255, unique=True, db_index=True)
    folder_name = models.CharField(max_length=512, blank=True)

    # Drive Changes API cursor — persisted so we only ever ask "what changed since last time"
    page_token = models.CharField(max_length=1024, blank=True)

    # SHA-256 of sorted (file_id, modifiedTime) pairs at last successful index
    # If this matches the current folder state → skip indexing entirely
    folder_fingerprint = models.CharField(max_length=64, blank=True)

    # File IDs that changed since last index run (populated by check, cleared after index)
    pending_file_ids = models.JSONField(default=list)

    # Debounce: don't index until Drive has been quiet for this many seconds
    debounce_seconds = models.PositiveIntegerField(default=600)  # 10 min

    # Timestamp of the most recent change event received
    last_change_at = models.DateTimeField(null=True, blank=True)

    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_indexed_at = models.DateTimeField(null=True, blank=True)

    # Lock: prevents two workers indexing simultaneously
    is_indexing = models.BooleanField(default=False)
    indexing_started_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Drive Sync State"

    def __str__(self):
        return f"DriveSync({self.folder_id}, token={'set' if self.page_token else 'unset'})"

    def is_debounce_over(self) -> bool:
        """Return True if Drive has been quiet long enough to start indexing."""
        if not self.last_change_at:
            return True
        quiet_for = (timezone.now() - self.last_change_at).total_seconds()
        return quiet_for >= self.debounce_seconds

    def acquire_index_lock(self) -> bool:
        """
        Atomic lock acquisition using SELECT FOR UPDATE.
        Returns True if this caller acquired the lock, False if already locked.
        Automatically releases stale locks older than 2 hours.
        """
        from django.db import transaction
        with transaction.atomic():
            # Re-fetch with row lock
            obj = DriveSync.objects.select_for_update().get(pk=self.pk)

            # Release stale lock (crashed worker)
            if obj.is_indexing and obj.indexing_started_at:
                stale = (timezone.now() - obj.indexing_started_at).total_seconds() > 7200
                if stale:
                    obj.is_indexing = False

            if obj.is_indexing:
                return False  # Someone else holds the lock

            obj.is_indexing = True
            obj.indexing_started_at = timezone.now()
            obj.save(update_fields=["is_indexing", "indexing_started_at"])
            self.is_indexing = True
            return True

    def release_index_lock(self):
        DriveSync.objects.filter(pk=self.pk).update(
            is_indexing=False,
            indexing_started_at=None,
        )
        self.is_indexing = False


class DriveSyncEvent(models.Model):
    """
    Audit log of every Drive change event received and every index triggered.
    Useful for debugging and monitoring.
    """

    class EventType(models.TextChoices):
        WEBHOOK     = "webhook",     "Webhook received"
        CHECK       = "check",       "Scheduled check"
        INDEX_START = "index_start", "Index started"
        INDEX_DONE  = "index_done",  "Index completed"
        INDEX_SKIP  = "index_skip",  "Index skipped (no changes)"
        INDEX_FAIL  = "index_fail",  "Index failed"
        DEBOUNCE    = "debounce",    "Skipped (debounce active)"
        LOCK_BUSY   = "lock_busy",   "Skipped (lock held)"

    folder_id  = models.CharField(max_length=255, db_index=True)
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    detail     = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes  = [models.Index(fields=["folder_id", "event_type"])]

    def __str__(self):
        return f"{self.event_type} @ {self.created_at:%Y-%m-%d %H:%M}"

class WebSyncState(models.Model):
    """
    Tracks scraping state for a web source (analogous to DriveSync for Drive).
    One row per root URL.
    """
    root_url = models.URLField(max_length=2048, unique=True, db_index=True)
    label    = models.CharField(
        max_length=255, blank=True,
        help_text="Human-friendly name, e.g. 'NSW Regs 2011-0653'"
    )
    folder_id = models.CharField(
        max_length=255,
        help_text="Logical folder_id used in IndexVersion — e.g. 'web:nsw-regs-0653'"
    )
 
    # SHA-256 of all (url, content_hash) pairs at last successful index
    content_fingerprint = models.CharField(max_length=64, blank=True)
 
    last_checked_at     = models.DateTimeField(null=True, blank=True)
    last_indexed_at     = models.DateTimeField(null=True, blank=True)
    is_indexing         = models.BooleanField(default=False)
    indexing_started_at = models.DateTimeField(null=True, blank=True)
 
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        verbose_name = "Web Sync State"
 
    def __str__(self):
        return f"WebSyncState({self.root_url})"
 
    def acquire_index_lock(self) -> bool:
        from django.db import transaction
        with transaction.atomic():
            obj = WebSyncState.objects.select_for_update().get(pk=self.pk)
            if obj.is_indexing and obj.indexing_started_at:
                stale = (timezone.now() - obj.indexing_started_at).total_seconds() > 7200
                if stale:
                    obj.is_indexing = False
            if obj.is_indexing:
                return False
            obj.is_indexing = True
            obj.indexing_started_at = timezone.now()
            obj.save(update_fields=["is_indexing", "indexing_started_at"])
            self.is_indexing = True
            return True
 
    def release_index_lock(self):
        WebSyncState.objects.filter(pk=self.pk).update(
            is_indexing=False,
            indexing_started_at=None,
        )
        self.is_indexing = False    

class Folder(models.Model):
    """
    A named folder owned by an authenticated user.
    Conversations can optionally be placed in a folder.
    Anonymous users cannot have folders.
    """
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user       = models.ForeignKey(
                   User, on_delete=models.CASCADE, related_name="folders"
                 )
    name       = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        ordering = ["name"]
        unique_together = [("user", "name")]
 
    def __str__(self):
        return f"{self.user.username} / {self.name}"
 
 
class Conversation(models.Model):
    """
    A single conversation thread.
 
    - Authenticated users: linked via `user` FK, optionally placed in a Folder.
    - Anonymous users:     linked via `session_key` only.
 
    When an anonymous user logs in, signals.py migrates their session
    conversations to their User account.
    """
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user        = models.ForeignKey(
                    User, null=True, blank=True,
                    on_delete=models.CASCADE, related_name="conversations"
                  )
    folder      = models.ForeignKey(
                    Folder, null=True, blank=True,
                    on_delete=models.SET_NULL, related_name="conversations"
                  )
    session_key = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    title       = models.CharField(max_length=200, default="New conversation")
    created_at  = models.DateTimeField(default=timezone.now)
    updated_at  = models.DateTimeField(auto_now=True)
 
    class Meta:
        ordering = ["-updated_at"]
        indexes  = [
            models.Index(fields=["user", "-updated_at"]),
            models.Index(fields=["folder", "-updated_at"]),
            models.Index(fields=["session_key", "-updated_at"]),
        ]
 
    def __str__(self):
        owner = self.user.username if self.user else f"anon:{self.session_key[:8]}"
        folder = f" [{self.folder.name}]" if self.folder_id else ""
        return f"[{owner}]{folder} {self.title[:50]}"
 
    def get_history(self, max_messages: int = 10) -> list[dict]:
        msgs = list(self.messages.order_by("-created_at")[:max_messages])
        msgs.reverse()
        return [{"role": m.role, "content": m.content} for m in msgs]
 
 
class Message(models.Model):
    ROLE_USER      = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES   = [(ROLE_USER, "User"), (ROLE_ASSISTANT, "Assistant")]
 
    conversation  = models.ForeignKey(
                      Conversation, on_delete=models.CASCADE, related_name="messages"
                    )
    role          = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content       = models.TextField()
    source_chunks = models.JSONField(default=list, blank=True)
    created_at    = models.DateTimeField(default=timezone.now)
 
    class Meta:
        ordering = ["created_at"]
 
    def __str__(self):
        return f"[{self.role}] {self.content[:60]}"
    
class ScrapedURL(models.Model):
    """
    Permanent record of every URL successfully scraped.
    Used to skip re-crawling URLs already indexed today,
    even if they're discovered via a different root or parent.
    """
    url             = models.URLField(max_length=2048, unique=True, db_index=True)
    content_hash    = models.CharField(max_length=64, blank=True)
    last_scraped_at = models.DateTimeField(default=timezone.now)
    source_version  = models.ForeignKey(
        IndexVersion,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        help_text="The version this URL was last indexed into"
    )

    class Meta:
        verbose_name = "Scraped URL Cache"

    def __str__(self):
        return f"{self.url} @ {self.last_scraped_at:%Y-%m-%d}"

    @classmethod
    def was_scraped_today(cls, url: str) -> bool:
        from django.utils import timezone
        today = timezone.now().date()
        return cls.objects.filter(
            url=url,
            last_scraped_at__date=today,
        ).exists()

    @classmethod
    def mark_scraped(cls, url: str, content_hash: str = "", version=None):
        cls.objects.update_or_create(
            url=url,
            defaults={
                "content_hash":    content_hash,
                "last_scraped_at": timezone.now(),
                "source_version":  version,
            }
        )


class NightlyBundle(models.Model):
    """
    Groups all per-domain IndexVersions created during a single nightly crawl run.

    Lifecycle:
        1. nightly_scrape_orchestrator_task creates a NightlyBundle(date=today)
        2. Each scrape_web_source_task completes → registers its IndexVersion
           with the bundle via bundle.versions.add(version)
        3. nightly_activate_bundle_task fires after all domains finish:
           - calls bundle.activate() which activates completed versions
           - deactivates the previous night's bundle's versions (per domain)
           - failed domain versions are skipped → previous night's version
             for that domain stays active (answers still served from it)

    Rollback:
        Set yesterday's NightlyBundle.is_active=True and call its activate()
        to restore the previous night's data across all domains atomically.
    """
    date = models.DateField(
        unique=True,
        db_index=True,
        help_text="The calendar date this bundle was created for (midnight run date)",
    )
    is_active = models.BooleanField(
        default=False,
        help_text="True for the currently serving bundle",
    )
    versions = models.ManyToManyField(
        "IndexVersion",
        blank=True,
        related_name="bundles",
        help_text="All IndexVersions (any status) created during this nightly run",
    )

    created_at   = models.DateTimeField(default=timezone.now)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date"]
        verbose_name        = "Nightly Bundle"
        verbose_name_plural = "Nightly Bundles"

    def __str__(self):
        status = "active" if self.is_active else "inactive"
        return f"NightlyBundle({self.date}, {status})"

    # ── Queryset helpers ──────────────────────────────────────────────────

    def completed_versions(self):
        return self.versions.filter(status=IndexVersion.Status.COMPLETED)

    def failed_versions(self):
        return self.versions.filter(status=IndexVersion.Status.FAILED)

    def pending_or_running_versions(self):
        return self.versions.filter(
            status__in=[IndexVersion.Status.PENDING, IndexVersion.Status.RUNNING]
        )

    def is_fully_finished(self) -> bool:
        """
        True when every domain task has finished (pass or fail).
        Used by nightly_activate_bundle_task to know it's safe to activate.
        """
        from django.conf import settings
        expected = len(getattr(settings, "SCRAPING_URLS", []))
        if expected == 0:
            return False
        return self.pending_or_running_versions().count() == 0 and \
               self.versions.count() >= expected

    # ── Activation ───────────────────────────────────────────────────────

    def activate(self):
        """
        Activate completed versions in this bundle.

        Per-domain logic (key correctness guarantee):
          - Only deactivates a previous version for domain X if tonight's
            version for domain X completed successfully.
          - If tonight's version for domain X failed, the previous active
            version for X is left untouched → answers keep being served.

        This means after activation:
          - Successful domains  → tonight's fresh data
          - Failed domains      → last successful night's data (no gap)
        """
        from django.db import transaction

        with transaction.atomic():
            completed = list(self.completed_versions().select_related())

            for version in completed:
                # Deactivate the previous active version for this folder_id only
                IndexVersion.objects.filter(
                    folder_id=version.folder_id,
                    is_active=True,
                ).exclude(pk=version.pk).update(is_active=False)

                # Activate tonight's version
                version.is_active = True
                version.save(update_fields=["is_active"])

            self.is_active   = True
            self.activated_at = timezone.now()
            self.save(update_fields=["is_active", "activated_at"])

    # ── Summary ──────────────────────────────────────────────────────────

    def summary(self) -> dict:
        return {
            "date":      str(self.date),
            "completed": self.completed_versions().count(),
            "failed":    self.failed_versions().count(),
            "pending":   self.pending_or_running_versions().count(),
            "is_active": self.is_active,
        }