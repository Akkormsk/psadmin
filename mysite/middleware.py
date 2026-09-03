from ipaddress import ip_address

from django.http import HttpResponse


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
