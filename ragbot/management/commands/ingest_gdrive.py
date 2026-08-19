from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from ragbot.gdrive_ingest import iter_drive_files, fetch_text_for_file
from ragbot.textsplit import chunk_text
from ragbot.vectorstore_db import DBVectorStore, invalidate_db_store_cache
from ragbot.models import IndexVersion

import gc


class Command(BaseCommand):
    help = "Ingest a Google Drive folder into the PostgreSQL vector store (versioned)"

    def add_arguments(self, parser):
        parser.add_argument("--folder-id", required=False, default=None)
        parser.add_argument("--max-chars",  type=int, default=1500)
        parser.add_argument("--overlap",    type=int, default=150)
        parser.add_argument(
            "--no-activate",
            action="store_true",
            help="Index the documents but do NOT activate the new version",
        )
        parser.add_argument("--debug", action="store_true")

        # Version management helpers
        parser.add_argument(
            "--list-versions",
            action="store_true",
            help="List all versions for the folder and exit",
        )
        parser.add_argument(
            "--rollback-to",
            type=int,
            default=None,
            metavar="VERSION_NUMBER",
            help="Activate a previous version instead of ingesting",
        )

    def handle(self, *args, **opts):
        folder_id = opts["folder_id"] or getattr(settings, "GDRIVE_DEFAULT_FOLDER_ID", None)
        if not folder_id:
            raise CommandError(
                "Provide --folder-id or set GDRIVE_DEFAULT_FOLDER_ID in settings."
            )

        store = DBVectorStore(
            folder_id=folder_id,
            embed_model=settings.OPENAI_EMBED_MODEL,
            chunk_max_chars=opts["max_chars"],
            chunk_overlap=opts["overlap"],
        )

        # ── list versions ────────────────────────────────────────────────────
        if opts["list_versions"]:
            self._list_versions(store)
            return

        # ── rollback ─────────────────────────────────────────────────────────
        if opts["rollback_to"] is not None:
            target = opts["rollback_to"]
            try:
                version = store.rollback_to_version(target)
                invalidate_db_store_cache(folder_id)
                self.stdout.write(
                    self.style.SUCCESS(f"Rolled back to version v{version.version_number}")
                )
            except IndexVersion.DoesNotExist:
                raise CommandError(
                    f"Version {target} not found or not completed for folder {folder_id}"
                )
            return

        # ── fresh ingestion ───────────────────────────────────────────────────
        self.stdout.write(f"Starting ingestion from folder: {folder_id}")

        version = store.begin_version()
        self.stdout.write(
            self.style.HTTP_INFO(
                f"Created IndexVersion v{version.version_number} (pk={version.pk})"
            )
        )

        added_files   = 0
        skipped_files = 0

        try:
            for f in iter_drive_files(folder_id):
                if opts["debug"]:
                    self.stdout.write(f"  → {f['name']} ({f['mimeType']})")

                text, meta = fetch_text_for_file(f)

                if not text.strip():
                    self.stdout.write(
                        self.style.WARNING(f"Skipping {meta['name']} (no text)")
                    )
                    skipped_files += 1
                    continue

                chunks = chunk_text(
                    text,
                    max_chars=opts["max_chars"],
                    overlap=opts["overlap"],
                )

                # NOTE: `text` key in metadatas is no longer used by add_texts()
                # (the original chunk text is taken from the `chunks` list arg),
                # but it is kept here for backward compatibility with any other
                # consumers of this metadata dict.
                metadatas = [
                    {
                        "source_id":   meta["id"],
                        "source_name": meta["name"],
                        "mime":        meta["mime"],
                        "source_url":  f"https://drive.google.com/file/d/{meta['id']}/view",
                        "source_type": "drive",
                        "chunk":       i,
                        "text":        c,
                    }
                    for i, c in enumerate(chunks)
                ]

                store.add_texts(chunks, metadatas)
                added_files += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Prepared {meta['name']}: {len(chunks)} chunks"
                    )
                )
                gc.collect()

        except Exception as exc:
            store.mark_failed(str(exc))
            raise CommandError(f"Ingestion failed: {exc}") from exc

        if not store._pending_chunks:
            store.mark_failed("No chunks produced")
            self.stdout.write(self.style.WARNING("No text found – nothing indexed."))
            return

        activate = not opts["no_activate"]
        store.save(activate=activate)
        invalidate_db_store_cache(folder_id)

        self.stdout.write(
            self.style.SUCCESS(
                f"\n✅ Done. Version v{version.version_number} | "
                f"{added_files} files | "
                f"{version.chunks_indexed} chunks | "
                f"{'ACTIVE' if activate else 'inactive'}"
            )
        )
        self.stdout.write(f"   Skipped: {skipped_files} files")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _list_versions(self, store: DBVectorStore):
        versions = store.list_versions()
        if not versions:
            self.stdout.write("No versions found.")
            return

        self.stdout.write(
            f"\n{'v#':<6} {'pk':<8} {'status':<12} {'active':<8} "
            f"{'files':<8} {'chunks':<10} {'created'}"
        )
        self.stdout.write("-" * 72)

        for v in versions:
            active_marker = "✓" if v["is_active"] else ""
            created = v["created_at"].strftime("%Y-%m-%d %H:%M") if v["created_at"] else ""
            row = (
                f"v{v['version_number']:<5} {v['pk']:<8} {v['status']:<12} "
                f"{active_marker:<8} {v['files_processed']:<8} "
                f"{v['chunks_indexed']:<10} {created}"
            )
            if v["is_active"]:
                self.stdout.write(self.style.SUCCESS(row))
            else:
                self.stdout.write(row)