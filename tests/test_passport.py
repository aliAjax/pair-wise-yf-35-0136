import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _ready_sample(service, athlete_id, code, collected_at):
    """Create a sample and drive it through to analyzed."""
    admin = Actor("admin", "admin")
    sample = service.create(
        admin,
        "sample",
        {"athlete_id": athlete_id, "sample_code": code, "event": "national-final"},
    )
    service.transition(admin, sample["id"], "collect", {"collected_at": collected_at})
    service.transition(admin, sample["id"], "seal", {"seal_id": "SEAL-" + code})
    service.transition(admin, sample["id"], "ship", {"carrier": "Courier-A"})
    service.transition(admin, sample["id"], "receive", {"lab_id": "LAB-1"})
    service.transition(admin, sample["id"], "analyze", {"result": "negative"})
    return service.get(sample["id"])


class PassportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")
        self.panel = Actor("panel-1", "panel")
        self.viewer = Actor("viewer-1", "viewer")
        self.athlete = self.service.create(
            self.admin,
            "athlete",
            {"name": "A. Rider", "discipline": "cycling"},
        )["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _reading(self, sample_id, value, marker="HGB", unit="g/L", actor=None):
        return self.service.create(
            actor or self.lab,
            "passport_reading",
            {"sample_id": sample_id, "marker": marker, "value": value, "unit": unit},
        )

    def test_first_two_readings_recorded(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        first = self._reading(sample_1["id"], 140)
        second = self._reading(sample_2["id"], 142)
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "recorded")

    def test_third_reading_within_ten_percent_is_recorded(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        third = self._reading(sample_3["id"], 148)
        self.assertEqual(third["status"], "recorded")
        self.assertAlmostEqual(third["data"]["baseline"], 141.0)
        self.assertAlmostEqual(third["data"]["deviation_ratio"], 7 / 141, places=5)

    def test_third_reading_over_ten_percent_pending_review(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        third = self._reading(sample_3["id"], 170)
        # baseline (140+142)/2 = 141, deviation > 10%
        self.assertEqual(third["status"], "pending_review")
        self.assertGreater(third["data"]["deviation_ratio"], 0.10)

    def test_exactly_ten_percent_is_not_flagged(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 140)
        third = self._reading(sample_3["id"], 154)
        self.assertEqual(third["status"], "recorded")

    def test_negative_deviation_over_ten_percent_is_flagged(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 150)
        self._reading(sample_2["id"], 150)
        third = self._reading(sample_3["id"], 130)
        self.assertEqual(third["status"], "pending_review")

    def test_series_uses_latest_two_prior_readings(self):
        dates = [
            "2024-11-01T08:00:00",
            "2024-12-01T08:00:00",
            "2025-01-01T08:00:00",
            "2025-02-01T08:00:00",
        ]
        samples = [
            _ready_sample(self.service, self.athlete, "S-%03d" % i, dates[i - 1])
            for i in range(1, 5)
        ]
        self._reading(samples[0]["id"], 140)
        self._reading(samples[1]["id"], 142)
        self._reading(samples[2]["id"], 144)
        # 4th compared against readings 2 and 3: (142+144)/2 = 143
        fourth = self._reading(samples[3]["id"], 145)
        self.assertEqual(fourth["status"], "recorded")
        self.assertAlmostEqual(fourth["data"]["baseline"], 143.0)

    def test_series_spans_years_but_ordered_by_sampling_time(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2024-11-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 170)
        # A late-registered sample from the previous year is placed by sampling time.
        sample_late = _ready_sample(self.service, self.athlete, "S-LATE", "2024-12-01T08:00:00")
        late = self._reading(sample_late["id"], 142)
        self.assertEqual(late["status"], "recorded")  # only one earlier reading exists
        sample_4 = _ready_sample(self.service, self.athlete, "S-004", "2025-04-01T08:00:00")
        newest = self._reading(sample_4["id"], 145)
        # Two chronological predecessors of 2025-04 are the 2024-12 (142) and 2025-02 (170).
        self.assertAlmostEqual(newest["data"]["baseline"], (142 + 170) / 2)
        self.assertAlmostEqual(newest["data"]["deviation_ratio"], 11 / 156, places=5)
        self.assertEqual(newest["status"], "recorded")

    def test_repeated_analysis_is_not_booked_twice(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        first = self._reading(sample_1["id"], 140)
        retry = self._reading(sample_1["id"], 140)
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(len(self.service.list("passport_readings")), 1)

    def test_retry_stays_idempotent_while_pending(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        pending = self._reading(sample_3["id"], 170)
        # Re-sending the same analyzed sample still returns the pending reading.
        retry = self._reading(sample_3["id"], 170)
        self.assertEqual(retry["id"], pending["id"])

    def test_pending_review_blocks_new_reading_for_same_marker(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        sample_4 = _ready_sample(self.service, self.athlete, "S-004", "2025-04-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        self._reading(sample_3["id"], 170)
        with self.assertRaises(ValidationError):
            self._reading(sample_4["id"], 145)

    def test_pending_review_does_not_block_other_marker(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140, marker="HGB")
        self._reading(sample_2["id"], 142, marker="HGB")
        self._reading(sample_3["id"], 170, marker="HGB")
        retic = self._reading(sample_1["id"], 1.0, marker="RET", unit="%")
        self.assertEqual(retic["status"], "recorded")

    def test_unit_mismatch_rejected(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        self._reading(sample_1["id"], 140, unit="g/L")
        with self.assertRaises(ValidationError):
            self._reading(sample_2["id"], 14.0, unit="g/dL")

    def test_pending_review_blocks_athlete_retirement(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        self._reading(sample_3["id"], 170)
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, self.athlete, "retire", {"reason": "done"})

    def test_dismiss_resumes_readings_and_history_remains(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        sample_4 = _ready_sample(self.service, self.athlete, "S-004", "2025-04-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        pending = self._reading(sample_3["id"], 170)
        dismissed = self.service.transition(
            self.panel,
            pending["id"],
            "dismiss",
            {"rationale": "altitude training camp explains the rise"},
        )
        self.assertEqual(dismissed["status"], "dismissed")
        self.assertEqual(dismissed["data"]["reviewed_by"], "panel-1")
        # Anomaly history is preserved with its original baseline/deviation.
        self.assertGreater(dismissed["data"]["deviation_ratio"], 0.10)
        # Retired is now allowed.
        retired = self.service.transition(self.admin, self.athlete, "retire", {"reason": "done"})
        self.assertEqual(retired["status"], "retired")
        # New readings resume and include the dismissed point in the series.
        resumed = self._reading(sample_4["id"], 146)
        self.assertEqual(resumed["status"], "recorded")
        self.assertAlmostEqual(resumed["data"]["baseline"], (142 + 170) / 2)

    def test_confirm_turns_sample_adverse_and_case_can_open(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        pending = self._reading(sample_3["id"], 170)
        confirmed = self.service.transition(
            self.panel,
            pending["id"],
            "confirm",
            {"rationale": "ABP atypical profile confirmed"},
        )
        self.assertEqual(confirmed["status"], "confirmed")
        sample = self.service.get(sample_3["id"])
        self.assertEqual(sample["status"], "adverse")
        self.assertEqual(sample["data"]["reading_id"], pending["id"])
        case = self.service.create(
            self.admin,
            "case",
            {
                "athlete_id": self.athlete,
                "sample_id": sample_3["id"],
                "alleged_rule": "abp-blood",
            },
        )
        self.assertEqual(case["status"], "open")

    def test_confirm_promotes_cleared_sample(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        pending = self._reading(sample_3["id"], 170)
        # Lab clears the sample per-sample before the panel finishes review.
        self.service.transition(
            self.lab, sample_3["id"], "clear", {"reason": "no direct adverse finding"}
        )
        self.assertEqual(self.service.get(sample_3["id"])["status"], "cleared")
        self.service.transition(
            self.panel, pending["id"], "confirm", {"rationale": "longitudinal profile violation"}
        )
        self.assertEqual(self.service.get(sample_3["id"])["status"], "adverse")

    def test_list_filters_pending_review(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        pending = self._reading(sample_3["id"], 170)
        items = self.service.list("passport_readings", status="pending_review")
        self.assertEqual([item["id"] for item in items], [pending["id"]])

    def test_only_panel_can_review(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        sample_2 = _ready_sample(self.service, self.athlete, "S-002", "2025-02-01T08:00:00")
        sample_3 = _ready_sample(self.service, self.athlete, "S-003", "2025-03-01T08:00:00")
        self._reading(sample_1["id"], 140)
        self._reading(sample_2["id"], 142)
        pending = self._reading(sample_3["id"], 170)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.lab, pending["id"], "confirm", {"rationale": "lab cannot self-review"}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.viewer, pending["id"], "dismiss", {"rationale": "viewer cannot review"}
            )

    def test_only_lab_registers_readings(self):
        sample_1 = _ready_sample(self.service, self.athlete, "S-001", "2025-01-01T08:00:00")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.viewer,
                "passport_reading",
                {"sample_id": sample_1["id"], "marker": "HGB", "value": 140, "unit": "g/L"},
            )

    def test_reading_requires_analyzed_sample(self):
        sample = self.service.create(
            self.admin,
            "sample",
            {"athlete_id": self.athlete, "sample_code": "S-EARLY", "event": "event"},
        )
        with self.assertRaises(ValidationError):
            self._reading(sample["id"], 140)

    def test_readings_embedded_in_analyze_action(self):
        sample = self.service.create(
            self.admin,
            "sample",
            {"athlete_id": self.athlete, "sample_code": "S-EMB", "event": "event"},
        )
        self.service.transition(self.admin, sample["id"], "collect", {"collected_at": "2025-05-01T08:00:00"})
        self.service.transition(self.admin, sample["id"], "seal", {"seal_id": "SEAL-EMB"})
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "C"})
        self.service.transition(self.admin, sample["id"], "receive", {"lab_id": "LAB-1"})
        analyzed = self.service.transition(
            self.lab,
            sample["id"],
            "analyze",
            {
                "result": "negative",
                "readings": [{"marker": "HGB", "value": 141, "unit": "g/L"}],
            },
        )
        self.assertEqual(analyzed["status"], "analyzed")
        readings = self.service.list("passport_readings")
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["data"]["sample_id"], sample["id"])
        self.assertEqual(readings[0]["data"]["collected_at"], "2025-05-01T08:00:00")

    def test_embedded_reading_then_standalone_is_idempotent(self):
        sample = self.service.create(
            self.admin,
            "sample",
            {"athlete_id": self.athlete, "sample_code": "S-IDEM", "event": "event"},
        )
        self.service.transition(self.admin, sample["id"], "collect", {"collected_at": "2025-06-01T08:00:00"})
        self.service.transition(self.admin, sample["id"], "seal", {"seal_id": "SEAL-IDEM"})
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "C"})
        self.service.transition(self.admin, sample["id"], "receive", {"lab_id": "LAB-1"})
        self.service.transition(
            self.lab,
            sample["id"],
            "analyze",
            {"result": "negative", "readings": [{"marker": "HGB", "value": 140, "unit": "g/L"}]},
        )
        embedded = self.service.list("passport_readings")
        self.assertEqual(len(embedded), 1)
        # Lab re-submits the same sample/marker through the readings endpoint.
        again = self._reading(sample["id"], 140)
        self.assertEqual(again["id"], embedded[0]["id"])
        self.assertEqual(len(self.service.list("passport_readings")), 1)


if __name__ == "__main__":
    unittest.main()
