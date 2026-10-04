"""
Measurement correction orders (测量更正单) — the correction closed loop.

A correction order points at exactly one TreeMeasurement and carries the
before/after values, units, coordinates, a reason and field evidence.
Lifecycle::

    pending --review(approve)--> reviewed --apply--> applied
                 |                        |
                 +--review(reject)--> rejected
                                  apply failure --> failed (retryable)

Guarantees
==========
* The historical TreeMeasurement row is NEVER updated. Applying a
  correction appends one immutable MeasurementRevision; new DRAFT
  EstimateVersions overlay the latest revision, confirmed editions are
  frozen and stay re-readable forever.
* Submission is idempotent on ``idempotency_key`` — the same key always
  returns the same order, never a duplicate.
* Apply is atomic: revision + status flip + identity re-scan commit
  together or not at all. A failure leaves the order in ``failed`` (or
  its previous state) with NO partial revision, and retrying cannot
  create a second revision for the same order (OneToOne + status guard).
* Two contradictory corrections on the same measurement cannot both be
  applied: applying checks the order's original snapshot against the
  CURRENT effective values; once a competing correction has landed the
  snapshot is stale and apply returns a conflict.
* Coordinate corrections re-run the identity-contradiction scan. A
  correction can therefore OPEN an IdentityConflict, which keeps the
  pair excluded from every component until a human verifies it — the
  correction never bypasses manual identity checks.
"""
import math

from django.core.exceptions import ValidationError as DjValidationError
from django.db import models, transaction
from django.utils import timezone

from inventory.models import (
    Campaign,
    CORRECTION_APPLIED,
    CORRECTION_FAILED,
    CORRECTION_PENDING,
    CORRECTION_REJECTED,
    CORRECTION_REVIEWED,
    MeasurementCorrection,
    MeasurementRevision,
    TreeMeasurement,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.units import (
    convert_dbh_to_cm,
    convert_height_to_m,
    point_in_ring,
)

# Fields a correction order may set. Status and field-number changes are
# deliberately NOT correctable here: identity goes through imports and
# human conflict resolution, never through a value edit.
CORRECTABLE_FIELDS = ("dbh_raw", "dbh_unit", "height_raw", "height_unit",
                      "x_m", "y_m")

_SNAPSHOT_COMPARE_FIELDS = ("dbh_cm", "height_m", "x_m", "y_m")


class StaleSnapshotError(Exception):
    """The measurement's effective values moved since the order was filed."""


class CorrectionStateError(Exception):
    """Illegal workflow transition for the order's current status."""


# --------------------------------------------------------- effective state
def effective_state(measurement):
    """Current effective values: base row overlaid with the latest revision."""
    rev = (measurement.revisions.order_by("-sequence").first()
           if measurement.pk else None)
    if rev is not None:
        return {
            "field_number_seen": measurement.field_number_seen,
            "status": measurement.status,
            "dbh_raw": rev.dbh_raw, "dbh_unit": rev.dbh_unit,
            "dbh_cm": rev.dbh_cm,
            "height_raw": rev.height_raw, "height_unit": rev.height_unit,
            "height_m": rev.height_m,
            "x_m": rev.x_m, "y_m": rev.y_m,
            "source": f"revision_{rev.sequence}",
            "revision_id": rev.id,
        }
    return {
        "field_number_seen": measurement.field_number_seen,
        "status": measurement.status,
        "dbh_raw": measurement.dbh_raw, "dbh_unit": measurement.dbh_unit,
        "dbh_cm": measurement.dbh_cm,
        "height_raw": measurement.height_raw,
        "height_unit": measurement.height_unit,
        "height_m": measurement.height_m,
        "x_m": measurement.x_m, "y_m": measurement.y_m,
        "source": "base_row",
        "revision_id": None,
    }


def _close(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-9)


def project_corrected_state(current, corrected, plot):
    """
    Merge a correction's ``corrected`` subset onto the effective state and
    validate the result with the SAME rules as field ingest (explicit
    units, plausible ranges, stem inside its own plot boundary).

    Returns (new_state, changed_fields) where new_state holds raw +
    canonical values ready to store on a MeasurementRevision.
    """
    unknown = sorted(set(corrected) - set(CORRECTABLE_FIELDS))
    if unknown:
        raise DjValidationError(
            f"fields not correctable via a correction order: {unknown}; "
            f"allowed: {list(CORRECTABLE_FIELDS)}. Status/identity changes "
            "go through imports and human conflict resolution."
        )
    if not corrected:
        raise DjValidationError("corrected payload is empty — nothing to do.")

    def pick(field):
        return corrected[field] if field in corrected else current[field]

    dbh_raw, dbh_unit = pick("dbh_raw"), pick("dbh_unit")
    height_raw, height_unit = pick("height_raw"), pick("height_unit")
    x_m, y_m = pick("x_m"), pick("y_m")

    # Units stay mandatory next to every value; a correction cannot erase
    # an existing measurement (that would be a status change, not a value
    # correction), but it MAY supply a value where one was missing.
    if (dbh_raw is None) != (dbh_unit is None):
        raise DjValidationError(
            "dbh_raw and dbh_unit must be given together — a bare number "
            "without its unit is never accepted."
        )
    if dbh_raw is None and current["dbh_raw"] is not None:
        raise DjValidationError(
            "a correction cannot erase an existing dbh; re-survey or use a "
            "new import row for status changes."
        )
    if (height_raw is None) != (height_unit is None):
        raise DjValidationError(
            "height_raw and height_unit must be given together."
        )
    if height_raw is None and current["height_raw"] is not None:
        raise DjValidationError(
            "a correction cannot erase an existing height measurement."
        )

    dbh_cm = (convert_dbh_to_cm(dbh_raw, dbh_unit)
              if dbh_raw is not None else None)
    height_m = (convert_height_to_m(height_raw, height_unit)
                if height_raw is not None else None)

    if x_m is None or y_m is None:
        raise DjValidationError("corrected coordinates must both be given.")
    try:
        x_m, y_m = float(x_m), float(y_m)
    except (TypeError, ValueError):
        raise DjValidationError(
            f"corrected coordinates not numeric: {x_m!r}, {y_m!r}")
    if not point_in_ring(x_m, y_m, plot.boundary):
        raise DjValidationError(
            f"corrected stem position ({x_m}, {y_m}) falls outside plot "
            f"{plot.code} boundary (unit/CRS mix-up or wrong plot?)."
        )

    new_state = {
        "dbh_raw": dbh_raw, "dbh_unit": dbh_unit, "dbh_cm": dbh_cm,
        "height_raw": height_raw, "height_unit": height_unit,
        "height_m": height_m,
        "x_m": x_m, "y_m": y_m,
    }
    # string fields (units) compare by equality, numerics by _close
    changed = sorted(set(
        [f for f in ("dbh_unit", "height_unit")
         if new_state[f] != current[f]]
        + [f for f in ("dbh_raw", "dbh_cm", "height_raw", "height_m",
                       "x_m", "y_m")
           if not _close(new_state[f], current[f])]
    ))
    return new_state, changed


# ------------------------------------------------------------------ submit
def submit_correction(measurement, idempotency_key, corrected, reason,
                      evidence):
    """
    File a correction order. Idempotent on ``idempotency_key``: a repeat
    submission returns (existing_order, False) and never a second order.
    The corrected values are validated at filing time so nonsense never
    enters the review queue.
    """
    existing = MeasurementCorrection.objects.filter(
        idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False

    current = effective_state(measurement)
    # validate the proposal against the current effective state
    project_corrected_state(current, corrected, measurement.tree.plot)

    from django.db import IntegrityError
    try:
        # savepoint: a lost race rolls back only this insert, never the
        # caller's transaction
        with transaction.atomic():
            order = MeasurementCorrection.objects.create(
                measurement=measurement,
                idempotency_key=idempotency_key,
                original_snapshot=current,
                corrected=corrected,
                reason=reason,
                evidence=evidence,
            )
    except IntegrityError:
        # a concurrent submit with the same key won the race — return it
        return (MeasurementCorrection.objects.get(
            idempotency_key=idempotency_key), False)
    return order, True


# ------------------------------------------------------------------ review
def review_correction(correction, approve, note=""):
    """Human review: approve -> reviewed, reject -> rejected (terminal)."""
    if correction.status != CORRECTION_PENDING:
        raise CorrectionStateError(
            f"only a pending order can be reviewed (status is "
            f"{correction.status})."
        )
    correction.status = (CORRECTION_REVIEWED if approve
                         else CORRECTION_REJECTED)
    correction.review_note = note or ""
    correction.reviewed_at = timezone.now()
    correction.save(update_fields=["status", "review_note", "reviewed_at"])
    return correction


# ------------------------------------------------------------------- apply
def _check_snapshot_fresh(correction, current):
    snap = correction.original_snapshot
    stale = [f for f in _SNAPSHOT_COMPARE_FIELDS
             if not _close(current.get(f), snap.get(f))]
    if stale:
        raise StaleSnapshotError(
            f"measurement {correction.measurement_id} effective values "
            f"changed since this order was filed (fields {stale}); a "
            "competing correction has been applied. File a fresh order "
            "against the current values — contradictory conclusions are "
            "never both applied."
        )


def apply_correction(correction):
    """
    Apply a reviewed (or previously failed) order.

    Atomic: the revision, the status flip and the identity re-scan commit
    together. Returns (revision, conflicts_found). Raises
    CorrectionStateError / StaleSnapshotError / ValidationError — the
    caller is responsible for marking the order failed on unexpected
    exceptions.
    """
    if correction.status == CORRECTION_APPLIED:
        # Idempotent retry: the revision already exists, never create a
        # second one.
        return correction.revision, []
    if correction.status not in (CORRECTION_REVIEWED, CORRECTION_FAILED):
        raise CorrectionStateError(
            f"only a reviewed order can be applied (status is "
            f"{correction.status}); review it first."
        )

    with transaction.atomic():
        locked = (MeasurementCorrection.objects
                  .select_for_update().get(pk=correction.pk))
        if locked.status == CORRECTION_APPLIED:
            return locked.revision, []  # a concurrent apply won the race
        if locked.status not in (CORRECTION_REVIEWED, CORRECTION_FAILED):
            raise CorrectionStateError(
                f"order moved to {locked.status} while applying."
            )

        measurement = (TreeMeasurement.objects
                       .select_related("tree__plot")
                       .get(pk=locked.measurement_id))
        current = effective_state(measurement)
        _check_snapshot_fresh(locked, current)
        new_state, changed = project_corrected_state(
            current, locked.corrected, measurement.tree.plot)

        last = measurement.revisions.order_by("-sequence").first()
        revision = MeasurementRevision.objects.create(
            measurement=measurement,
            correction=locked,
            sequence=(last.sequence + 1) if last else 1,
            changed_fields=changed,
            **new_state,
        )
        locked.status = CORRECTION_APPLIED
        locked.applied_at = timezone.now()
        locked.failure_detail = ""
        locked.save(update_fields=["status", "applied_at", "failure_detail"])

        # Coordinates (and therefore identity geometry) may have moved:
        # re-scan contradictions against every other campaign. New
        # contradictions open IdentityConflicts and stay EXCLUDED from all
        # components until a human verifies them.
        conflicts = rescan_identity_for(measurement)

    correction.refresh_from_db()
    return revision, conflicts


def mark_apply_failed(correction, detail):
    """Record a failed apply OUTSIDE the rolled-back transaction."""
    correction.refresh_from_db()
    if correction.status == CORRECTION_APPLIED:
        return correction  # actually succeeded before the error surfaced
    correction.status = CORRECTION_FAILED
    correction.failure_detail = str(detail)[:300]
    correction.save(update_fields=["status", "failure_detail"])
    return correction


def rescan_identity_for(measurement):
    """Re-run the contradiction scan for the measurement's campaign pair."""
    campaign = measurement.campaign
    others = Campaign.objects.exclude(pk=campaign.pk).order_by("measured_on")
    found = []
    for other in others:
        t1, t2 = sorted([campaign, other],
                        key=lambda c: c.measured_on)
        found.extend(scan_conflicts(t1, t2))
    return found


# ------------------------------------------------------------------ impact
def impact_report(correction):
    """
    What this order touches: before/after values, open identity items on
    the same tree, and which estimate editions are affected (confirmed
    ones never change — only new drafts pick the revision up).
    """
    from inventory.models import EstimateVersion, IdentityConflict
    measurement = (TreeMeasurement.objects
                   .select_related("tree__plot", "campaign")
                   .get(pk=correction.measurement_id))
    current = effective_state(measurement)
    projected, changed, validation_error = None, [], None
    try:
        projected, changed = project_corrected_state(
            current, correction.corrected, measurement.tree.plot)
    except DjValidationError as exc:
        validation_error = "; ".join(exc.messages)

    tree = measurement.tree
    open_conflicts = list(
        IdentityConflict.objects
        .filter(status="open")
        .filter(models.Q(t1_measurement__tree=tree) |
                models.Q(t2_measurement__tree=tree))
    )

    campaign = measurement.campaign
    versions = []
    for v in EstimateVersion.objects.all():
        involves = campaign.pk in (v.t1_campaign_id, v.t2_campaign_id)
        if not involves:
            continue
        versions.append({
            "id": v.id, "label": v.label, "status": v.status,
            "effect": ("frozen — confirmed editions never change"
                       if v.status == "confirmed"
                       else "already computed — only NEW drafts use the "
                            "revision"),
        })

    def brief(state):
        if state is None:
            return None
        return {k: state[k] for k in
                ("dbh_raw", "dbh_unit", "dbh_cm", "height_raw",
                 "height_unit", "height_m", "x_m", "y_m")}

    return {
        "correction_id": correction.id,
        "status": correction.status,
        "measurement": {
            "id": measurement.id,
            "tree_id": tree.id,
            "plot": tree.plot.code,
            "field_number": measurement.field_number_seen,
            "campaign": campaign.code,
        },
        "filed_against": brief(correction.original_snapshot),
        "current_effective": brief(current),
        "projected_after_apply": brief(projected),
        "changed_fields": changed,
        "validation_error": validation_error,
        "open_identity_conflicts_on_tree": [
            {"id": c.id, "field_number": c.field_number,
             "distance_m": c.distance_m,
             "note": "excluded from every component until a human verifies "
                     "— corrections never bypass this"}
            for c in open_conflicts
        ],
        "estimate_versions": versions,
    }
