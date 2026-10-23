from django.db import models


class Place(models.Model):
    """A named place photographs can belong to (a city, town or village from the offline gazetteer, ``places/gazetteer.py``).

    A row exists only once a photograph was placed there; it is shared by everyone, and what is private is *which photographs* of a person are in it (``ImageUpload.place``), not
    the place. ``latitude`` and ``longitude`` are the place's own centre from the gazetteer, never a photograph's position.
    """

    geoname_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=200)
    region = models.CharField(max_length=120, blank=True)
    country_code = models.CharField(max_length=2, db_index=True)
    country = models.CharField(max_length=100, blank=True)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return f"{self.name}, {self.country_code}"
