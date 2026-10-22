from django.conf import settings
from django.db import models

from uploads.models import ImageUpload


class Album(models.Model):
    """A collection the person made by hand ("Eid 2026"): a title, a description, an optional cover and photographs they put in it.

    A photograph can be in any number of albums. Deleting a photograph takes it out of its albums; deleting an album never deletes photographs.
    """

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="albums")
    title = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    # The photograph shown as the album's cover; empty means "the first photograph added". Cleared when that photograph leaves the album.
    cover_image = models.ForeignKey(ImageUpload, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        indexes = [models.Index(fields=["owner", "-updated_at"], name="album_owner_updated_idx")]

    def __str__(self):
        return f"{self.owner_id}:{self.title}"


class AlbumImage(models.Model):
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="items")
    image = models.ForeignKey(ImageUpload, on_delete=models.CASCADE, related_name="album_items")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["album", "image"], name="album_image_unique")]
        indexes = [models.Index(fields=["album", "-added_at"], name="album_item_added_idx")]
