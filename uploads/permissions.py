from rest_framework.permissions import BasePermission


class IsUploadOwner(BasePermission):
    message = "You do not have access to this upload."

    def has_object_permission(self, request, view, obj):
        return obj.owner_id == request.user.id
