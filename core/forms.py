from django import forms
from django.contrib.auth.forms import PasswordChangeForm


class AvatarUploadForm(forms.Form):
    avatar = forms.ImageField(label="Новый аватар")

    def clean_avatar(self):
        avatar = self.cleaned_data["avatar"]
        if avatar.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Размер изображения не должен превышать 5 МБ.")
        return avatar


class BackgroundUploadForm(forms.Form):
    background = forms.ImageField(label="Новый фон")

    def clean_background(self):
        background = self.cleaned_data["background"]
        if background.size > 15 * 1024 * 1024:
            raise forms.ValidationError("Размер изображения не должен превышать 15 МБ.")
        return background


class AccountPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "Текущий пароль"
        self.fields["new_password1"].label = "Новый пароль"
        self.fields["new_password2"].label = "Повторите новый пароль"
