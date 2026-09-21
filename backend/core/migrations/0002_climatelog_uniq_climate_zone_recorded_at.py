# Adds the (zone, recorded_at) unique constraint for climate logs.
# Existing duplicate rows are collapsed to the oldest one first so the
# constraint can be applied on databases that already hold dirty data.

from django.db import migrations, models
from django.db.models import Count, Min


def drop_duplicate_climate_logs(apps, schema_editor):
    ClimateLog = apps.get_model("core", "ClimateLog")
    dupes = (
        ClimateLog.objects.values("zone", "recorded_at")
        .annotate(cnt=Count("id"), keep_id=Min("id"))
        .filter(cnt__gt=1)
    )
    for dup in dupes:
        ClimateLog.objects.filter(
            zone_id=dup["zone"], recorded_at=dup["recorded_at"]
        ).exclude(id=dup["keep_id"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(drop_duplicate_climate_logs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="climatelog",
            constraint=models.UniqueConstraint(
                fields=("zone", "recorded_at"),
                name="uniq_climate_zone_recorded_at",
            ),
        ),
    ]
