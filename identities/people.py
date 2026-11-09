"""The people of a library as tappable chips: who is in my photographs, most photographs first."""
import re

from django.db.models import Count, Q

from .avatars import CONFIRMED
from .models import IdentityGroup

UNNAMED = re.compile(r"^Person [0-9]{3,}$")           # the name a folder gets before its owner renames it ("Person 001")
UNNAMED_REGEX = r"^Person [0-9]{3,}$"
DEFAULT_LIMIT = 12
MAX_LIMIT = 50


def is_named(group):
    return UNNAMED.match(group.display_name or "") is None


def people_queryset(user, *, query="", include_unnamed=True):
    """The owner's active folders that hold at least one photograph, with ``photo_count`` (distinct photographs of grouped faces), most photographs first."""
    people = (
        IdentityGroup.objects.filter(owner=user, state=IdentityGroup.State.ACTIVE)
        .annotate(photo_count=Count("memberships__identity_face__image", filter=Q(memberships__state__in=CONFIRMED), distinct=True))
        .filter(photo_count__gt=0)
    )
    if query:
        people = people.filter(display_name__icontains=query)
    if not include_unnamed:
        people = people.exclude(display_name__regex=UNNAMED_REGEX)
    return people.order_by("-photo_count", "display_name", "pk")
