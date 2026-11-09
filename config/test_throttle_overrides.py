"""Request rates can be changed from the environment (THROTTLE_RATE_<SCOPE>) and a bad value stops the start."""
import importlib
import os
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

import config.settings as base_settings


def rates_with(environment):
    """The throttle rates a fresh import of the settings module would give under ``environment``."""
    with mock.patch.dict(os.environ, environment):
        reloaded = importlib.reload(base_settings)
        return dict(reloaded.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])


class ThrottleOverrideTests(SimpleTestCase):
    def tearDown(self):
        importlib.reload(base_settings)

    def test_without_an_override_the_defaults_stand(self):
        self.assertEqual(rates_with({"THROTTLE_RATE_LOGIN": ""})["login"], "20/hour")

    def test_the_login_rate_can_be_raised_for_a_demo(self):
        rates = rates_with({"THROTTLE_RATE_LOGIN": "300/hour"})
        self.assertEqual(rates["login"], "300/hour")
        self.assertEqual(rates["registration"], "10/hour")           # nothing else moves

    def test_any_scope_can_be_overridden_by_its_upper_case_name(self):
        self.assertEqual(rates_with({"THROTTLE_RATE_IMAGE_UPLOAD": "5/minute"})["image_upload"], "5/minute")

    def test_a_malformed_rate_refuses_to_start_and_names_the_variable(self):
        for bad in ("lots", "300", "300/week", "0/hour", "-5/hour"):
            with self.subTest(bad=bad), self.assertRaisesMessage(ImproperlyConfigured, "THROTTLE_RATE_LOGIN"):
                rates_with({"THROTTLE_RATE_LOGIN": bad})
