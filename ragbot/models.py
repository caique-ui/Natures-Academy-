from django.db import models
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
    A single Drive file that was ingested as part of an IndexVersion.
    One SourceDocument → many DocumentChunks.
    """
    version = models.ForeignKey(
        IndexVersion, on_delete=models.CASCADE, related_name="documents"
    )
    drive_file_id   = models.CharField(max_length=255)
    drive_file_name = models.CharField(max_length=512)
    mime_type       = models.CharField(max_length=255)
    char_count      = models.PositiveIntegerField(default=0)
    chunk_count     = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["version", "drive_file_id"]),
        ]

    def __str__(self):
        return f"{self.drive_file_name} (v{self.version.version_number})"


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