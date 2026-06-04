from django.apps import AppConfig


class RagbotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ragbot"

    def ready(self):
        import ragbot.signals  # noqa — registers user_logged_in receiver