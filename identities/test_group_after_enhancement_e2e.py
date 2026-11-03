"""The real path, nothing stubbed but the face model: a photograph is enhanced FIRST and its person folder is made AFTER (the automatic enhancement comes before the person folders)."""
import tempfile

from django.core.files.base import ContentFile
from django.test import override_settings

from enhancement.models import ImageEnhancementResult
from enhancement.test_auto_enhancement import picture

from .models import IdentityAnalysis
from .test_group_after_verdict import Pipeline
from .test_regrouping import Provider, a_face, angle


MEDIA = tempfile.mkdtemp(prefix="facecloud-enhance-then-group-")


@override_settings(MEDIA_ROOT=MEDIA, AUTO_ENHANCEMENT_ENABLED=True, AUTO_ENHANCEMENT_RENDER_POLICY="lightweight_v1", ENHANCEMENT_RENDER_POLICY="lightweight_v1", MAX_ACTIVE_ENHANCEMENTS_PER_USER=10)
class EnhancedFirstThenGroupedTests(Pipeline):
    def setUp(self):
        super().setUp()
        Provider.vectors = {1: angle(0), 2: angle(10)}
        self.batch.auto_enhance = True
        self.batch.save(update_fields=["auto_enhance"])

    def photo(self, faces=None, **kwargs):
        """The identity tests' photograph, with a real stored picture so that the enhancement can really run."""
        image = super().photo(faces, **kwargs)
        encoded = picture(self.count)
        image.file_extension, image.mime_type, image.file_size, image.width, image.height = ".png", "image/png", len(encoded), 360, 480
        image.save(update_fields=["file_extension", "mime_type", "file_size", "width", "height"])
        image.original_file.save(f"e2e-{self.count}.png", ContentFile(encoded))
        preprocessing = image.preprocessing_result
        preprocessing.output_format, preprocessing.output_name, preprocessing.output_width, preprocessing.output_height = "png", f"e2e-{self.count}.png", 360, 480
        preprocessing.processed_file_size = len(encoded)
        preprocessing.save()
        preprocessing.processed_file.save(f"e2e-{self.count}.png", ContentFile(encoded))
        return image

    def test_an_accepted_photograph_is_enhanced_and_then_grouped(self):
        image = self.photo([a_face(1)])
        self.check(image, "accepted")
        result = ImageEnhancementResult.objects.get(image=image)
        analysis = IdentityAnalysis.objects.get(image=image)
        self.assertEqual((result.trigger, result.status), ("auto", "completed"))
        self.assertEqual(analysis.status, "completed")
        self.assertLessEqual(result.finished_at, analysis.created_at, "the person folder was queued only after the enhancement had finished")
        (folder,) = self.folders()
        self.assertEqual(folder.member_count, 1)

    def test_with_automatic_enhancement_off_the_photograph_is_grouped_as_before_and_not_enhanced(self):
        self.batch.auto_enhance = False
        self.batch.save(update_fields=["auto_enhance"])
        image = self.photo([a_face(1)])
        self.check(image, "accepted")
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")

    def test_a_failed_enhancement_does_not_leave_the_photograph_ungrouped(self):
        from unittest.mock import patch

        image = self.photo([a_face(1)])
        with patch("enhancement.tasks.process_enhancement_result", side_effect=RuntimeError("model fell over")):
            self.check(image, "accepted")
        self.assertEqual(ImageEnhancementResult.objects.get(image=image).status, "failed")
        self.assertEqual(IdentityAnalysis.objects.get(image=image).status, "completed")
