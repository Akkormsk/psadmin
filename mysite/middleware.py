from ipaddress import ip_address
from pathlib import Path
import re

from django.http import HttpResponse


_RELEASE_SHA = Path(__file__).resolve().parent.parent / ".release-sha"
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


def release_revision() -> str | None:
    try:
        revision = _RELEASE_SHA.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return revision if _SHA_RE.fullmatch(revision) else None


class InternalHealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.META.get("HTTP_HOST", "").split(":", 1)[0]
        remote = request.META.get("REMOTE_ADDR", "")
        try:
            host_ip = ip_address(host)
            if host_ip.is_private and not host_ip.is_loopback and ip_address(remote).is_private:
                return HttpResponse("ok")
        except ValueError:
            pass
        return self.get_response(request)


class WorkspaceFrameOptionsMiddleware:
    """Allow only explicitly embedded app pages inside our same-origin workspace shell."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if revision := release_revision():
            response.headers["X-PSADMIN-Revision"] = revision
        if request.GET.get("workspace") == "1":
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response
