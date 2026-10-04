"""
Measurement correction order (测量更正单) workflow.

    submit (pending) -> review -> reviewed -> apply -> applied
                              \\-> rejected
    apply may land in 'failed' (interrupted, or blocked by an OPEN identity
    contradiction); it is retryable and can never produce more than one
    effective MeasurementRevision.

Hard guarantees implemented here
================================
1. The historical TreeMeasurement row is never modified. Applying appends
   a MeasurementRevision; estimators read the latest effective revision.
2. Idempotency: the same idempotency key always returns the SAME order.
3. Only one non-terminal order per measurement — mutually contradictory
   reviews on the same record therefore can never both be applied.
4. A reviewed order is re-checked at application time: the effective input
   must still equal the pre-review snapshot (another order didn't land in
   between) and the requested entry must still be valid/in-bounds.
5. Coordinate corrections that would sit inside an OPEN identity
   contradiction are blocked; a human must resolve it first.
6. Two-phase revision creation + crash recovery means an interrupted
   application leaves only a complete pending/failed state and retrying
   never creates a second effective revision.
7. Only NEW DRAFT EstimateVersions consume revisions. Confirmed editions
   keep their frozen payload, checksum and provenance forever.
"""
from django.core.exceptions import ValidationError as DjValidationError
from django.db import transaction
from django.utils import timezone

from inventory.models import (
    CORRECTION_APPLIED,
    CORRECTION_FAILED,
    CORRECTION_PENDING,
    CORRECTION_REJECTED,
    CORRECTION_REVIEWED,
    EstimateVersion,
    MeasurementCorrection,
    MeasurementRevision,
    REVISION_EFFECTIVE,
    REVISION_PENDING,
    REVISION_SUPERSEDED,
    VERSION_CONFIRMED,
)
from inventory.services.conflicts import correction_preflight, scan_conflicts
from inventory.services.revisions import (
    latest_effective_revision,
    reconcile_interrupted_applications,
)
from inventory.services.units import (
    convert_dbh_to_cm,
    convert_height_to_m,
    point_in_ring,
)


class CorrectionError(Exception):
    """Workflow rule violation; mapped to 4xx by the view layer."""

    def __init__(self, detail, status_code=400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


_TERMINAL_STATUSES = (CORRECTION_APPLIED, CORRECTION_REJECTED)
# Fields the estimator actually consumes, in effective-value form.
_EFFECTIVE_FIELDS = ("dbh_cm", "height_m", "x_m", "y_m", "status")


# ----------------------------------------------------------------- helpers
def _effective_state(measurement):
    """The values calculations currently see for the base measurement."""
    rev = latest_effective_revision(measurement.id)
    if rev is None:
        return {
            "dbh_cm": measurement.dbh_cm,
            "height_m": measurement.height_m,
            "x_m": measurement.x_m,
            "y_m": measurement.y_m,
            "status": measurement.status,
        }
    return {f: getattr(rev, f) for f in _EFFECTIVE_FIELDS}


def _normalize_corrected(measurement, data):
    """
    Build the complete corrected entry. Callers may omit a field, in which
    case the CURRENT effective value is retained. Units are mandatory with
    every raw value and re-converted to canonical units here.
    """
    eff = _effective_state(measurement)

    status_ = data.get("corrected_status") or eff["status"]
    x_m = data.get("corrected_x_m", eff["x_m"])
    y_m = data.get("corrected_y_m", eff["y_m"])
    if x_m is None:
        x_m = eff["x_m"]
    if y_m is None:
        y_m = eff["y_m"]

    dbh_raw = data.get("corrected_dbh_raw", ...)
    dbh_unit = data.get("corrected_dbh_unit", ...)
    if dbh_raw is ... and dbh_unit is ...:
        dbh_raw, dbh_unit, dbh_cm = (measurement.dbh_raw,
                                     measurement.dbh_unit, eff["dbh_cm"])
    else:
        if dbh_raw in (..., None) or dbh_unit in (..., None):
            raise CorrectionError(
                "dbh correction requires both corrected_dbh_raw and "
                "corrected_dbh_unit — the unit declaration is mandatory.")
        try:
            dbh_cm = convert_dbh_to_cm(dbh_raw, dbh_unit)
        except DjValidationError as exc:
            raise CorrectionError("; ".join(exc.messages))

    height_raw = data.get("corrected_height_raw", ...)
    height_unit = data.get("corrected_height_unit", ...)
    if height_raw is ... and height_unit is ...:
        height_raw = measurement.height_raw
        height_unit = measurement.height_unit
        height_m = eff["height_m"]
    elif height_raw is None:
        height_raw, height_unit, height_m = None, None, None
    else:
        if height_unit in (..., None):
            raise CorrectionError(
                "height correction requires corrected_height_unit with "
                "corrected_height_raw.")
        try:
            height_m = convert_height_to_m(height_raw, height_unit)
        except DjValidationError as exc:
            raise CorrectionError("; ".join(exc.messages))

    return {
        "status": status_, "x_m": float(x_m), "y_m": float(y_m),
        "dbh_raw": dbh_raw, "dbh_unit": dbh_unit, "dbh_cm": dbh_cm,
        "height_raw": height_raw, "height_unit": height_unit,
        "height_m": height_m,
    }


def _evidence_payload(raw):
    """Evidence is required and must say something verifiable."""
    if not raw:
        raise CorrectionError("evidence is required for a correction order.")
    if isinstance(raw, str):
        raw = [{"type": "note", "ref": "", "note": raw}]
    if not isinstance(raw, list) or not raw:
        raise CorrectionError("evidence must be a non-empty list.")
    cleaned = []
    for item in raw:
        if not isinstance(item, dict):
            raise CorrectionError("every evidence item must be an object.")
        note = str(item.get("note", "")).strip()
        ref = str(item.get("ref", "")).strip()
        kind = str(item.get("type", "note")).strip()
        if not note and not ref:
            raise CorrectionError(
                "each evidence item needs a ref or a note (photo id, "
                "datasheet page, field-book entry …).")
        cleaned.append({"type": kind, "ref": ref, "note": note})
    return cleaned


# ----------------------------------------------------------------- submit
def submit_correction(measurement, data, submitted_by=""):
    """
    Create a correction order in 'pending'.

    ``data``: reason, evidence, corrected_* fields (omitted fields keep the
    current effective value), and idempotency_key.
    """
    key = str(data.get("idempotency_key") or "").strip()
    if not key:
        raise CorrectionError("idempotency_key is required.")

    existing = MeasurementCorrection.objects.filter(
        idempotency_key=key).select_related("measurement").first()
    if existing is not None:
        # Idempotent submission: same key always yields the SAME order.
        return existing, False

    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise CorrectionError("reason is required for a correction order.")
    evidence = _evidence_payload(data.get("evidence"))

    corrected = _normalize_corrected(measurement, data)

    plot = measurement.tree.plot
    if not point_in_ring(corrected["x_m"], corrected["y_m"], plot.boundary):
        raise CorrectionError(
            "corrected stem coordinates fall outside the plot boundary "
            "(CRS/unit mix-up or wrong plot?).")

    # The correction must actually change something.
    eff = _effective_state(measurement)
    same = all(
        _close(eff[f], corrected[_canon(f)]) for f in _EFFECTIVE_FIELDS
    )
    if same:
        raise CorrectionError(
            "corrected values are identical to the current effective "
            "values — nothing to correct.")

    # Only one OPEN order at a time per measurement: two orders surviving
    # review could otherwise race to mutually contradictory applications.
    open_order = MeasurementCorrection.objects.filter(
        measurement=measurement
    ).exclude(status__in=_TERMINAL_STATUSES).first()
    if open_order is not None:
        raise CorrectionError(
            f"measurement {measurement.id} already has an open correction "
            f"order #{open_order.id} ({open_order.status}); resolve or "
            "reject it before opening another.",
            status_code=409)

    base_rev = latest_effective_revision(measurement.id)
    with transaction.atomic():
        order = MeasurementCorrection.objects.create(
            measurement=measurement,
            idempotency_key=key,
            reason=reason,
            evidence=evidence,
            # original TRANSCRIPTION (kept on the base row forever)
            original_dbh_raw=measurement.dbh_raw,
            original_dbh_unit=measurement.dbh_unit,
            original_height_raw=measurement.height_raw,
            original_height_unit=measurement.height_unit,
            # effective input at submission (base row or earlier revision)
            original_dbh_cm=eff["dbh_cm"],
            original_height_m=eff["height_m"],
            original_x_m=eff["x_m"],
            original_y_m=eff["y_m"],
            original_status=eff["status"],
            based_on_revision=base_rev,
            corrected_dbh_raw=corrected["dbh_raw"],
            corrected_dbh_unit=corrected["dbh_unit"],
            corrected_dbh_cm=corrected["dbh_cm"],
            corrected_height_raw=corrected["height_raw"],
            corrected_height_unit=corrected["height_unit"],
            corrected_height_m=corrected["height_m"],
            corrected_x_m=corrected["x_m"],
            corrected_y_m=corrected["y_m"],
            corrected_status=corrected["status"],
            submitted_by=submitted_by,
        )
    return order, True


def _canon(field):
    # effective-state key -> corrected payload key (they coincide here)
    return field


def _close(a, b, tol=1e-9):
    if a is None or b is None:
        return a is b
    if isinstance(a, str):
        return a == b
    return abs(float(a) - float(b)) <= tol


# ------------------------------------------------------------------ review
def review_correction(order, decision, reviewed_by="", note=""):
    """
    Supervisory review. decision = 'apply' (approve) or 'reject'.
    Only a pending/failed-before-review order can be reviewed; a failed
    application is retried via apply, not re-reviewed.
    """
    if order.status not in (CORRECTION_PENDING,):
        raise CorrectionError(
            f"correction #{order.id} is {order.status}; only pending orders "
            "can be reviewed.", status_code=409)
    if decision not in ("apply", "reject"):
        raise CorrectionError("review decision must be 'apply' or 'reject'.")

    with transaction.atomic():
        order.reviewed_by = reviewed_by
        order.review_note = note
        order.reviewed_at = timezone.now()
        if decision == "reject":
            order.status = CORRECTION_REJECTED
        else:
            order.status = CORRECTION_REVIEWED
            order.failure_reason = ""
        order.save()
    return order


# ------------------------------------------------------------------- apply
def apply_correction(order, *, blocker_hook=None):
    """
    Apply a REVIEWED order: append the effective revision (no overwrite).

    Phase 1 writes a 'pending' revision; phase 2 flips it to 'effective'
    after the identity re-scan. A crash in between is healed by
    reconcile_interrupted_applications() (called first here and on every
    read), so a refresh shows only complete pending/failed states and a
    retry never yields a second effective revision.
    """
    reconcile_interrupted_applications()
    order.refresh_from_db()

    # idempotent retry: already applied -> return the existing revision
    if order.status == CORRECTION_APPLIED:
        return order.revision, False
    if order.status == CORRECTION_REJECTED:
        raise CorrectionError(
            f"correction #{order.id} was rejected; it cannot be applied.",
            status_code=409)
    if order.status not in (CORRECTION_REVIEWED, CORRECTION_FAILED):
        raise CorrectionError(
            f"correction #{order.id} is {order.status}; it must be reviewed "
            "and approved before application.", status_code=409)

    # A voided revision from a previous crashed attempt is retained; the
    # retry creates a new audit row but — thanks to the reconcile step and
    # the partial unique index — never a second EFFECTIVE one.
    m = order.measurement

    # Re-verify inputs against the pre-review snapshot: another effective
    # revision must not have landed while this order waited.
    eff = _effective_state(m)
    for f in _EFFECTIVE_FIELDS:
        if not _close(eff[f], getattr(order, "original_" + _map_field(f))):
            raise CorrectionError(
                f"the effective {f} of measurement {m.id} changed between "
                "review and application; reject this order and re-submit "
                "against the current values.", status_code=409)

    # Human gate: coordinate corrections must not bypass an OPEN identity
    # contradiction. The pre-flight simulates the corrected geometry.
    blockers = correction_preflight(order)
    if blockers:
        # Persist any NEWLY triggered contradiction (tagged with this
        # order) so it shows up in the verification workbench; the
        # application then fails and waits for a human.
        _persist_blocker_conflicts(order, blockers)
        order.status = CORRECTION_FAILED
        order.failure_reason = (
            "blocked by unresolved identity contradiction(s): "
            + "; ".join(
                f"{b['field_number']} d={b['distance_m']}m "
                f"[{'NEW' if b['newly_triggered'] else 'pre-existing'} open]"
                for b in blockers))
        order.save(update_fields=["status", "failure_reason"])
        if blocker_hook is not None:
            blocker_hook(blockers)
        raise CorrectionError(order.failure_reason, status_code=409)

    with transaction.atomic():
        prior_effective = (
            MeasurementRevision.objects
            .select_for_update()
            .filter(measurement=m, revision_status=REVISION_EFFECTIVE)
            .first()
        )
        # phase 1 — the incomplete, not-yet-authoritative row
        rev = MeasurementRevision.objects.create(
            measurement=m,
            correction=order,
            supersedes=prior_effective,
            dbh_cm=order.corrected_dbh_cm,
            height_m=order.corrected_height_m,
            x_m=order.corrected_x_m,
            y_m=order.corrected_y_m,
            status=order.corrected_status,
            dbh_raw=order.corrected_dbh_raw,
            dbh_unit=order.corrected_dbh_unit,
            height_raw=order.corrected_height_raw,
            height_unit=order.corrected_height_unit,
            revision_status=REVISION_PENDING,
        )

        # A later correction in the chain demotes its predecessor first.
        # The predecessor row is kept (audit chain via supersedes); only
        # exactly one revision per measurement may be effective.
        if prior_effective is not None:
            prior_effective.revision_status = REVISION_SUPERSEDED
            prior_effective.save(update_fields=["revision_status"])

        # phase 2 — promotion. The partial unique index guarantees that
        # even a concurrent retry cannot create a second effective row.
        rev.revision_status = REVISION_EFFECTIVE
        rev.effective_at = timezone.now()
        rev.save(update_fields=["revision_status", "effective_at"])

        # Re-scan identity contradictions under the corrected, now
        # effective positions. Newly exposed contradictions are persisted
        # and tagged with this order so the UI routes the operator to
        # manual verification. The pre-flight above guarantees none of
        # them currently block THIS order; later corrections that move
        # another stem into the same open contradiction will hit the gate.
        t1, t2 = _pair_campaigns(m.campaign)
        scan_conflicts(t1, t2, triggered_by_correction=order)

        order.status = CORRECTION_APPLIED
        order.failure_reason = ""
        order.applied_at = timezone.now()
        order.save(update_fields=["status", "failure_reason", "applied_at"])

    return rev, True


def _map_field(f):
    return {"dbh_cm": "dbh_cm", "height_m": "height_m",
            "x_m": "x_m", "y_m": "y_m", "status": "status"}[f]


def _pair_campaigns(campaign):
    from inventory.models import Campaign
    other = Campaign.objects.exclude(pk=campaign.pk).order_by(
        "measured_on").first()
    if other is None:
        return campaign, campaign
    return tuple(sorted([campaign, other], key=lambda c: c.measured_on))


def _persist_blocker_conflicts(order, blockers):
    """Create IdentityConflict rows for newly triggered blockers."""
    from inventory.models import IdentityConflict, TreeMeasurement
    t1, t2 = _pair_campaigns(order.measurement.campaign)
    for b in blockers:
        if not b.get("newly_triggered"):
            continue
        m1 = TreeMeasurement.objects.get(pk=b["t1_measurement_id"])
        m2 = TreeMeasurement.objects.get(pk=b["t2_measurement_id"])
        IdentityConflict.objects.get_or_create(
            t1_measurement=m1, t2_measurement=m2,
            defaults={
                "plot_id": b["plot_id"],
                "field_number": b["field_number"],
                "t1_campaign": t1, "t2_campaign": t2,
                "distance_m": b["distance_m"],
                "resolution_note": b["hint"],
                "triggered_by_correction": order,
            })


# ------------------------------------------------------------- impact view
def impact_summary(order, estimator_ctx=None):
    """
    Impact scope before application:
      * old vs new values;
      * confirmed editions that stay frozen (never change);
      * drafts that WOULD be recomputed;
      * identity blockers (open contradictions) with the human gate.
    """
    from inventory.services.estimates_run import summarize_correction_impact
    blockers = correction_preflight(order)
    m = order.measurement
    confirmed = list(
        EstimateVersion.objects
        .filter(status=VERSION_CONFIRMED)
        .values("id", "label", "confirmed_at"))
    drafts = list(
        EstimateVersion.objects
        .exclude(status=VERSION_CONFIRMED)
        .values("id", "label", "status"))

    delta = {
        "dbh_cm": {"old": order.original_dbh_cm,
                   "new": order.corrected_dbh_cm},
        "height_m": {"old": order.original_height_m,
                     "new": order.corrected_height_m},
        "x_m": {"old": order.original_x_m, "new": order.corrected_x_m},
        "y_m": {"old": order.original_y_m, "new": order.corrected_y_m},
        "status": {"old": order.original_status,
                   "new": order.corrected_status},
    }
    out = {
        "correction": order.id,
        "measurement": {
            "id": m.id,
            "tree": str(m.tree),
            "campaign": m.campaign.code,
            "tag": m.field_number_seen,
            "original_transcription": {
                "dbh_raw": order.original_dbh_raw,
                "dbh_unit": order.original_dbh_unit,
                "height_raw": order.original_height_raw,
                "height_unit": order.original_height_unit,
            },
        },
        "changes": delta,
        "identity_blockers": blockers,
        "blocked": bool(blockers),
        "confirmed_editions_unchanged": confirmed,
        "draft_editions_to_recompute": drafts,
        "reason": order.reason,
        "evidence": order.evidence,
    }
    if estimator_ctx is not None:
        out["preview"] = summarize_correction_impact(order, estimator_ctx)
    return out


# --------------------------------------------------------- recompute (new
# draft only)
def recompute_with_correction(order, *, label=None, equation_ids=None,
                              fpc=None):
    """
    Produce a NEW DRAFT EstimateVersion that consumes this revision.
    Confirmed editions are never recomputed/touched.
    """
    reconcile_interrupted_applications()
    order.refresh_from_db()
    if order.status != CORRECTION_APPLIED:
        raise CorrectionError(
            f"correction #{order.id} is {order.status}; only an applied "
            "correction can drive a recompute.", status_code=409)
    from inventory.services.estimates_run import run_draft_for_correction
    return run_draft_for_correction(
        order, label=label, equation_ids=equation_ids, fpc=fpc)
