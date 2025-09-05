from django.core.management.base import BaseCommand
from django.conf import settings
from ragbot.vectorstore import get_store
from ragbot.gdrive_ingest import iter_drive_files, fetch_text_for_file
from ragbot.textsplit import chunk_text
import gc
import time

class Command(BaseCommand):
    help = "Ingest a Google Drive folder into the local vector store"

    def add_arguments(self, parser):
        parser.add_argument("--folder-id", required=True)
        parser.add_argument("--max-chars", type=int, default=1500)
        parser.add_argument("--overlap", type=int, default=150)
        parser.add_argument("--debug", action="store_true", help="Enable debug output")

    def handle(self, *args, **opts):
        folder_id = opts["folder_id"]
        max_chars = opts["max_chars"]
        overlap = opts["overlap"]
        debug = opts["debug"]

        store = get_store()
        texts = []
        metas = []
        added_files = 0
        skipped_files = 0

        self.stdout.write(f"Starting ingestion from folder ID: {folder_id}")
        
        for f in iter_drive_files(folder_id):
            if debug:
                self.stdout.write(f"Processing file: {f['name']} (MIME: {f['mimeType']})")
            
            text, meta = fetch_text_for_file(f)
            
            if not text.strip():
                self.stdout.write(self.style.WARNING(f"Skipping {meta['name']} (no text extracted)"))
                skipped_files += 1
                if debug:
                    self.stdout.write(f"  MIME type: {meta['mime']}")
                continue
                
            if debug:
                self.stdout.write(f"  Extracted {len(text)} characters")
                
            chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)

            texts.extend(chunks)
            metas.extend([
                {"source_id": meta["id"], "source_name": meta["name"], "mime": meta["mime"], "chunk": i, "text": c}
                for i, c in enumerate(chunks)
            ])
            added_files += 1
            self.stdout.write(self.style.SUCCESS(f"Prepared {meta['name']} with {len(chunks)} chunks"))

        if texts:
            self.stdout.write(f"Adding {len(texts)} chunks to vector store...")
            store.add_texts(texts, metas)
            store.save()
            self.stdout.write(self.style.SUCCESS(f"Successfully indexed {len(texts)} chunks from {added_files} files"))
        else:
            self.stdout.write(self.style.WARNING("No texts to index"))
            
        self.stdout.write(f"Summary: {added_files} processed, {skipped_files} skipped")