from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine

REVIEW_THRESHOLD = 0.10


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        if kind == "passport_reading":
            entity = self._register_reading(actor, payload)
        else:
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind)
            entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
            self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        payload = dict(data or {})
        embedded_readings = None
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "sample" and action == "analyze":
            embedded_readings = payload.pop("readings", None)
            if embedded_readings is not None and not isinstance(embedded_readings, list):
                raise ValidationError("readings must be a list")
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        if embedded_readings is not None:
            for reading in embedded_readings:
                reading_payload = dict(reading)
                reading_payload.setdefault("sample_id", entity_id)
                self.rules.validate_create(
                    actor,
                    "passport_reading",
                    reading_payload,
                    self._lookup,
                )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if embedded_readings is not None:
            for reading in embedded_readings:
                self._register_reading(actor, dict(reading), sample_id=entity_id)
        if kind == "passport_reading" and action == "confirm":
            self._promote_sample_from_reading(actor, updated)
        return updated

    def _marker_series(self, athlete_id, marker):
        """Same athlete + marker ordered by sampling time, then registration order."""
        readings = self._lookup("passport_reading", "athlete_id", athlete_id)
        readings = [item for item in readings if item["data"].get("marker") == marker]
        return sorted(
            readings,
            key=lambda item: (
                item["data"].get("collected_at") or "",
                item["created_at"],
                item["id"],
            ),
        )

    def _register_reading(self, actor, payload, sample_id=None):
        """Register a passport reading for an analyzed sample.

        Repeated analysis of the same sample/marker is idempotent: the existing
        reading is returned instead of being booked twice.
        """
        sample_ref = sample_id or payload.get("sample_id")
        sample = self.repository.get_entity(sample_ref) if sample_ref else None
        if not sample or self.rules.normalize_kind(sample["kind"]) != "sample":
            raise ValidationError("sample_id must reference an existing sample")
        if sample["status"] not in ("analyzed", "adverse", "cleared"):
            raise ValidationError("sample must be analyzed before a reading is registered")
        athlete_id = sample["data"].get("athlete_id")
        marker = str(payload.get("marker", "")).strip()
        unit = str(payload.get("unit", "")).strip()
        value = payload.get("value")

        series = self._marker_series(athlete_id, marker)

        # Repeated analysis must never be booked twice (checked before the
        # pending gate so retries stay idempotent).
        duplicate = next(
            (item for item in series if item["data"].get("sample_id") == sample["id"]),
            None,
        )
        if duplicate is not None:
            return duplicate

        if any(item["status"] == "pending_review" for item in series):
            raise ValidationError(
                "marker %s has an unreviewed reading; new readings are paused" % marker
            )
        if series and any(item["data"].get("unit") != unit for item in series):
            raise ValidationError("unit for marker %s is inconsistent with prior readings" % marker)

        collected_at = sample["data"].get("collected_at")
        if not collected_at:
            raise ValidationError("sample has no collected_at to place the reading in the series")

        prior = [item for item in series if (item["data"].get("collected_at") or "") <= collected_at]
        status = self.rules.initial_status("passport_reading")
        detail = {}
        if len(prior) >= 2:
            baseline = sum(float(item["data"].get("value")) for item in prior[-2:]) / 2.0
            deviation = abs(float(value) - baseline) / baseline
            detail["baseline"] = baseline
            detail["deviation_ratio"] = round(deviation, 6)
            if deviation > REVIEW_THRESHOLD:
                status = "pending_review"

        reading_data = {
            "sample_id": sample["id"],
            "sample_code": sample["data"].get("sample_code"),
            "athlete_id": athlete_id,
            "marker": marker,
            "value": float(value),
            "unit": unit,
            "collected_at": collected_at,
        }
        reading_data.update(detail)
        entity_id = str(payload.get("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        reading = self.repository.create_entity(
            entity_id, "passport_reading", status, reading_data, actor.user_id
        )
        self.audit.record(
            entity_id,
            actor,
            "create",
            None,
            status,
            {"kind": "passport_reading", "marker": marker},
        )
        return reading

    def _promote_sample_from_reading(self, actor, reading):
        """A confirmed violation turns the sample positive so a case can open."""
        sample = self.repository.get_entity(reading["data"].get("sample_id"))
        if not sample or sample["status"] == "adverse":
            return
        merged = dict(sample["data"])
        merged.update(
            {
                "result": "adverse",
                "confirmed_by": actor.user_id,
                "reading_id": reading["id"],
            }
        )
        self.repository.update_entity(sample["id"], sample["version"], "adverse", merged)
        self.audit.record(
            sample["id"],
            actor,
            "confirm_passport",
            sample["status"],
            "adverse",
            {"reading_id": reading["id"]},
        )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
