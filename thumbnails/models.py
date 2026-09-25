import uuid

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from uploads.models import ImageUpload


def thumbnail_upload_path(instance, filename):
    return f"users/{instance.owner_id}/thumbnails/{uuid.uuid4().hex}.jpg"


class ImageThumbnail(models.Model):
    """A small JPEG preview of an uploaded photo, generated on first request.

    It is a derivative of the user's own photo (user content, not facial data:
    nothing about faces is read or stored), kept so a gallery never has to load
    a multi-megabyte original just to draw a tile. Stored through the default
    storage, so it is encrypted at rest with everything else.
    """

    image = models.ForeignKey(ImageUpload, on_delete=models.CASCADE, related_name="thumbnails")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="image_thumbnails")
    size = models.PositiveSmallIntegerField(help_text="Longest edge in pixels that was requested.")
    file = models.FileField(upload_to=thumbnail_upload_path)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    file_size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["image_id", "size"]
        constraints = [
            models.UniqueConstraint(fields=["image", "size"], name="thumbnail_one_per_image_size"),
        ]

    def __str__(self):
        return f"thumbnail {self.size}px of image {self.image_id}"


@receiver(post_delete, sender=ImageThumbnail)
def delete_thumbnail_file(sender, instance, **kwargs):
    if instance.file:
        instance.file.storage.delete(instance.file.name)
