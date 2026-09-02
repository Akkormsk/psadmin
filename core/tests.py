from io import BytesIO

from PIL import Image
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from .models import Profile


class HealthCheckTests(TestCase):
    def test_private_timeweb_health_check_does_not_require_public_host(self):
        response = self.client.get("/", HTTP_HOST="172.18.0.5:8000", REMOTE_ADDR="172.18.0.1")

        self.assertEqual(response.status_code, 200)

    def test_unknown_public_host_is_still_rejected(self):
        response = self.client.get("/", HTTP_HOST="untrusted.example", REMOTE_ADDR="203.0.113.10")

        self.assertEqual(response.status_code, 400)


class LogoutTests(TestCase):
    def test_user_can_log_out_from_the_main_page(self):
        user = get_user_model().objects.create_user(
            username="logout-test", password="password"
        )
        self.client.force_login(user)

        response = self.client.post(reverse("logout"))

        self.assertRedirects(response, reverse("login"))


class UserAdminTests(TestCase):
    def test_user_add_form_includes_name_fields(self):
        admin = get_user_model().objects.create_superuser(
            username="admin", password="password"
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("admin:auth_user_add"))

        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="last_name"')
        self.assertContains(response, 'name="first_name"')
        self.assertContains(response, 'name="password1"')
        self.assertContains(response, 'name="password2"')

    def test_profile_admin_includes_avatar_controls(self):
        admin = get_user_model().objects.create_superuser(
            username="profile-admin", password="password"
        )
        self.client.force_login(admin)
        profile = Profile.objects.get(user=admin)

        response = self.client.get(reverse("admin:core_profile_change", args=[profile.pk]))

        self.assertContains(response, 'name="avatar_upload"')
        self.assertContains(response, 'name="remove_avatar"')


class AccountTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="account-user",
            first_name="Ольга",
            last_name="Королева",
            password="old-password-123",
        )
        self.client.force_login(self.user)

    @staticmethod
    def avatar_file():
        output = BytesIO()
        Image.new("RGB", (640, 320), "#4f46e5").save(output, "PNG")
        return SimpleUploadedFile("avatar.png", output.getvalue(), content_type="image/png")

    @staticmethod
    def background_file():
        output = BytesIO()
        Image.new("RGB", (3200, 1800), "#0f766e").save(output, "JPEG")
        return SimpleUploadedFile("background.jpg", output.getvalue(), content_type="image/jpeg")

    def test_user_can_upload_optimized_avatar(self):
        response = self.client.post(
            reverse("account"),
            {"action": "avatar", "avatar": self.avatar_file()},
        )

        self.assertRedirects(response, reverse("account"))
        profile = Profile.objects.get(user=self.user)
        self.assertTrue(profile.avatar_data)
        self.assertEqual(profile.avatar_content_type, "image/webp")
        avatar_response = self.client.get(reverse("account_avatar"))
        self.assertEqual(avatar_response.status_code, 200)
        self.assertEqual(avatar_response["Content-Type"], "image/webp")
        with Image.open(BytesIO(avatar_response.content)) as image:
            self.assertEqual(image.size, (256, 256))

    def test_user_can_change_password_without_being_logged_out(self):
        response = self.client.post(
            reverse("account"),
            {
                "action": "password",
                "old_password": "old-password-123",
                "new_password1": "new-secure-password-456",
                "new_password2": "new-secure-password-456",
            },
        )

        self.assertRedirects(response, reverse("account"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-secure-password-456"))
        self.assertEqual(self.client.get(reverse("account")).status_code, 200)

    def test_user_can_upload_optimized_background_and_restore_default(self):
        response = self.client.post(reverse("account"), {"action": "background", "background": self.background_file()})

        self.assertRedirects(response, reverse("account"))
        profile = Profile.objects.get(user=self.user)
        self.assertTrue(profile.has_background)
        background_response = self.client.get(reverse("account_background"))
        self.assertEqual(background_response.status_code, 200)
        self.assertEqual(background_response["Content-Type"], "image/webp")
        with Image.open(BytesIO(background_response.content)) as image:
            self.assertLessEqual(max(image.size), 2560)

        response = self.client.post(reverse("account"), {"action": "remove_background"})
        self.assertRedirects(response, reverse("account"))
        profile.refresh_from_db()
        self.assertFalse(profile.has_background)
        self.assertEqual(self.client.get(reverse("account_background")).status_code, 404)

    def test_topbar_uses_full_name_and_links_to_account(self):
        response = self.client.get(reverse("index"))

        self.assertContains(response, "Королева Ольга")
        self.assertContains(response, reverse("account"))
        self.assertNotContains(response, "account-user")

# Create your tests here.
