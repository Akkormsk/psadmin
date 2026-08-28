import os


def app_commit(request):
    return {"app_commit": os.getenv("APP_COMMIT", "ad0f4f3")}
