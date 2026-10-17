import django.db.models.deletion
from django.db import migrations, models


def backfill(apps, schema_editor):
    """Existing emails only know their last execution; keep that one."""
    Email = apps.get_model("inbox", "Email")
    EmailExecution = apps.get_model("inbox", "EmailExecution")
    rows = [EmailExecution(email_id=email_id, execution_id=execution_id)
            for email_id, execution_id in Email.objects.exclude(last_execution_id="")
            .values_list("id", "last_execution_id").iterator()]
    EmailExecution.objects.bulk_create(rows, batch_size=1000, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("inbox", "0004_email_needed_review_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailExecution",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("execution_id", models.CharField(max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "email",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="executions", to="inbox.email"
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("execution_id", "email"), name="unique_execution_per_email")
                ],
            },
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
