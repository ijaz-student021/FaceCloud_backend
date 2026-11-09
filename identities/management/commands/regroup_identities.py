"""Re-run one owner's person-folder grouping with the current limits.

    python manage.py regroup_identities --owner 7              # a dry run: prints what would change, writes nothing
    python manage.py regroup_identities --owner 7 --apply      # makes the change

Photographs the system placed are placed again, in upload order, with the identity model's current limits (see ``identities/regrouping.py``). A folder the owner
renamed, a folder holding a face the owner confirmed or moved, and those faces are never touched. Nothing is deleted: an emptied folder is merged into the one that took
most of its faces (or archived), every move is written to the folder's history and the owner's activity log.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from identities.providers import get_identity_provider
from identities.regrouping import apply_regroup, plan_regroup, public


class Command(BaseCommand):
    help = "Re-group one owner's faces with the current identity limits (a dry run unless --apply is given)."

    def add_arguments(self, parser):
        parser.add_argument("--owner", type=int, required=True, help="The id of the owner whose person folders to regroup.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Only report what would change (the default).")
        mode.add_argument("--apply", action="store_true", help="Make the change.")
        parser.add_argument("--verbose", action="store_true", help="List every photograph that would move.")

    def handle(self, *args, **options):
        owner = get_user_model().objects.filter(pk=options["owner"]).first()
        if owner is None:
            raise CommandError(f"There is no user with id {options['owner']}.")
        provider = get_identity_provider()
        plan = plan_regroup(owner, provider)
        out = self.stdout.write
        report = public(plan)
        out(f"Owner {owner.pk} ({owner.get_username()}), model {report['provider']}, limits {report['limits']}")
        out(f"Faces: {report['faces_total']} in folders or waiting for review; {report['faces_regrouped']} can be placed again")
        out(f"Folders before: {report['before']['folders']} (sizes {report['before']['sizes']}), waiting for review {report['before']['needs_review']}")
        out(f"Folders after:  {report['after']['folders']} (sizes {report['after']['sizes']}, {report['after']['new_folders']} new), waiting for review {report['after']['needs_review']}")
        if report["left_alone"]:
            out("Left alone (never changed):")
            for folder in report["left_alone"]:
                out(f"  folder {folder['id']} \"{folder['name']}\": {folder['reason']} ({folder['photos']} faces)")
        out(f"Faces that would move: {len(report['moves'])}")
        if options["verbose"]:
            for move in report["moves"]:
                out(f"  photo {move['image_id']}: folder {move['from_group']} ({move['from_state']}) -> {move['to_group']} ({move['to_state']})")
        for entry in report["merge_or_archive"]:
            out(f"  folder {entry['folder']} would be " + (f"merged into folder {entry['into']}" if entry["into"] else "archived (nothing moved into another folder)"))
        if not options["apply"]:
            out("\nDry run: nothing was changed. Run again with --apply to make this change.")
            return
        summary = apply_regroup(plan, owner)
        out(f"\nApplied: {summary['moved']} faces moved, {len(summary['created_folders'])} folders created, {len(summary['merged'])} merged, {len(summary['archived'])} archived.")
