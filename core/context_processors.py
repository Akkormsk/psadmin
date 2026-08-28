import os


def app_commit(request):
    return {"app_commit": os.getenv("APP_COMMIT", "673255d")}
