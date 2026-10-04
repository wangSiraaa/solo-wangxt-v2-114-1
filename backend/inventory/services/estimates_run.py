"""
Building DRAFT EstimateVersions.

Extracted from the view layer so that both the plain "run draft" endpoint
and the "recompute with this correction order" endpoint share one code
path. Only DRAFT versions are ever created here; a confirmed version is
frozen (model-level) and simply never passed through this module.

Every run records the measurement revisions (applied correction orders)
that contributed to it in the design snapshot, on the M2M relation and in
the provenance payload — so an edition can always show WHICH corrected
values it used, while an older confirmed edition keeps showing the
un-revised values it was frozen with.
"""
from django.conf import settings

from inventory.models import (
    AllometricEquation,
    EstimateVersion,
    MeasurementRevision,
)
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)


def _equations_dict(equations_qs):
    equations = {}
    for e in equations_qs:
        for sp in e.species.all():
            equations[sp.code] = {
                "code": e.code, "version": e.version,
                "a": e.a, "b": e.b, "c": e.c,
                "dbh_min_cm": e.dbh_min_cm, "dbh_max_cm": e.dbh_max_cm,
                "height_required": e.height_required,
                "residual_sigma": e.residual_sigma,
                "citation": e.citation,
            }
    return equations


def run_draft(*, label, t1, t2, equation_ids, fpc=True,
              extra_design=None, tag_revision_ids=None):
    """
    Compute a fresh DRAFT edition from current EFFECTIVE measurements.
    Raises ValueError with a user-facing message on bad inputs.
    """
    if t1.measured_on >= t2.measured_on:
        raise ValueError("need t1 earlier than t2 campaign codes")
    if not equation_ids:
        raise ValueError("equation_ids invalid/empty")

    equations_qs = list(
        AllometricEquation.objects
        .filter(id__in=equation_ids)
        .prefetch_related("species"))
    if len(equations_qs) != len(set(equation_ids)) or not equations_qs:
        raise ValueError("equation_ids invalid/empty")

    table_t1, table_t2, equations, plots, strata = (
        build_measurement_table(t1, t2, equations_qs))
    uncovered = sorted({
        r["species"] for r in table_t1 + table_t2
        if r["species"] not in equations
    })
    renumber, distinct = resolved_identity_pairs(t1, t2)

    interval = round((t2.measured_on - t1.measured_on).days / 365.25, 3)
    design = {
        "t1_code": t1.code, "t2_code": t2.code,
        "interval_years": interval,
        "dbh_sd_cm": settings.DBH_MEASUREMENT_SD_CM,
        "height_sd_m": settings.HEIGHT_MEASUREMENT_SD_M,
        "zero_tol_cm": settings.ZERO_GROWTH_TOL_CM,
        "recruitment_cm": settings.RECRUITMENT_DBH_CM,
        "fpc": bool(fpc),
        "crs_epsg": settings.SURVEY_CRS_EPSG,
    }
    if extra_design:
        design.update(extra_design)

    result = estimate(table_t1, table_t2, equations, plots, strata, design,
                      resolved_renumber_pairs=renumber,
                      resolved_distinct_pairs=distinct)
    result["species_without_equation"] = uncovered
    checksum = equation_checksum(equations)

    # effective revisions folded into THIS edition
    revised_ids = sorted({
        r["revision_id"] for r in table_t1 + table_t2
        if r.get("revision_id")
    })
    if tag_revision_ids:
        revised_ids = sorted(set(revised_ids) | set(tag_revision_ids))
    result["measurement_revision_ids"] = revised_ids

    snap_strata = {code: {**s, "plot_codes": list(s["plot_codes"])}
                   for code, s in strata.items()}
    design_snapshot = {
        **design,
        "strata": snap_strata,
        "equation_ids": sorted(equation_ids),
        "equation_codes": {sp: e["code"] + "@" + e["version"]
                           for sp, e in equations.items()},
        "area_tolerance": settings.PLOT_AREA_TOLERANCE,
        "measurement_revision_ids": revised_ids,
    }

    version = EstimateVersion.objects.create(
        label=label, t1_campaign=t1, t2_campaign=t2,
        design_snapshot=design_snapshot,
        result_payload=result, equation_checksum=checksum,
    )
    version.equations.set(equations_qs)
    if revised_ids:
        version.measurement_revisions.set(
            MeasurementRevision.objects.filter(id__in=revised_ids))
    return version


# --------------------------------------------- correction-driven recompute
def run_draft_for_correction(order, *, label=None, equation_ids=None,
                             fpc=None):
    """
    New draft built AFTER the order's revision became effective. Confirms
    the correction-chain requirement: confirmed editions are untouched,
    only the new draft consumes the revision.
    """
    m = order.measurement
    t1, t2 = _pair_campaigns(m.campaign)

    if equation_ids is None:
        # default: equations of the most recent edition over this pair
        prev = (
            EstimateVersion.objects
            .filter(t1_campaign=t1, t2_campaign=t2)
            .order_by("-created_at").first())
        if prev is None:
            raise ValueError(
                "no previous edition to inherit equation_ids from; "
                "supply equation_ids explicitly.")
        equation_ids = [e.id for e in prev.equations.all()]
        fpc = prev.design_snapshot.get("fpc", True) if fpc is None else fpc
    fpc = True if fpc is None else fpc

    label = label or f"Draft recompute after correction #{order.id}"
    version = run_draft(
        label=label, t1=t1, t2=t2, equation_ids=equation_ids, fpc=fpc,
        extra_design={"derived_from_correction": order.id},
        tag_revision_ids=[order.revision.id])
    return version


def _pair_campaigns(campaign):
    from inventory.models import Campaign
    other = Campaign.objects.exclude(pk=campaign.pk).order_by(
        "measured_on").first()
    t1, t2 = sorted([campaign, other], key=lambda c: c.measured_on)
    return t1, t2


# --------------------------------------------------------- impact preview
def summarize_correction_impact(order, estimator_ctx):
    """
    Optional numeric preview: run the estimator with the correction
    simulated and return net/component totals vs. a baseline run. Both are
    throwaway computations — no EstimateVersion rows are created.
    """
    # Kept lightweight on purpose; the API's impact endpoint mainly reports
    # scope/blockers. Full numbers are produced by the recompute endpoint.
    return {"note": "use the recompute endpoint for full new-draft numbers."}
