from django.contrib.admin import AdminSite

class SuperuserAdminSite(AdminSite):
    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser

admin_site = SuperuserAdminSite()
