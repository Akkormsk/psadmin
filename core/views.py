from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from .avatar import optimize_avatar, optimize_background
from .forms import AccountPasswordChangeForm, AvatarUploadForm, BackgroundUploadForm
from .models import Profile, ProfileBackground

@login_required
def index(request):
    return render(request, "core/index.html")


def _initials(user):
    letters = [part[:1].upper() for part in (user.first_name.strip(), user.last_name.strip()) if part]
    return "".join(letters) or "?"


@login_required
def account(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    avatar_form = AvatarUploadForm()
    background_form = BackgroundUploadForm()
    password_form = AccountPasswordChangeForm(request.user)

    if request.method == "POST" and request.POST.get("action") == "avatar":
        avatar_form = AvatarUploadForm(request.POST, request.FILES)
        if avatar_form.is_valid():
            profile.avatar_data, profile.avatar_content_type = optimize_avatar(avatar_form.cleaned_data["avatar"])
            profile.avatar_updated_at = timezone.now()
            profile.save(update_fields=("avatar_data", "avatar_content_type", "avatar_updated_at"))
            messages.success(request, "Аватар обновлён.")
            return redirect("account")
    elif request.method == "POST" and request.POST.get("action") == "remove_avatar":
        profile.avatar_data = None
        profile.avatar_content_type = ""
        profile.avatar_updated_at = None
        profile.save(update_fields=("avatar_data", "avatar_content_type", "avatar_updated_at"))
        messages.success(request, "Аватар удалён.")
        return redirect("account")
    elif request.method == "POST" and request.POST.get("action") == "password":
        password_form = AccountPasswordChangeForm(request.user, request.POST)
        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Пароль изменён.")
            return redirect("account")
    elif request.method == "POST" and request.POST.get("action") == "background":
        background_form = BackgroundUploadForm(request.POST, request.FILES)
        if background_form.is_valid():
            image_data, content_type = optimize_background(background_form.cleaned_data["background"])
            ProfileBackground.objects.update_or_create(
                profile=profile,
                defaults={"image_data": image_data, "content_type": content_type},
            )
            profile.background_updated_at = timezone.now()
            profile.save(update_fields=("background_updated_at",))
            messages.success(request, "Фон обновлён.")
            return redirect("account")
    elif request.method == "POST" and request.POST.get("action") == "remove_background":
        ProfileBackground.objects.filter(profile=profile).delete()
        profile.background_updated_at = None
        profile.save(update_fields=("background_updated_at",))
        messages.success(request, "Установлен общий фон.")
        return redirect("account")

    return render(request, "core/account.html", {
        "profile": profile,
        "initials": _initials(request.user),
        "avatar_form": avatar_form,
        "background_form": background_form,
        "password_form": password_form,
    })


@login_required
def avatar(request):
    return _avatar_response(Profile.objects.get_or_create(user=request.user)[0])


@login_required
def background(request):
    background_file = ProfileBackground.objects.filter(profile__user=request.user).first()
    if background_file is None:
        raise Http404
    response = HttpResponse(bytes(background_file.image_data), content_type=background_file.content_type or "image/webp")
    response["Cache-Control"] = "private, max-age=86400"
    return response


@login_required
def user_avatar(request, user_id):
    profile = Profile.objects.filter(user_id=user_id).first()
    return _avatar_response(profile)


def _avatar_response(profile):
    if profile is None or not profile.avatar_data:
        raise Http404
    response = HttpResponse(bytes(profile.avatar_data), content_type=profile.avatar_content_type or "image/webp")
    response["Cache-Control"] = "private, max-age=300"
    return response
