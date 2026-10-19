from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from organizations.models import Organization, OrganizationMembership

from .models import AuthEvent, PendingRegistration, User
from .services import has_moderator_organization


class ModeratorOrganizationMixin:
    """A moderator account must belong to an organization: the form asks for one and creates the membership when the account is saved.

    Administrators and users are unaffected. The check also covers an account that is already a moderator and has no membership (it can be
    saved only once an organization is chosen, or its role is changed).
    """

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("role") == "moderator":
            chosen = cleaned.get("moderator_organization")
            existing = self.instance.pk is not None and has_moderator_organization(self.instance)
            if chosen is None and not existing:
                self.add_error(
                    "moderator_organization",
                    "A moderator works for an organization: choose one (or create the organization first).",
                )
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=commit)
        chosen = self.cleaned_data.get("moderator_organization")
        if chosen is not None and self.cleaned_data.get("role") == "moderator":
            def attach():
                OrganizationMembership.objects.update_or_create(
                    organization=chosen, user=user,
                    defaults={"role": OrganizationMembership.Role.MODERATOR, "status": OrganizationMembership.Status.ACTIVE},
                )
            if commit:
                attach()
            else:
                original = self.save_m2m
                self.save_m2m = lambda: (original(), attach())
        return user


def _organizations():
    return Organization.objects.filter(is_personal=False, status=Organization.Status.ACTIVE).order_by("name")


class FaceCloudUserChangeForm(ModeratorOrganizationMixin, UserChangeForm):
    moderator_organization = forms.ModelChoiceField(
        queryset=Organization.objects.none(), required=False, label="Moderator organization",
        help_text="Required when the role is moderator and the account has no moderator membership yet.",
    )

    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["moderator_organization"].queryset = _organizations()


class FaceCloudUserCreationForm(ModeratorOrganizationMixin, UserCreationForm):
    role = forms.ChoiceField(choices=User.ROLE_CHOICES, initial="user")
    moderator_organization = forms.ModelChoiceField(
        queryset=Organization.objects.none(), required=False, label="Moderator organization",
        help_text="Required when the role is moderator.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "role")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["moderator_organization"].queryset = _organizations()


@admin.register(User)
class FaceCloudUserAdmin(UserAdmin):
    form = FaceCloudUserChangeForm
    add_form = FaceCloudUserCreationForm
    fieldsets = UserAdmin.fieldsets + (
        ("FaceCloud", {"fields": ("role", "moderator_organization", "is_email_verified", "is_suspended", "suspension_reason", "token_version")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "email", "password1", "password2", "role", "moderator_organization")}),
    )
    list_display = UserAdmin.list_display + ("role", "is_email_verified", "is_suspended")


@admin.register(AuthEvent)
class AuthEventAdmin(admin.ModelAdmin):
    list_display = ("event", "user", "identifier", "ip_address", "created_at")
    list_filter = ("event", "created_at")
    search_fields = ("user__username", "user__email", "identifier")
    readonly_fields = ("user", "event", "identifier", "ip_address", "details", "created_at")


@admin.register(PendingRegistration)
class PendingRegistrationAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "username",
        "full_name",
        "email",
        "status",
        "failed_attempts",
        "token_expires_at",
        "created_at",
    )
    list_filter = ("status", "created_at", "token_expires_at")
    search_fields = ("public_id", "username", "email")
    readonly_fields = (
        "public_id",
        "username",
        "full_name",
        "email",
        "password_hash",
        "token_hash",
        "status",
        "failed_attempts",
        "resend_count",
        "resend_window_started_at",
        "token_expires_at",
        "resend_available_at",
        "consumed_at",
        "created_user",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
