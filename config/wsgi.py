import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

# Start the event-forecast agent only in a real server process (runserver / waitress import this module;
# migrate, tests and other manage.py commands do not). Disable with FORECAST_AGENT=0 in .env.
from django.conf import settings  # noqa: E402

if settings.FORECAST_AGENT:
    from boxapp.forecast import ForecastAgent  # noqa: E402

    ForecastAgent.start()
