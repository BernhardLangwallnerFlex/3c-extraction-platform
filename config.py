import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

API_KEY = os.getenv("INVOICE_API_KEY", "https://product.soundstrue.com/the-spirituality-of-internal-family-systems/live-event/?utm_source=%5BKL%5D%200-30%20Day%20Engaged&utm_medium=email&utm_campaign=C260120-IFS-Live-Replay-FullList%20%2801KF0ZM8E2BRPV886A33X9K3PB%29&tw_source=Klaviyo&tw_profile_id=01FXHWSW2HR0XDY3SPBVH4JEY3&tw_medium=campaign&_kx=xk_WkwFbjbBKfvbqt25-qxZXTO5ZQbG6gCq10x5iVVWKcuTrmCPOximmwM6k7MCz.JMDgaq#/		")