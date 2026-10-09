from django.core.management.base import BaseCommand

from inbox.models import Playbook

DEFAULT_FACTS = """Office hours: Monday to Saturday, 9:00 to 18:00 Pakistan time.
We arrange FTL and LTL trucking across Pakistan, and FCL/LCL sea freight via Karachi and Port Qasim.
Typical road transit: Lahore to Karachi 2 to 3 days; Islamabad to Karachi 3 to 4 days.
Quotes are normally sent within 4 office hours once we have origin, destination, pickup date and weight or piece count."""

DEFAULT_NEVER = """Never quote prices or amounts in an email reply.
Never guarantee a delivery date or transit time.
Never admit liability for a claim; say the team is investigating."""


class Command(BaseCommand):
    help = "Create the default playbook row if none exists."

    def handle(self, *args, **options):
        if Playbook.objects.exists():
            self.stdout.write("Playbook already exists.")
            return
        Playbook.objects.create(facts=DEFAULT_FACTS, never_promise=DEFAULT_NEVER, updated_by="setup")
        self.stdout.write(self.style.SUCCESS("Default playbook created."))
