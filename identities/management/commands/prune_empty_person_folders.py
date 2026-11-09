"""Delete the person folders that hold no photograph and close the gaps in the automatic names (a one-off clean-up of data from before this was automatic).

    python manage.py prune_empty_person_folders             # a dry run: lists the folders and the new names, changes nothing
    python manage.py prune_empty_person_folders --apply     # deletes them and renumbers

A folder is left with no photograph when its photographs are deleted for good or taken out of it. The server now deletes such a folder on its own and renumbers
the owner's other automatic names (``Person 004`` becomes ``Person 002``, the folder id does not change); this command does the same for folders left that way
before. A folder a photograph in the trash can still return to is kept, and so is a merged one. ``--owner`` limits it to one account.
"""
from django.core.management.base import BaseCommand

from identities.models import IdentityGroup
from identities.services import folder_has_no_photographs, prune_empty_folders, renamed_folder_ids, renumbering_plan


def empty_folders(owner_id=None):
    queryset = IdentityGroup.objects.filter(state=IdentityGroup.State.ACTIVE).select_related("owner").order_by("owner_id", "pk")
    if owner_id:
        queryset = queryset.filter(owner_id=owner_id)
    return [group for group in queryset if folder_has_no_photographs(group)]


class Command(BaseCommand):
    help = "Delete person folders with no photograph and renumber the automatic names (a dry run unless --apply is given)."

    def add_arguments(self, parser):
        parser.add_argument("--owner", type=int, help="Only this owner's folders.")
        parser.add_argument("--apply", action="store_true", help="Make the change (the default is a dry run).")

    def handle(self, *args, **options):
        empties = empty_folders(options.get("owner"))
        out = self.stdout.write
        if not empties:
            out("No person folder is empty.")
            return
        out(f"Person folders with no photograph: {len(empties)}")
        for group in empties:
            out(f"  folder {group.pk} \"{group.display_name}\" (owner {group.owner_id}, created {group.created_at:%d %b %Y})")
        if not options["apply"]:
            gone = {group.pk for group in empties}
            for owner in {group.owner for group in empties}:
                remaining = IdentityGroup.objects.filter(owner=owner).exclude(pk__in=gone)
                for pk, old, new in renumbering_plan(remaining, renamed_folder_ids(owner)):
                    out(f"  would rename folder {pk}: \"{old}\" -> \"{new}\"")
            out(f"{len(empties)} folder(s) would be deleted. Nothing was changed; add --apply to delete them and close the numbering.")
            return
        result = prune_empty_folders(empties, reason="The folder had no photographs left (clean-up).")
        out(f"Deleted {len(result['deleted'])} folder(s); renumbered {result['renumbered']} automatic name(s).")
