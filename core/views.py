from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from .avatar import optimize_avatar
from .forms import AccountPasswordChangeForm, AvatarUploadForm
from .models import Profile

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

    return render(request, "core/account.html", {
        "profile": profile,
        "initials": _initials(request.user),
        "avatar_form": avatar_form,
        "password_form": password_form,
    })


@login_required
def avatar(request):
    return _avatar_response(Profile.objects.get_or_create(user=request.user)[0])


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
