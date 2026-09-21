import importlib
import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import ClimateLog, Greenhouse, Zone
from core.serializers import ClimateLogSerializer

User = get_user_model()

CONFLICT_TEXT = "同一分区在该采样时刻已有气候记录"


def make_greenhouse_zone(code="A-01", greenhouse=None):
    greenhouse = greenhouse or Greenhouse.objects.create(
        name="测试棚", location="东区", area_m2=Decimal("100.00")
    )
    zone = Zone.objects.create(
        greenhouse=greenhouse, zone_code=code, crop_name="番茄", status=Zone.STATUS_GROWING
    )
    return greenhouse, zone


def climate_payload(zone, recorded_at, humidity="60.00"):
    return {
        "zoneId": zone.id,
        "recordedAt": recorded_at.isoformat(),
        "tempC": "24.50",
        "humidityPct": humidity,
        "parUmol": "300.00",
        "co2Ppm": "600.00",
    }


class ClimateLogConflictTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        _, self.zone = make_greenhouse_zone()
        self.t1 = timezone.now().replace(microsecond=0)
        self.url = reverse("climate-log-list")

    def test_duplicate_submit_is_rejected_and_never_overwrites(self):
        r1 = self.client.post(self.url, climate_payload(self.zone, self.t1, "60.00"), format="json")
        self.assertEqual(r1.status_code, 201)

        r2 = self.client.post(self.url, climate_payload(self.zone, self.t1, "33.00"), format="json")
        self.assertEqual(r2.status_code, 400)
        self.assertIn("recordedAt", r2.data)
        self.assertIn(CONFLICT_TEXT, str(r2.data["recordedAt"]))

        # Still exactly one row, and its humidity must not have been swapped.
        rows = ClimateLog.objects.filter(zone=self.zone, recorded_at=self.t1)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().humidity_pct, Decimal("60.00"))

    def test_update_moving_row_onto_occupied_timestamp_is_rejected(self):
        t2 = self.t1 + timedelta(hours=1)
        row_a = ClimateLog.objects.create(
            zone=self.zone, recorded_at=self.t1,
            temp_c=Decimal("24.00"), humidity_pct=Decimal("60.00"),
        )
        row_b = ClimateLog.objects.create(
            zone=self.zone, recorded_at=t2,
            temp_c=Decimal("25.00"), humidity_pct=Decimal("70.00"),
        )

        url_b = reverse("climate-log-detail", args=[row_b.id])
        resp = self.client.put(url_b, climate_payload(self.zone, self.t1, "70.00"), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("recordedAt", resp.data)

        row_b.refresh_from_db()
        self.assertEqual(row_b.recorded_at, t2)
        row_a.refresh_from_db()
        self.assertEqual(row_a.recorded_at, self.t1)

    def test_same_timestamp_in_another_zone_is_allowed(self):
        row = ClimateLog.objects.create(
            zone=self.zone, recorded_at=self.t1 + timedelta(hours=2),
            temp_c=Decimal("25.00"), humidity_pct=Decimal("70.00"),
        )
        _, zone2 = make_greenhouse_zone(code="A-02")

        url = reverse("climate-log-detail", args=[row.id])
        resp = self.client.put(url, climate_payload(zone2, self.t1, "70.00"), format="json")
        self.assertEqual(resp.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.zone_id, zone2.id)
        self.assertEqual(row.recorded_at, self.t1)

    def test_updating_other_fields_while_keeping_timestamp_is_allowed(self):
        row = ClimateLog.objects.create(
            zone=self.zone, recorded_at=self.t1,
            temp_c=Decimal("24.00"), humidity_pct=Decimal("60.00"),
        )
        url = reverse("climate-log-detail", args=[row.id])
        resp = self.client.patch(
            url, {"zoneId": self.zone.id, "recordedAt": self.t1.isoformat(), "humidityPct": "88.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.recorded_at, self.t1)
        self.assertEqual(row.humidity_pct, Decimal("88.00"))

    def test_timestamp_can_be_reused_after_old_row_is_deleted(self):
        resp = self.client.post(self.url, climate_payload(self.zone, self.t1), format="json")
        self.assertEqual(resp.status_code, 201)

        row_id = resp.data["id"]
        delete_url = reverse("climate-log-detail", args=[row_id])
        self.assertEqual(self.client.delete(delete_url).status_code, 204)

        resp_again = self.client.post(self.url, climate_payload(self.zone, self.t1, "42.00"), format="json")
        self.assertEqual(resp_again.status_code, 201)
        self.assertEqual(
            ClimateLog.objects.filter(zone=self.zone, recorded_at=self.t1).count(), 1
        )
        self.assertEqual(
            ClimateLog.objects.get(zone=self.zone, recorded_at=self.t1).humidity_pct,
            Decimal("42.00"),
        )

    def test_database_constraint_blocks_bypassing_the_api(self):
        ClimateLog.objects.create(
            zone=self.zone, recorded_at=self.t1,
            temp_c=Decimal("24.00"), humidity_pct=Decimal("60.00"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ClimateLog.objects.create(
                    zone=self.zone, recorded_at=self.t1,
                    temp_c=Decimal("25.00"), humidity_pct=Decimal("61.00"),
                )
        self.assertEqual(
            ClimateLog.objects.filter(zone=self.zone, recorded_at=self.t1).count(), 1
        )


class DirtyClimateDataTests(TransactionTestCase):
    """Legacy duplicate rows created BEFORE the unique constraint existed."""

    def setUp(self):
        # Roll back to the schema before the unique constraint was added, so
        # duplicate (zone, recorded_at) rows can actually be inserted.
        call_command("migrate", "core", "0001", verbosity=0)
        self.user = User.objects.create_user(username="legacy", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        _, self.zone = make_greenhouse_zone()
        self.t1 = timezone.now().replace(microsecond=0)

    def tearDown(self):
        # Remove dirty rows, then restore the current schema for other tests.
        ClimateLog.objects.all().delete()
        call_command("migrate", "core", "0002", verbosity=0)
        super().tearDown()

    def test_list_still_returns_rows_when_dirty_duplicates_exist(self):
        ClimateLog.objects.create(
            zone=self.zone, recorded_at=self.t1,
            temp_c=Decimal("24.00"), humidity_pct=Decimal("60.00"),
        )
        ClimateLog.objects.create(
            zone=self.zone, recorded_at=self.t1,
            temp_c=Decimal("24.00"), humidity_pct=Decimal("61.00"),
        )

        resp = self.client.get(reverse("climate-log-list"), {"zoneId": self.zone.id})
        self.assertEqual(resp.status_code, 200)
        results = resp.data["results"] if "results" in resp.data else resp.data
        self.assertEqual(len(results), 2)


class ClimateLogParallelSubmitTests(TransactionTestCase):
    """Two simultaneous POSTs for the same (zone, recorded_at).

    Uses a file-based SQLite test database so two real DB connections contend
    on the unique index; the loser must get 409 and only one row may survive.
    """

    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(username="racer", password="x")
        _, self.zone = make_greenhouse_zone()
        self.recorded_at = timezone.now().replace(microsecond=0)

    def test_parallel_posts_leave_exactly_one_row(self):
        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(name, humidity):
            client = APIClient()
            client.force_authenticate(User.objects.get(username="racer"))
            payload = climate_payload(self.zone, self.recorded_at, humidity)
            barrier.wait()
            # Bypass the application-level clash pre-check so both requests
            # reach INSERT with no row visible yet -- the actual race the DB
            # unique constraint exists to settle.
            with patch.object(
                ClimateLogSerializer, "validate", lambda self_, attrs: attrs
            ):
                resp = client.post(reverse("climate-log-list"), payload, format="json")
            outcomes[name] = resp.status_code

        threads = [
            threading.Thread(target=worker, args=("a", "60.00")),
            threading.Thread(target=worker, args=("b", "60.00")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(sorted(outcomes.values()), [201, 409])
        rows = ClimateLog.objects.filter(zone=self.zone, recorded_at=self.recorded_at)
        self.assertEqual(rows.count(), 1)


class DedupeMigrationTests(TransactionTestCase):
    def test_migration_dedupes_then_enforces_uniqueness(self):
        migration = importlib.import_module(
            "core.migrations.0002_climatelog_unique_zone_recorded_at"
        )
        # Unconstrained legacy schema.
        call_command("migrate", "core", "0001", verbosity=0)
        try:
            _, zone1 = make_greenhouse_zone(code="A-01")
            _, zone2 = make_greenhouse_zone(code="A-02")
            t1 = timezone.now().replace(microsecond=0)
            t2 = t1 - timedelta(hours=1)

            first = ClimateLog.objects.create(
                zone=zone1, recorded_at=t1, temp_c=Decimal("20.00"), humidity_pct=Decimal("60.00")
            )
            ClimateLog.objects.create(
                zone=zone1, recorded_at=t1, temp_c=Decimal("21.00"), humidity_pct=Decimal("61.00")
            )
            ClimateLog.objects.create(
                zone=zone1, recorded_at=t1, temp_c=Decimal("22.00"), humidity_pct=Decimal("62.00")
            )
            other_zone_first = ClimateLog.objects.create(
                zone=zone2, recorded_at=t2, temp_c=Decimal("20.00"), humidity_pct=Decimal("50.00")
            )
            ClimateLog.objects.create(
                zone=zone2, recorded_at=t2, temp_c=Decimal("21.00"), humidity_pct=Decimal("51.00")
            )

            # First the data cleanup alone: keep the earliest row per pair.
            migration.dedupe_climate_logs(django_apps, None)
            self.assertEqual(ClimateLog.objects.filter(zone=zone1, recorded_at=t1).count(), 1)
            self.assertEqual(ClimateLog.objects.filter(zone=zone2, recorded_at=t2).count(), 1)
            self.assertTrue(ClimateLog.objects.filter(pk=first.id).exists())
            self.assertTrue(ClimateLog.objects.filter(pk=other_zone_first.id).exists())

            # Then the full migration forward must apply the constraint cleanly.
            call_command("migrate", "core", "0002", verbosity=0)
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ClimateLog.objects.create(
                        zone=zone1, recorded_at=t1,
                        temp_c=Decimal("23.00"), humidity_pct=Decimal("63.00"),
                    )
        finally:
            # Always leave the database at the latest migration state.
            call_command("migrate", "core", verbosity=0)
