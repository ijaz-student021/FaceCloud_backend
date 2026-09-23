from rest_framework import serializers

from .models import ConsentPurpose, OrganizationExport


class ConsentRequestSerializer(serializers.Serializer):
    purpose = serializers.ChoiceField(choices=ConsentPurpose.choices, default=ConsentPurpose.FACE_IDENTITY)
    granted = serializers.BooleanField()
    policy_version = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    erase_facial_data = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs["erase_facial_data"] and attrs["granted"]:
            raise serializers.ValidationError(
                {"erase_facial_data": "This option only applies when withdrawing consent."}
            )
        return attrs


class FacialDataErasureSerializer(serializers.Serializer):
    confirm = serializers.BooleanField()

    def validate_confirm(self, value):
        if value is not True:
            raise serializers.ValidationError("Set confirm to true to permanently erase your facial data.")
        return value


class OrganizationExportSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationExport
        fields = [
            "id", "organization", "requested_by", "status", "file_size",
            "row_counts", "truncated", "failure_reason", "generated_at",
            "expires_at", "created_at",
        ]
        read_only_fields = fields
