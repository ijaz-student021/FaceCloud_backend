from django.core.management.base import BaseCommand, CommandError

from search.semantic.embedders import SemanticSearchUnavailable
from search.semantic.service import enabled, get_embedder, index_images, pending_images


class Command(BaseCommand):
    help = (
        "Index accepted photographs for search by what is in them (needs SEMANTIC_SEARCH_ENABLED=True and the model installed). Safe to run again: photographs "
        "already indexed by the current model are skipped. --reindex recomputes them all (after the model changed)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many photographs (0: all).")
        parser.add_argument("--batch-size", type=int, default=16, help="Photographs embedded per model call.")
        parser.add_argument("--reindex", action="store_true", help="Recompute vectors that already exist.")
        parser.add_argument("--owner", type=int, default=None, help="Only this user id.")

    def handle(self, *args, limit, batch_size, reindex, owner, **options):
        if not enabled():
            raise CommandError("SEMANTIC_SEARCH_ENABLED is False: nothing to do.")
        try:
            embedder = get_embedder()
        except SemanticSearchUnavailable as error:
            raise CommandError(str(error)) from error
        queryset = pending_images(embedder, owner=owner, reindex=reindex)
        total = queryset.count() if not limit else min(limit, queryset.count())
        self.stdout.write(f"{total} photograph(s) to index with {getattr(embedder, 'name', 'the model')}.")
        done = failed = 0
        ids = list(queryset.values_list("pk", flat=True)[: limit or None])
        for start in range(0, len(ids), max(1, batch_size)):
            chunk = ids[start:start + batch_size]
            result = index_images(list(queryset.model.objects.filter(pk__in=chunk)), embedder, reindex=reindex)
            done += result["indexed"]
            failed += result["failed"]
            self.stdout.write(f"  {min(start + batch_size, len(ids))}/{len(ids)}")
        self.stdout.write(self.style.SUCCESS(f"Indexed {done}, could not read {failed}."))
