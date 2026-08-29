import json

from django.core.management.base import BaseCommand
from django.db.models import Q

from tenders.catalog import CatalogSyncError, OasisClient
from tenders.models import CatalogProduct, CatalogSupplier, CatalogSyncRun


class Command(BaseCommand):
    help = "Показывает состояние каталогов без изменения данных"

    def add_arguments(self, parser):
        parser.add_argument("--check-oasis", action="store_true")

    def handle(self, *args, **options):
        suppliers = []
        for supplier in CatalogSupplier.objects.all():
            products = CatalogProduct.objects.filter(supplier=supplier)
            suppliers.append({
                "code": supplier.code,
                "status": supplier.sync_status,
                "last_synced_at": supplier.last_synced_at.isoformat() if supplier.last_synced_at else None,
                "products": products.count(),
                "active_products": products.filter(is_active=True).count(),
                "available_products": products.filter(Q(total_stock__gt=0) | Q(is_on_order=True), is_active=True).count(),
            })
        latest_runs = [{
            "source": run.supplier.code,
            "status": run.status,
            "received": run.received_count,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        } for run in CatalogSyncRun.objects.select_related("supplier")[:5]]
        result = {"database_sources": suppliers, "latest_sync_runs": latest_runs}
        if options["check_oasis"]:
            try:
                client = OasisClient()
                categories = client.get("/v4/categories", {"format": "json"})
                category_items = categories.get("items", []) if isinstance(categories, dict) else categories
                products = client.get("/v4/products", {
                    "format": "json", "limit": 1, "offset": 0, "available": 1,
                    "fields": "id,article,name,price,discount_price,total_stock",
                })
                product_items = products.get("items", []) if isinstance(products, dict) else products
                result["oasis_live"] = {
                    "status": "success",
                    "categories_received": len(category_items) if isinstance(category_items, list) else None,
                    "sample_products_received": len(product_items) if isinstance(product_items, list) else None,
                }
            except CatalogSyncError as exc:
                result["oasis_live"] = {"status": "failed", "message": str(exc)}
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
