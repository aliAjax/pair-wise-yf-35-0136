from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_athlete_retire(actor, entity, data, lookup):
    pending = _find_many(lookup, "passport_reading", "athlete_id", entity["id"])
    if any(item["status"] == "pending_review" for item in pending):
        raise ValidationError(
            "athlete has passport readings pending review and cannot retire"
        )
    return {"retired_by": actor.user_id}


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_passport_reading(actor, data, lookup):
    if not str(data.get("marker", "")).strip():
        raise ValidationError("marker is required")
    value = data.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValidationError("value must be a positive number")
    if not str(data.get("unit", "")).strip():
        raise ValidationError("unit is required")


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_confirm_passport(actor, entity, data, lookup):
    reading_id = data.get("reading_id")
    reading = _find_one(lookup, "passport_reading", "id", reading_id)
    if not reading:
        raise ValidationError("reading_id must reference a passport reading")
    if reading["status"] != "confirmed":
        raise ValidationError("only a confirmed passport reading can turn a sample adverse")
    if reading["data"].get("sample_id") != entity["id"]:
        raise ValidationError("passport reading belongs to a different sample")
    return {"confirmed_by": actor.user_id, "reading_id": reading_id}


def _validate_reading_review(actor, entity, data, lookup):
    rationale = str(data.get("rationale", "")).strip()
    if not rationale:
        raise ValidationError("rationale is required")
    return {"reviewed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case, 'passport_reading': _validate_passport_reading}
CUSTOM_TRANSITIONS = {('sample', 'report_adverse'): _validate_report_adverse, ('sample', 'confirm_passport'): _validate_confirm_passport, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('athlete', 'retire'): _validate_athlete_retire, ('passport_reading', 'dismiss'): _validate_reading_review, ('passport_reading', 'confirm'): _validate_reading_review}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'passport_readings': 'passport_reading'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'passport_reading': 'recorded'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received',), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'confirm_passport': (('analyzed', 'cleared'), 'adverse'), 'clear': (('analyzed',), 'cleared')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')}, 'passport_reading': {'dismiss': (('pending_review',), 'dismissed'), 'confirm': (('pending_review',), 'confirmed')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'sample_id', 'alleged_rule'), 'passport_reading': ('sample_id', 'marker', 'value', 'unit')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'confirm_passport'): ('reading_id',), ('sample', 'clear'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',), ('passport_reading', 'dismiss'): ('rationale',), ('passport_reading', 'confirm'): ('rationale',)}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel'), 'passport_reading': ('admin', 'lab')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'confirm_passport': ('admin', 'panel'), 'clear': ('admin', 'lab'), 'provisional_suspend': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel'), 'dismiss': ('admin', 'panel'), 'confirm': ('admin', 'panel')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _find_many(lookup, kind, field, value):
    if lookup is None:
        return []
    return lookup(kind, field, value) or []


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
