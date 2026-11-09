"""Take photographs the checks rejected out of person folders (a one-off clean-up of data from before they were kept out).

    python manage.py cleanup_rejected_folder_members            # a dry run: lists the photographs and folders, changes nothing
    python manage.py cleanup_rejected_folder_members --apply    # takes them out

A photograph refused at upload, rejected by the face check or by the quality decision belongs in no folder. Its memberships become REMOVED, the folder is
refreshed, and a folder left empty is archived (unless its owner named it). The same rule now runs when a photograph is rejected (see
``identities.services.remove_photo_from_folders``); this command repairs what was grouped before that. ``--owner`` limits it to one account.
"""
from django.core.management.base import BaseCommand

from identities.models import IdentityMembership
from identities.services import folder_is_renamed, photo_is_rejected, remove_photo_from_folders
from uploads.models import ImageUpload


class Command(BaseCommand):
    help = "Remove rejected photographs from person folders (a dry run unless --apply is given)."

    def add_arguments(self, parser):
        parser.add_argument("--owner", type=int, help="Only this owner's folders.")
        parser.add_argument("--apply", action="store_true", help="Make the change (the default is a dry run).")

    def handle(self, *args, **options):
        memberships = IdentityMembership.objects.exclude(state__in=[IdentityMembership.State.REMOVED, IdentityMembership.State.REJECTED])
        if options["owner"]:
            memberships = memberships.filter(owner_id=options["owner"])
        image_ids = sorted(set(memberships.values_list("identity_face__image_id", flat=True)))
        rejected = [image for image in ImageUpload.objects.filter(pk__in=image_ids).select_related("batch") if photo_is_rejected(image)]
        out = self.stdout.write
        folders = {}
        for image in rejected:
            for membership in memberships.filter(identity_face__image=image).select_related("identity_group"):
                if membership.identity_group:
                    folders.setdefault(membership.identity_group, []).append(image.pk)
        out(f"Rejected photographs still in a person folder: {len(rejected)}")
        for group, photos in sorted(folders.items(), key=lambda item: item[0].pk):
            note = "named by its owner, kept" if folder_is_renamed(group) else "archived if it ends up empty"
            out(f"  folder {group.pk} \"{group.display_name}\" (created {group.created_at:%d %b %Y}): photos {photos} ({note})")
        if not options["apply"]:
            out("\nDry run: nothing was changed. Run again with --apply to take them out.")
            return
        removed, archived = 0, []
        for image in rejected:
            result = remove_photo_from_folders(image)
            removed += result["memberships"]
            archived += result["archived"]
        out(f"\nApplied: {removed} memberships removed, {len(archived)} folders archived ({archived}).")
