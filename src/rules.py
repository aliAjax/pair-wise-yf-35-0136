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


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


DEVIATION_THRESHOLD = 0.10


def passport_deviation(previous_values, value):
    """Deviation of a new reading from the mean of the previous two readings.

    Returns None while there is no baseline (fewer than two readings).
    """
    if len(previous_values) < 2:
        return None
    baseline = (previous_values[-1] + previous_values[-2]) / 2.0
    if baseline == 0:
        return 0.0 if value == 0 else float("inf")
    return abs(value - baseline) / abs(baseline)


def _validate_reading_create(actor, data, lookup):
    raise ValidationError("passport readings are recorded through sample analysis")


def _validate_sample_analyze(actor, entity, data, lookup):
    provided = [field for field in ("marker", "value", "unit") if data.get(field) not in (None, "")]
    if not provided:
        return None
    if len(provided) < 3:
        raise ValidationError("marker, value and unit must be provided together")
    try:
        float(data.get("value"))
    except (TypeError, ValueError):
        raise ValidationError("passport value must be numeric")
    if lookup is None:
        return None
    marker = data.get("marker")
    readings = lookup("reading", "athlete_id", entity["data"].get("athlete_id")) or []
    for reading in readings:
        if reading["data"].get("sample_id") == entity["id"] and reading["data"].get("marker") == marker:
            return None  # repeat analysis of the same sample records nothing new
    for reading in readings:
        if reading["data"].get("marker") == marker and reading["status"] == "pending_review":
            raise ConflictError("passport reading for marker %s is pending review" % marker)
    return None


def _validate_athlete_retire(actor, entity, data, lookup):
    if lookup is None:
        return None
    for reading in lookup("reading", "athlete_id", entity["id"]) or []:
        if reading["status"] == "pending_review":
            raise ConflictError("athlete has passport readings pending review")
    return None


def _validate_reading_release(actor, entity, data, lookup):
    return {"released_by": actor.user_id}


def _validate_reading_confirm(actor, entity, data, lookup):
    sample = _find_one(lookup, "sample", "id", entity["data"].get("sample_id"))
    if not sample:
        raise ValidationError("reading has no linked sample")
    if sample["status"] not in ("analyzed", "adverse"):
        raise ConflictError("linked sample cannot turn adverse from status " + sample["status"])
    return {"confirmed_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case, 'reading': _validate_reading_create}
CUSTOM_TRANSITIONS = {('athlete', 'retire'): _validate_athlete_retire, ('sample', 'analyze'): _validate_sample_analyze, ('sample', 'report_adverse'): _validate_report_adverse, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('reading', 'release'): _validate_reading_release, ('reading', 'confirm'): _validate_reading_confirm}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'readings': 'reading'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'reading': 'normal'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received', 'analyzed'), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')}, 'reading': {'release': (('pending_review',), 'released'), 'confirm': (('pending_review',), 'confirmed')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'sample_id', 'alleged_rule')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'clear'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',), ('reading', 'release'): ('reason',), ('reading', 'confirm'): ('reason',)}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'clear': ('admin', 'lab'), 'provisional_suspend': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel'), 'release': ('admin', 'panel'), 'confirm': ('admin', 'panel')}

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


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
