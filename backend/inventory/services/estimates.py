"""
Shared EstimateVersion helpers.

``run_draft_estimate`` is the single code path that creates a DRAFT
edition — used both by POST /estimates/ and by a correction order's
recompute action. Every draft records exactly which measurement
revisions it used (``applied_revision_ids`` in the design snapshot and
``provenance.applied_revisions`` in the payload), so a corrected value
is always traceable from the numbers back to the signed-off order.

``diff_result_payloads`` produces the old-vs-new comparison shown next
to a recomputed draft. Confirmed editions are never recomputed — the
diff is always "frozen old edition vs new draft".
"""
from django.conf import settings

from inventory.models import EstimateVersion
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)


def run_draft_estimate(label, t1, t2, equations_qs, fpc=True):
    """Compute and persist a DRAFT EstimateVersion. Never touches others."""
    table_t1, table_t2, equations, plots, strata = (
        build_measurement_table(t1, t2, equations_qs)
    )
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
    result = estimate(table_t1, table_t2, equations, plots, strata,
                      design,
                      resolved_renumber_pairs=renumber,
                      resolved_distinct_pairs=distinct)
    result["species_without_equation"] = uncovered
    checksum = equation_checksum(equations)

    snap_strata = {code: {**s, "plot_codes": list(s["plot_codes"])}
                   for code, s in strata.items()}
    design_snapshot = {
        **design,
        "strata": snap_strata,
        "equation_ids": sorted(e.id for e in equations_qs),
        "equation_codes": {sp: e["code"] + "@" + e["version"]
                           for sp, e in equations.items()},
        "area_tolerance": settings.PLOT_AREA_TOLERANCE,
        # the exact correction revisions this draft's numbers rely on
        "applied_revision_ids": sorted(
            r["revision_id"] for r in result["provenance"]
            ["applied_revisions"]
        ),
    }

    version = EstimateVersion.objects.create(
        label=label, t1_campaign=t1, t2_campaign=t2,
        design_snapshot=design_snapshot,
        result_payload=result, equation_checksum=checksum,
    )
    version.equations.set(equations_qs)
    return version


def diff_result_payloads(old_payload, new_payload):
    """Component-level old-vs-new comparison of two result payloads."""
    def comp_diff(key):
        before = old_payload["components"][key]["total_kg"]
        after = new_payload["components"][key]["total_kg"]
        return {
            "before_total_kg": round(before, 3),
            "after_total_kg": round(after, 3),
            "delta_kg": round(after - before, 3),
        }

    old_net = old_payload["net_change"]["total_kg"]
    new_net = new_payload["net_change"]["total_kg"]
    return {
        "components": {k: comp_diff(k)
                       for k in ("survivor_growth", "mortality",
                                 "ingrowth")},
        "net_change": {
            "before_total_kg": round(old_net, 3),
            "after_total_kg": round(new_net, 3),
            "delta_kg": round(new_net - old_net, 3),
        },
        "stocks": {
            "t1": {"before_kg": round(old_payload["stocks"]["t1_mg"]
                                      * 1000, 3),
                   "after_kg": round(new_payload["stocks"]["t1_mg"]
                                     * 1000, 3)},
            "t2": {"before_kg": round(old_payload["stocks"]["t2_mg"]
                                      * 1000, 3),
                   "after_kg": round(new_payload["stocks"]["t2_mg"]
                                     * 1000, 3)},
        },
        "revisions_in_new_draft":
            new_payload["provenance"]["applied_revisions"],
        "note": "the compared edition is frozen; only this new draft "
                "reflects the applied correction",
    }
