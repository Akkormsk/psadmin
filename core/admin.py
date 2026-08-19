from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django import forms
from django.utils import timezone

from .avatar import optimize_avatar
from .models import Profile


User = get_user_model()
admin.site.unregister(User)


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "last_name",
                    "first_name",
                    "password1",
                    "password2",
                ),
            },
        ),
    )
    list_display = ("username", "last_name", "first_name", "is_staff", "is_active")
    search_fields = ("username", "first_name", "last_name")


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    class ProfileAdminForm(forms.ModelForm):
        avatar_upload = forms.ImageField(label="Новый аватар", required=False)
        remove_avatar = forms.BooleanField(label="Удалить аватар", required=False)

        class Meta:
            model = Profile
            fields = ("user", "role")

        def save(self, commit=True):
            profile = super().save(commit=False)
            upload = self.cleaned_data.get("avatar_upload")
            if self.cleaned_data.get("remove_avatar"):
                profile.avatar_data = None
                profile.avatar_content_type = ""
                profile.avatar_updated_at = None
            elif upload:
                profile.avatar_data, profile.avatar_content_type = optimize_avatar(upload)
                profile.avatar_updated_at = timezone.now()
            if commit:
                profile.save()
            return profile

    form = ProfileAdminForm
    fields = ("user", "role", "avatar_upload", "remove_avatar")
    list_display = ("user", "role", "has_avatar")
    list_filter = ("role",)
    search_fields = ("user__username",)
