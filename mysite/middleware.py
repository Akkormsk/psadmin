from ipaddress import ip_address
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.http import HttpResponse
from django.utils.http import url_has_allowed_host_and_scheme


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
        if request.GET.get("workspace") == "1":
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            location = response.headers.get("Location")
            if response.status_code in {301, 302, 303, 307, 308} and location and url_has_allowed_host_and_scheme(
                location, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
            ):
                target = urlsplit(location)
                query = [(key, value) for key, value in parse_qsl(target.query, keep_blank_values=True) if key != "workspace"]
                response.headers["Location"] = urlunsplit(target._replace(query=urlencode([*query, ("workspace", "1")])))
        return response
