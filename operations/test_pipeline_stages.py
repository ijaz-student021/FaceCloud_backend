"""GET /api/system/health/ lists which enhancement stages are on and which are off and why (`checks.models.stages`), so nobody has to guess from the results."""
from django.test import SimpleTestCase

from .services import DEGRADED, HEALTHY, PIPELINE_STAGES, pipeline_stages

OFF = {"status": "disabled", "installed": True, "checksum_verified": True, "reason": "its setting is off"}
ON = {"status": HEALTHY, "size_bytes": 1}
BROKEN = {"status": DEGRADED, "installed": False, "checksum_verified": False, "code": "unreadable"}


def everything(**states):
    names = {name for _key, _label, models, _instead, _consent in PIPELINE_STAGES for name in models}
    return {name: states.get(name, OFF) for name in names}


class PipelineStagesTests(SimpleTestCase):
    def by_key(self, models):
        return {item["stage"]: item for item in pipeline_stages(models)}

    def test_the_stages_come_in_pipeline_order_with_plain_labels(self):
        stages = pipeline_stages(everything())
        self.assertEqual([item["stage"] for item in stages], ["low_light", "denoise", "deblur", "face_restore", "upscale", "detail", "colourize"])
        self.assertTrue(all(item["label"] for item in stages))

    def test_a_stage_is_on_when_one_of_its_models_is_healthy_and_names_it(self):
        item = self.by_key(everything(zero_dce=ON))["low_light"]
        self.assertEqual((item["active"], item["model"]), (True, "zero_dce"))
        self.assertNotIn("reason", item)

    def test_the_preferred_healthy_model_is_the_one_named(self):
        item = self.by_key(everything(zero_dce=ON, retinexformer=ON))["low_light"]
        self.assertEqual(item["model"], "zero_dce")
        self.assertEqual(self.by_key(everything(codeformer=OFF, gfpgan=ON))["face_restore"]["model"], "gfpgan")

    def test_an_off_stage_says_why_and_what_runs_in_its_place(self):
        item = self.by_key(everything())["denoise"]
        self.assertEqual((item["active"], item["model"]), (False, None))
        self.assertEqual(item["reason"], "nafnet_sidd: its setting is off")
        self.assertEqual(item["instead"], "an OpenCV noise filter")

    def test_a_model_whose_file_cannot_be_used_is_off_with_the_reason(self):
        item = self.by_key(everything(nafnet=BROKEN))["deblur"]
        self.assertFalse(item["active"])
        self.assertIn("nafnet: its file cannot be used (unreadable)", item["reason"])

    def test_every_model_of_an_off_stage_is_listed(self):
        reason = self.by_key(everything())["face_restore"]["reason"]
        self.assertIn("codeformer: its setting is off", reason)
        self.assertIn("gfpgan: its setting is off", reason)

    def test_face_restoration_says_it_needs_consent_and_nothing_else_does(self):
        stages = self.by_key(everything(codeformer=ON))
        self.assertEqual(stages["face_restore"]["needs_consent"], "Face restoration needs the person's face-processing consent.")
        self.assertEqual([key for key, item in stages.items() if "needs_consent" in item], ["face_restore"])

    def test_a_stage_none_of_whose_models_is_known_is_off_without_an_error(self):
        item = self.by_key({})["upscale"]
        self.assertEqual((item["active"], item["reason"]), (False, "no model is configured"))
