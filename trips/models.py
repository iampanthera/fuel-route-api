from django.db import models


class FuelStation(models.Model):
    """One physical truck stop from the OPIS price file.

    Duplicate rows for the same OPIS ID are collapsed on import (lowest price wins).
    lat/lon are null when the city could not be geocoded; such rows are kept for
    auditing but never used for routing.
    """

    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.IntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=7, decimal_places=4)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)
    geocode_source = models.CharField(max_length=20, blank=True)
    geocode_precision = models.CharField(max_length=20, default="city")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["opis_id"]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
