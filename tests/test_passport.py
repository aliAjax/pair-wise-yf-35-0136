import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine, passport_deviation
from src.service import DomainService


class PassportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")
        self.panel = Actor("panel-1", "panel")
        self.athlete = self.service.create(
            self.admin, "athlete", {"name": "A. Rider", "discipline": "cycling"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _received_sample(self, code, collected_at):
        sample = self.service.create(
            self.admin,
            "sample",
            {
                "athlete_id": self.athlete["id"],
                "sample_code": code,
                "event": "race-" + code,
            },
        )
        self.service.transition(
            self.admin, sample["id"], "collect", {"collected_at": collected_at}
        )
        self.service.transition(self.admin, sample["id"], "seal", {"seal_id": "SEAL-" + code})
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "Courier-A"})
        self.service.transition(self.lab, sample["id"], "receive", {"lab_id": "LAB-1"})
        return sample

    def _analyzed_sample(
        self, code, collected_at, marker="hemoglobin", value=14.0, unit="g/dL"
    ):
        sample = self._received_sample(code, collected_at)
        data = {"result": "negative"}
        if marker is not None:
            data.update({"marker": marker, "value": value, "unit": unit})
        self.service.transition(self.lab, sample["id"], "analyze", data)
        return sample

    def _make_pending(self):
        self._analyzed_sample("S-1", "2024-03-01T08:00:00Z", value=14.0)
        self._analyzed_sample("S-2", "2025-03-01T08:00:00Z", value=14.0)
        # mean of the previous two is 14.0; 16.0 deviates by ~14.3%
        self._analyzed_sample("S-3", "2026-03-01T08:00:00Z", value=16.0)
        pending = self.service.list("reading", status="pending_review")
        assert len(pending) == 1
        return pending[0]

    def _reading_by_value(self, value):
        for reading in self.service.list("reading"):
            if reading["data"].get("value") == value:
                return reading
        self.fail("no reading with value %r" % (value,))

    def test_analyze_records_passport_reading(self):
        sample = self._analyzed_sample("S-1", "2026-01-10T08:00:00Z", value=14.2)
        readings = self.service.list("reading")
        self.assertEqual(len(readings), 1)
        reading = readings[0]
        self.assertEqual(reading["status"], "normal")
        self.assertEqual(reading["data"]["athlete_id"], self.athlete["id"])
        self.assertEqual(reading["data"]["sample_id"], sample["id"])
        self.assertEqual(reading["data"]["marker"], "hemoglobin")
        self.assertEqual(reading["data"]["value"], 14.2)
        self.assertEqual(reading["data"]["unit"], "g/dL")
        self.assertEqual(reading["data"]["sampled_at"], "2026-01-10T08:00:00Z")
        self.assertFalse(reading["data"]["anomaly"])

    def test_analyze_without_marker_records_nothing(self):
        self._analyzed_sample("S-1", "2026-01-10T08:00:00Z", marker=None)
        self.assertEqual(self.service.list("reading"), [])

    def test_marker_value_unit_must_come_together(self):
        sample = self._received_sample("S-1", "2026-01-10T08:00:00Z")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab, sample["id"], "analyze", {"result": "negative", "marker": "hemoglobin"}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab,
                sample["id"],
                "analyze",
                {"result": "negative", "marker": "hemoglobin", "value": "not-a-number", "unit": "g/dL"},
            )

    def test_series_is_ordered_by_sample_time(self):
        self._analyzed_sample("S-1", "2026-01-01T08:00:00Z", value=14.0)
        self._analyzed_sample("S-2", "2024-01-01T08:00:00Z", value=13.5)
        series = self.service.passport.series(self.athlete["id"], "hemoglobin")
        self.assertEqual([row["data"]["value"] for row in series], [13.5, 14.0])

    def test_sampled_at_can_be_overridden(self):
        sample = self._received_sample("S-1", "2026-01-01T08:00:00Z")
        self.service.transition(
            self.lab,
            sample["id"],
            "analyze",
            {
                "result": "negative",
                "marker": "hemoglobin",
                "value": 14.0,
                "unit": "g/dL",
                "sampled_at": "2025-12-31T23:00:00Z",
            },
        )
        reading = self.service.list("reading")[0]
        self.assertEqual(reading["data"]["sampled_at"], "2025-12-31T23:00:00Z")

    def test_repeat_analysis_is_not_recorded_twice(self):
        sample = self._analyzed_sample("S-1", "2026-01-10T08:00:00Z", value=14.2)
        self.service.transition(
            self.lab,
            sample["id"],
            "analyze",
            {"result": "negative", "marker": "hemoglobin", "value": 14.2, "unit": "g/dL"},
        )
        self.assertEqual(len(self.service.list("reading")), 1)

    def test_third_reading_beyond_ten_percent_is_pending(self):
        self._analyzed_sample("S-1", "2024-03-01T08:00:00Z", value=14.0)
        self._analyzed_sample("S-2", "2025-03-01T08:00:00Z", value=14.0)
        # +7.1% against the 14.0 baseline: still normal
        self._analyzed_sample("S-3", "2026-03-01T08:00:00Z", value=15.0)
        self.assertEqual(self._reading_by_value(15.0)["status"], "normal")
        # baseline is now mean(14.0, 15.0) = 14.5; 16.6 deviates by ~14.5%
        self._analyzed_sample("S-4", "2026-06-01T08:00:00Z", value=16.6)
        reading = self._reading_by_value(16.6)
        self.assertEqual(reading["status"], "pending_review")
        self.assertTrue(reading["data"]["anomaly"])
        self.assertAlmostEqual(reading["data"]["baseline_avg"], 14.5)
        self.assertGreater(reading["data"]["deviation"], 0.10)

    def test_exactly_ten_percent_is_not_an_anomaly(self):
        self._analyzed_sample("S-1", "2024-01-01T08:00:00Z", marker="reticulocyte", value=100.0, unit="%")
        self._analyzed_sample("S-2", "2025-01-01T08:00:00Z", marker="reticulocyte", value=100.0, unit="%")
        self._analyzed_sample("S-3", "2026-01-01T08:00:00Z", marker="reticulocyte", value=110.0, unit="%")
        self.assertEqual(self._reading_by_value(110.0)["status"], "normal")

    def test_pending_review_pauses_new_readings_for_marker(self):
        self._make_pending()
        sample = self._received_sample("S-4", "2026-07-01T08:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.lab,
                sample["id"],
                "analyze",
                {"result": "negative", "marker": "hemoglobin", "value": 14.1, "unit": "g/dL"},
            )
        # other markers of the same athlete are still accepted
        self.service.transition(
            self.lab,
            sample["id"],
            "analyze",
            {"result": "negative", "marker": "reticulocyte", "value": 0.9, "unit": "%"},
        )
        self.assertEqual(len(self.service.list("reading", status="normal")), 3)

    def test_pending_review_blocks_retire_until_released(self):
        self._make_pending()
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, self.athlete["id"], "retire", {})
        reading = self.service.list("reading", status="pending_review")[0]
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.lab, reading["id"], "release", {"reason": "not our call"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.panel, reading["id"], "release", {})
        updated = self.service.transition(
            self.panel, reading["id"], "release", {"reason": "explained by altitude camp"}
        )
        self.assertEqual(updated["status"], "released")
        self.assertEqual(updated["data"]["released_by"], "panel-1")
        # anomaly history is preserved after the release
        self.assertTrue(updated["data"]["anomaly"])
        self.assertIn("deviation", updated["data"])
        retired = self.service.transition(self.admin, self.athlete["id"], "retire", {})
        self.assertEqual(retired["status"], "retired")

    def test_confirm_marks_sample_adverse_and_opens_case(self):
        self._make_pending()
        reading = self.service.list("reading", status="pending_review")[0]
        updated = self.service.transition(
            self.panel, reading["id"], "confirm", {"reason": "blood doping pattern"}
        )
        self.assertEqual(updated["status"], "confirmed")
        self.assertEqual(updated["data"]["confirmed_by"], "panel-1")
        sample = self.service.get(reading["data"]["sample_id"])
        self.assertEqual(sample["status"], "adverse")
        self.assertEqual(sample["data"]["result"], "adverse")
        self.assertEqual(sample["data"]["passport_reading_id"], reading["id"])
        case = self.service.create(
            self.panel,
            "case",
            {
                "athlete_id": self.athlete["id"],
                "sample_id": sample["id"],
                "alleged_rule": "passport-violation",
            },
        )
        self.assertEqual(case["status"], "open")

    def test_only_pending_readings_can_be_reviewed(self):
        self._analyzed_sample("S-1", "2026-01-01T08:00:00Z", value=14.0)
        reading = self.service.list("reading")[0]
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.panel, reading["id"], "confirm", {"reason": "x"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.panel, reading["id"], "release", {"reason": "x"})

    def test_list_filters_pending_review(self):
        self._make_pending()
        self.assertEqual(len(self.service.list("reading", status="pending_review")), 1)
        self.assertEqual(len(self.service.list("reading", status="normal")), 2)
        self.assertEqual(len(self.service.list("readings", status="pending_review")), 1)

    def test_direct_reading_creation_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "reading",
                {
                    "athlete_id": self.athlete["id"],
                    "marker": "hemoglobin",
                    "value": 14.0,
                    "unit": "g/dL",
                },
            )

    def test_passport_deviation(self):
        self.assertIsNone(passport_deviation([], 14.0))
        self.assertIsNone(passport_deviation([14.0], 14.0))
        self.assertAlmostEqual(passport_deviation([14.0, 14.0], 15.4), 0.1)
        self.assertEqual(passport_deviation([0.0, 0.0], 0.0), 0.0)
        self.assertEqual(passport_deviation([0.0, 0.0], 1.0), float("inf"))


if __name__ == "__main__":
    unittest.main()
