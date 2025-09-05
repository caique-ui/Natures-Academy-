from django.db import models

class Document(models.Model):
    drive_file_id = models.CharField(max_length=128, unique=True)
    title = models.CharField(max_length=512)
    mime_type = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.title} ({self.drive_file_id})"

class Chunk(models.Model):
    chunk_id = models.CharField(max_length=256, unique=True)  # e.g. "<fileid>_0"
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    text = models.TextField()
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def summary(self, n=200):
        return (self.text[:n] + "…") if len(self.text) > n else self.text
