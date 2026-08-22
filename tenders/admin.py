from django.contrib import admin

from .models import TenderEstimate, TenderLine, TenderSettings


class TenderLineInline(admin.TabularInline):
    model = TenderLine
    extra = 0


@admin.register(TenderEstimate)
class TenderEstimateAdmin(admin.ModelAdmin):
    list_display = ("tender_number", "name", "owner", "reduction_percent", "updated_at")
    list_filter = ("owner", "updated_at")
    search_fields = ("tender_number", "name", "owner__first_name", "owner__last_name")
    autocomplete_fields = ("owner",)
    inlines = (TenderLineInline,)


@admin.register(TenderSettings)
class TenderSettingsAdmin(admin.ModelAdmin):
    list_display = ("vat_rate",)

    def has_add_permission(self, request):
        return not TenderSettings.objects.exists()
