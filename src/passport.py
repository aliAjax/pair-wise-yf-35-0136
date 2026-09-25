import math
from uuid import uuid4

from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import DEVIATION_THRESHOLD, passport_deviation


class PassportService:
    """Biological passport orchestration.

    Readings are recorded as `reading` entities when a sample is analyzed;
    the rule engine guards the transitions, this service keeps the ledger
    and propagates confirmed violations back to the sample.
    """

    def __init__(self, repository, audit):
        self.repository = repository
        self.audit = audit

    def after_transition(self, actor, entity, action):
        if entity["kind"] == "sample" and action == "analyze":
            self.record_reading(actor, entity)
        elif entity["kind"] == "reading" and action == "confirm":
            self.mark_sample_adverse(actor, entity)

    def series(self, athlete_id, marker):
        rows = [
            row
            for row in self.repository.find_entities("reading", "athlete_id", athlete_id)
            if row["data"].get("marker") == marker
        ]
        return sorted(
            rows,
            key=lambda row: (
                str(row["data"].get("sampled_at", "")),
                row["created_at"],
                row["id"],
            ),
        )

    def record_reading(self, actor, sample):
        data = sample["data"]
        marker = data.get("marker")
        if marker in (None, ""):
            return None
        for existing in self.repository.find_entities("reading", "sample_id", sample["id"]):
            if existing["data"].get("marker") == marker:
                return existing  # repeat analysis is not recorded twice
        value = float(data.get("value"))
        previous_values = [
            float(row["data"]["value"])
            for row in self.series(data.get("athlete_id"), marker)
        ]
        deviation = passport_deviation(previous_values, value)
        anomaly = deviation is not None and deviation > DEVIATION_THRESHOLD
        payload = {
            "athlete_id": data.get("athlete_id"),
            "sample_id": sample["id"],
            "marker": marker,
            "value": value,
            "unit": data.get("unit"),
            "sampled_at": data.get("sampled_at") or data.get("collected_at") or utcnow(),
            "anomaly": anomaly,
        }
        if len(previous_values) >= 2:
            payload["baseline_avg"] = round(
                (previous_values[-1] + previous_values[-2]) / 2.0, 6
            )
        if deviation is not None and math.isfinite(deviation):
            payload["deviation"] = round(deviation, 6)
        status = "pending_review" if anomaly else "normal"
        reading = self.repository.create_entity(
            str(uuid4()), "reading", status, payload, actor.user_id
        )
        self.audit.record(
            reading["id"],
            actor,
            "record_reading",
            None,
            status,
            {
                "kind": "reading",
                "sample_id": sample["id"],
                "marker": marker,
                "anomaly": anomaly,
            },
        )
        return reading

    def mark_sample_adverse(self, actor, reading):
        sample_id = reading["data"].get("sample_id")
        sample = self.repository.get_entity(sample_id) if sample_id else None
        if not sample:
            raise NotFoundError("linked sample not found: " + str(sample_id))
        if sample["status"] == "adverse":
            return sample
        if sample["status"] != "analyzed":
            raise ConflictError(
                "linked sample cannot turn adverse from status " + sample["status"]
            )
        patch = {
            "result": "adverse",
            "passport_reading_id": reading["id"],
            "confirmed_by": actor.user_id,
        }
        merged = dict(sample["data"])
        merged.update(patch)
        updated = self.repository.update_entity(
            sample["id"], sample["version"], "adverse", merged
        )
        self.audit.record(
            sample["id"],
            actor,
            "report_adverse",
            sample["status"],
            "adverse",
            {"patch": patch, "source": "passport_confirm"},
        )
        return updated
