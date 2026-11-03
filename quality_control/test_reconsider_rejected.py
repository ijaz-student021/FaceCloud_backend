"""``manage.py reconsider_rejected_photos``: a photograph refused before quality checks stopped refusing photographs is run again; nothing else is."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from .models import ImageQualityDecision
from .test_effective_decision import EffectiveDecisionBase


class ReconsiderRejectedPhotosTests(EffectiveDecisionBase):
    def setUp(self):
        super().setUp()
        self.refused = self.photo(self.batch("personal"), "rejected", ["severe_blur", "underexposed"])
        self.accepted = self.photo(self.batch("personal"), "accepted")
        self.review = self.photo(self.batch("team"), "needs_review", ["underexposed"])

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        with patch("quality_control.management.commands.reconsider_rejected_photos.validate_image_faces_safely") as faces, \
                patch("quality_control.management.commands.reconsider_rejected_photos.enqueue_quality_analysis") as quality:
            call_command("reconsider_rejected_photos", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue(), faces, quality

    def test_a_dry_run_lists_the_refused_photographs_and_changes_nothing(self):
        out, _err, faces, quality = self.run_command()
        self.assertIn(f"photograph {self.refused.pk}", out)
        self.assertIn("severe_blur, underexposed", out)
        self.assertNotIn(f"photograph {self.accepted.pk} ", out)
        self.assertNotIn(f"photograph {self.review.pk} ", out)
        self.assertIn("Nothing was changed", out)
        faces.assert_not_called()
        quality.assert_not_called()
        self.assertEqual(ImageQualityDecision.objects.get(image=self.refused).decision, "rejected")

    def test_apply_runs_the_face_check_and_the_quality_check_for_the_refused_photographs_only(self):
        out, _err, faces, quality = self.run_command("--apply")
        faces.assert_called_once()
        quality.assert_called_once()
        self.assertEqual(faces.call_args.args[0].pk, self.refused.pk)
        self.assertEqual(quality.call_args.args[0].pk, self.refused.pk)
        self.assertIn("Re-run 1 photograph(s); 0 could not be re-run.", out)

    def test_one_photograph_can_be_chosen_and_a_failure_does_not_stop_the_others(self):
        out, err, faces, _quality = self.run_command("--apply", "--image", str(self.accepted.pk))
        self.assertIn("No photograph is refused", out)
        faces.assert_not_called()
        with patch("quality_control.management.commands.reconsider_rejected_photos.validate_image_faces_safely", side_effect=RuntimeError("boom")), \
                patch("quality_control.management.commands.reconsider_rejected_photos.enqueue_quality_analysis"):
            out, err = StringIO(), StringIO()
            call_command("reconsider_rejected_photos", "--apply", stdout=out, stderr=err)
        self.assertIn("could not be re-run (RuntimeError)", err.getvalue())
        self.assertIn("1 could not be re-run", out.getvalue())

    def test_a_photograph_in_the_trash_is_not_touched(self):
        from django.utils import timezone

        from uploads.models import ImageUpload

        ImageUpload.all_objects.filter(pk=self.refused.pk).update(trashed_at=timezone.now())
        out, _err, faces, _quality = self.run_command("--apply")
        self.assertIn("No photograph is refused", out)
        faces.assert_not_called()
