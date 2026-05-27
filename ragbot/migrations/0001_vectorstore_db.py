"""
ragbot/migrations/0001_vectorstore_db.py

Initial migration for the versioned vector store tables.
Run with:  python manage.py migrate ragbot
"""

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="IndexVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("version_number", models.PositiveIntegerField(
                    help_text="Monotonically increasing per folder_id"
                )),
                ("folder_id", models.CharField(db_index=True, max_length=255)),
                ("folder_name", models.CharField(blank=True, max_length=512)),
                ("status", models.CharField(
                    choices=[
                        ("pending",   "Pending"),
                        ("running",   "Running"),
                        ("completed", "Completed"),
                        ("failed",    "Failed"),
                    ],
                    default="pending",
                    max_length=20,
                )),
                ("is_active", models.BooleanField(
                    default=False,
                    help_text="Only one version per folder should be active at a time",
                )),
                ("embed_model",      models.CharField(max_length=100)),
                ("chunk_max_chars",  models.PositiveIntegerField(default=1500)),
                ("chunk_overlap",    models.PositiveIntegerField(default=150)),
                ("files_processed",  models.PositiveIntegerField(default=0)),
                ("files_skipped",    models.PositiveIntegerField(default=0)),
                ("chunks_indexed",   models.PositiveIntegerField(default=0)),
                ("error_message",    models.TextField(blank=True)),
                ("created_at",       models.DateTimeField(default=django.utils.timezone.now)),
                ("completed_at",     models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="SourceDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("version", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="documents",
                    to="ragbot.indexversion",
                )),
                ("drive_file_id",   models.CharField(max_length=255)),
                ("drive_file_name", models.CharField(max_length=512)),
                ("mime_type",       models.CharField(max_length=255)),
                ("char_count",      models.PositiveIntegerField(default=0)),
                ("chunk_count",     models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="DocumentChunk",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("version", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="chunks",
                    to="ragbot.indexversion",
                )),
                ("document", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="chunks",
                    to="ragbot.sourcedocument",
                )),
                ("chunk_index", models.PositiveIntegerField(
                    help_text="Position of this chunk within the source document"
                )),
                ("text", models.TextField()),
                ("embedding", models.BinaryField(
                    help_text="numpy float32 array serialised as raw bytes"
                )),
            ],
            options={"ordering": ["document", "chunk_index"]},
        ),
        migrations.AddConstraint(
            model_name="indexversion",
            constraint=models.UniqueConstraint(
                fields=["folder_id", "version_number"],
                name="unique_folder_version",
            ),
        ),
        migrations.AddIndex(
            model_name="indexversion",
            index=models.Index(fields=["folder_id", "is_active"], name="idx_folder_active"),
        ),
        migrations.AddIndex(
            model_name="sourcedocument",
            index=models.Index(fields=["version", "drive_file_id"], name="idx_doc_version_file"),
        ),
        migrations.AddIndex(
            model_name="documentchunk",
            index=models.Index(fields=["version"], name="idx_chunk_version"),
        ),
        migrations.AddIndex(
            model_name="documentchunk",
            index=models.Index(fields=["document", "chunk_index"], name="idx_chunk_doc_order"),
        ),
    ]
