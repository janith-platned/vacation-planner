from django.core.management.base import BaseCommand

from leave import notifications


class Command(BaseCommand):
    help = "Send any notification emails that have not been delivered yet (for example after a mail outage)."

    def handle(self, *args, **options):
        sent, failed = notifications.send_pending()
        self.stdout.write(f"Sent {sent}; {failed} still failing.")
