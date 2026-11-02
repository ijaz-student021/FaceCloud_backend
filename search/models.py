from django.conf import settings
from django.db import models


class ImageEmbedding(models.Model):
    """What a photograph looks like to the search-by-meaning model: one L2-normalised vector per photograph (``search/semantic/``).

    It describes the **whole picture** (a beach, a red car, a birthday cake); it is not a face template and is kept apart from the face embeddings
    (``identities.IdentityFace``): erasing a person's facial data does not touch it, and it is not derived from any face crop. ``vector`` is
    ``dimension`` little-endian float16 values. ``model_checksum`` identifies the exact model that made it; vectors of another model are never
    compared with a query and are replaced by ``manage.py index_semantic_search --reindex``. It goes with the photograph when that is deleted.
    """

    image = models.OneToOneField("uploads.ImageUpload", on_delete=models.CASCADE, related_name="semantic_embedding")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="semantic_embeddings")
    vector = models.BinaryField()
    dimension = models.PositiveSmallIntegerField()
    model_name = models.CharField(max_length=60)
    model_checksum = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "model_checksum"], name="semantic_owner_model_idx")]
