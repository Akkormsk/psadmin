from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse


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

# Create your tests here.
