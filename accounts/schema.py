"""drf-spectacular extensions for FaceCloud's custom auth/serializer classes.

Imported from AccountsConfig.ready() so the extension registry picks these up
before schema generation runs.
"""

from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme


class FaceCloudJWTScheme(SimpleJWTScheme):
    target_class = "accounts.authentication.FaceCloudJWTAuthentication"
