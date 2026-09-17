from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from .services import UNAVAILABLE, build_system_health


class SystemHealthView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    def get(self, request):
        health = build_system_health()
        response_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if health["status"] == UNAVAILABLE
            else status.HTTP_200_OK
        )
        return Response(health, status=response_status)

