"""
Effective measurement view + crash recovery for measurement revisions.

A TreeMeasurement row is the historical field record and is never updated.
Corrections append MeasurementRevision rows; every consumer (estimator,
identity scan, API serializers) reads the *effective* value of a
measurement: the latest EFFECTIVE revision if one exists, otherwise the
base row.

Revision creation is two-phase (pending -> effective, void on failure).
``reconcile_interrupted_applications`` closes the tiny crash window between
the two phases so that after a refresh the world contains only complete
states: a stale ``pending`` revision is voided and its correction order is
marked ``failed``. Retrying then issues at most ONE effective revision
(the partial unique index is the database-level backstop).
"""
from types import SimpleNamespace

from django.utils import timezone

from inventory.models import (
    CORRECTION_APPLIED,
    CORRECTION_FAILED,
    REVISION_EFFECTIVE,
    REVISION_PENDING,
    REVISION_VOID,
    MeasurementRevision,
    TreeMeasurement,
)


# Fields a revision can override. Keep in sync with estimator/conflict rows.
_OVERLAY_FIELDS = ("dbh_cm", "height_m", "x_m", "y_m", "status",
                   "dbh_raw", "dbh_unit", "height_raw", "height_unit")


def latest_effective_revision(measurement_id):
    return (
        MeasurementRevision.objects
        .filter(measurement_id=measurement_id,
                revision_status=REVISION_EFFECTIVE)
        .select_related("correction")
        .first()
    )


def effective_revision_map(campaign=None, measurement_ids=None):
    """{measurement_id: revision} for every effective revision."""
    qs = MeasurementRevision.objects.filter(
        revision_status=REVISION_EFFECTIVE
    ).select_related("correction")
    if campaign is not None:
        qs = qs.filter(measurement__campaign=campaign)
    if measurement_ids is not None:
        qs = qs.filter(measurement_id__in=measurement_ids)
    return {r.measurement_id: r for r in qs}


def effective_values(measurement, revision=None):
    """
    Effective (calculation-facing) values for one measurement.
    Returns a plain dict that always carries the same keys the base row
    would provide, plus the trace markers ``revision_id``/``correction_id``.
    """
    rev = revision
    if rev is None:
        rev = getattr(measurement, "_effective_revision", None)
    if rev is None:
        rev = latest_effective_revision(measurement.id)

    values = {
        "dbh_cm": measurement.dbh_cm,
        "height_m": measurement.height_m,
        "x_m": measurement.x_m,
        "y_m": measurement.y_m,
        "status": measurement.status,
        "dbh_raw": measurement.dbh_raw,
        "dbh_unit": measurement.dbh_unit,
        "height_raw": measurement.height_raw,
        "height_unit": measurement.height_unit,
        "revision_id": None,
        "correction_id": None,
    }
    if rev is not None:
        for f in _OVERLAY_FIELDS:
            values[f] = getattr(rev, f)
        values["revision_id"] = rev.id
        values["correction_id"] = rev.correction_id
    return values


def annotate_queryset(qs):
    """Attach ``_effective_revision`` to every measurement in a queryset."""
    rev_map = effective_revision_map(
        measurement_ids=[m.id for m in qs])
    for m in qs:
        m._effective_revision = rev_map.get(m.id)
    return qs


def effective_campaign_rows(campaign, extra_overlay=None):
    """
    Lightweight row objects for one campaign with revisions overlaid.

    Used by the identity scanner so contradictions are tested against the
    positions calculations actually use, never against stale base rows.
    ``extra_overlay`` maps measurement_id -> field dict to simulate an
    as-yet-unapplied revision (impact preview / application pre-flight).
    """
    qs = (
        TreeMeasurement.objects
        .filter(campaign=campaign)
        .select_related("tree", "tree__plot", "tree__superseded_tree")
    )
    measurements = list(qs)
    rev_map = effective_revision_map(
        measurement_ids=[m.id for m in measurements])
    extra_overlay = extra_overlay or {}

    rows = []
    for m in measurements:
        rev = rev_map.get(m.id)
        values = effective_values(m, rev)
        if m.id in extra_overlay:
            values = {**values, **extra_overlay[m.id]}
        rows.append(SimpleNamespace(
            id=m.id,
            tree_id=m.tree_id,
            field_number_seen=m.field_number_seen,
            tree=SimpleNamespace(
                id=m.tree_id,
                plot_id=m.tree.plot_id,
                plot=m.tree.plot,
                superseded_tree_id=m.tree.superseded_tree_id,
            ),
            **{f: values[f] for f in _OVERLAY_FIELDS},
        ))
    return rows


def reconcile_interrupted_applications():
    """
    Crash recovery (idempotent).

    A 'pending' revision only exists between phase 1 (revision written) and
    phase 2 (made effective). If the process died in that window the
    correction can never be 'applied' (that happens in the same later
    transaction), so the honest state after refresh is:

        revision -> void (incomplete, not authoritative)
        correction -> failed (retryable, no second revision yet exists)

    Returns the list of recovered correction ids.
    """
    recovered = []
    stale = (
        MeasurementRevision.objects
        .filter(revision_status=REVISION_PENDING)
        .select_related("correction")
    )
    for rev in stale:
        correction = rev.correction
        if correction.status == CORRECTION_APPLIED and rev.id:
            # Should be impossible; an applied correction means phase 2
            # committed. Leave untouched rather than guess.
            continue
        rev.revision_status = REVISION_VOID
        rev.void_reason = (
            "application interrupted before this revision became effective; "
            "voided by crash recovery — retry the correction order."
        )
        rev.save(update_fields=["revision_status", "void_reason"])
        if correction.status != CORRECTION_FAILED:
            correction.status = CORRECTION_FAILED
            correction.failure_reason = (
                "application interrupted; revision voided, safe to retry."
            )
            correction.save(update_fields=["status", "failure_reason"])
        recovered.append(correction.id)
    return recovered


def revision_ledger(measurement_ids=None):
    """Effective + voided revisions for provenance/audit display."""
    qs = (
        MeasurementRevision.objects
        .select_related("measurement", "measurement__tree",
                        "measurement__tree__plot", "measurement__campaign",
                        "correction", "supersedes")
        .order_by("created_at")
    )
    if measurement_ids is not None:
        qs = qs.filter(measurement_id__in=measurement_ids)
    out = []
    for r in qs:
        m = r.measurement
        out.append({
            "revision_id": r.id,
            "revision_status": r.revision_status,
            "correction_id": r.correction_id,
            "measurement_id": m.id,
            "tree": f"{m.tree.plot.code}/{m.field_number_seen}",
            "campaign": m.campaign.code,
            "dbh_cm": r.dbh_cm,
            "height_m": r.height_m,
            "x_m": r.x_m,
            "y_m": r.y_m,
            "status": r.status,
            "supersedes_revision_id": r.supersedes_id,
            "effective_at": r.effective_at.isoformat() if r.effective_at
                            else None,
            "void_reason": r.void_reason,
            "reason": r.correction.reason,
        })
    return out


def mark_now():
    return timezone.now()
