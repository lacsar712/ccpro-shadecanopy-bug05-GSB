from django.db import migrations, models
from django.db.models import Count, Min


def dedupe_climate_logs(apps, schema_editor):
    """Collapse pre-existing duplicate (zone, recorded_at) rows.

    The legacy upsert path could leave two climate rows for the same zone and
    sampling time. Keep the earliest row per pair (smallest id) and delete the
    rest, so the unique constraint can be applied without failing on dirty
    data.
    """
    ClimateLog = apps.get_model("core", "ClimateLog")
    duplicate_pairs = (
        ClimateLog.objects.values("zone_id", "recorded_at")
        .annotate(keep_id=Min("id"), row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for pair in duplicate_pairs:
        ClimateLog.objects.filter(
            zone_id=pair["zone_id"],
            recorded_at=pair["recorded_at"],
        ).exclude(pk=pair["keep_id"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            dedupe_climate_logs, migrations.RunPython.noop
        ),
        migrations.AddConstraint(
            model_name="climatelog",
            constraint=models.UniqueConstraint(
                fields=("zone", "recorded_at"),
                name="uniq_climate_recorded_at_per_zone",
            ),
        ),
    ]
