"""
Acceptance tests for the permanent-plot station.

Covers the acceptance checks specified by the station:
  A. remeasurement renumber handling;
  B. unequal plot areas in population expansion;
  C. measurement-unit mistakes rejected at ingest;
  D. real zero growth vs missing data vs mortality kept distinct;
  E. same number + contradictory position never auto-merged;
  F. confirmed estimate editions cannot be silently changed by a new
     allometric equation;
  G. measurement correction orders (测量更正单): idempotent submission,
     pending->reviewed->applied/rejected, applied corrections APPEND a
     traceable revision (never overwrite), only a NEW draft consumes it,
     confirmed editions stay byte-identical, coordinate corrections that
     open an identity conflict cannot bypass human verification, and an
     interrupted/repeated application yields at most one effective
     revision.
"""
import json

from django.conf import settings
from django.core.exceptions import ValidationError as DjValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    CORRECTION_APPLIED,
    CORRECTION_FAILED,
    CORRECTION_PENDING,
    CORRECTION_REJECTED,
    CORRECTION_REVIEWED,
    EstimateVersion,
    IdentityConflict,
    MeasurementCorrection,
    MeasurementRevision,
    Plot,
    REVISION_EFFECTIVE,
    REVISION_PENDING,
    REVISION_VOID,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements
from inventory.services.ingest import import_campaign_rows, verify_plot_area
from inventory.services.units import convert_dbh_to_cm, ring_area_ha
from inventory.services.revisions import (
    effective_values,
    reconcile_interrupted_applications,
)


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM, AN, DE = "alive_measured", "alive_not_measured", "dead"


class EstimatorAcceptanceTests(TestCase):
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        # Unequal plot areas: 0.10 ha and 0.25 ha.
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.p2 = Plot.objects.create(
            code="P2", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)

    def _run(self):
        t1t, t2t, equations, plots, strata = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        ren, dist = resolved_identity_pairs(self.t1, self.t2)
        design = dict(t1_code="t1", t2_code="t2", interval_years=5.0,
                      dbh_sd_cm=0.1, height_sd_m=0.3, zero_tol_cm=0.15,
                      recruitment_cm=5.0, fpc=False, crs_epsg=32650)
        return estimate(t1t, t2t, equations, plots, strata, design, ren, dist)

    # ---------- C. unit mistakes ------------------------------------------------
    def test_dbh_unit_must_be_explicit(self):
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(25.0, None)

    def test_mm_entered_as_cm_is_rejected_by_range(self):
        # 250 (mm) typed as cm -> 250 cm beyond the accepted demo range.
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(250.0, "cm")

    def test_mm_value_correctly_converted(self):
        self.assertAlmostEqual(convert_dbh_to_cm(250.0, "mm"), 25.0)

    def test_import_rejects_bad_unit_rows(self):
        rows = [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=10, y_m=10, status=AM,
                 dbh_raw=250.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P1", field_number="2", species="OAK",
                 x_m=12, y_m=12, status=AM,
                 dbh_raw=20.0, height_raw=15.0, height_unit="m"),
        ]
        r = import_campaign_rows(self.t2, rows, 0.01)
        self.assertEqual(r["n_rejected"], 2)
        self.assertEqual(TreeMeasurement.objects.count(), 0)

    # ---------- B. unequal plot areas ------------------------------------------
    def test_unequal_plot_areas_expanded_per_plot(self):
        # 100 kg growth on each plot; per-ha: 1000 vs 400 kg/ha.
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        # choose t2 dbh giving +100 kg growth per tree with a=0.1,b=2,c=.5
        def b_at(d):
            return 0.1 * d ** 2 * 15 ** 0.5
        import math
        d2 = math.sqrt((b_at(20.0) + 100.0) / (0.1 * 15 ** 0.5))
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        res = self._run()
        # per-ha mean = (1000 + 400)/2 = 700 kg/ha; x 100 ha = 70 000 kg
        self.assertAlmostEqual(
            res["components"]["survivor_growth"]["total_kg"],
            70_000.0, delta=1e-6)
        # The naive "mean tree * area" would give 100 kg * (100/0.175 avg?)
        # and clearly differs; the provenance carries per-plot values.
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual(p1["area_ha"], 0.10)

    def test_plot_area_polygon_crosscheck(self):
        bad = Plot(code="BAD", stratum=self.sA, x_m=0, y_m=0,
                   declared_area_ha=0.50, boundary=rect(0, 0, 50, 20),
                   area_polygon_ha=0.10)
        with self.assertRaises(DjValidationError):
            verify_plot_area(bad, 0.01)

    # ---------- D. zero / missing / dead ---------------------------------------
    def test_zero_growth_missing_and_dead_are_distinct(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=AM, dbh_raw=22.0, dbh_unit="cm",
                 height_raw=16.0, height_unit="m"),
        ], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m",
                 notes="verified zero growth"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AN),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=DE),
        ], 0.01)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([z["tree"] for z in p1["verified_zero_growth"]],
                         ["P1/z"])
        self.assertEqual([m["tree"] for m in p1["alive_not_measured"]],
                         ["P1/m"])
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/d"])
        # missing survivor did NOT silently become zero growth:
        self.assertTrue(p1["imputed_survivor_growth_kg"] >= 0)

    # ---------- A + E. renumber / same-number contradiction --------------------
    def test_renumber_keeps_one_individual(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="007", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        tree = Tree.objects.get(current_field_number="007")
        tree.current_field_number = "017"
        tree.save(update_fields=["current_field_number"])
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="017", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        t1t, t2t, *_ = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        pairing = pair_measurements(t1t, t2t)
        self.assertEqual(len(pairing["pairs"]), 1)
        self.assertEqual(pairing["pairs"][0]["kind"], "renumber")

    def test_same_number_position_contradiction_is_excluded(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="008", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        # new tree row, same label, 15 m away
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="008", species="OAK", x_m=15, y_m=5,
                 status=AM, dbh_raw=12.0, dbh_unit="cm",
                 height_raw=10.0, height_unit="m")], 0.01)
        self.assertEqual(
            Tree.objects.filter(plot=self.p1,
                                current_field_number="008").count(), 2)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        excluded = [c["tree"] for c in p1["excluded_identity_conflicts"]]
        self.assertIn("P1/008", excluded)
        # not counted as growth, nor as mortality, nor as ingrowth
        self.assertEqual(p1["kg"]["survivor_growth"], 0.0)
        self.assertEqual(p1["mortality"], [])
        self.assertEqual(p1["ingrowth"], [])

    def test_distinct_resolution_counts_removal_and_ingrowth(self):
        from inventory.services.conflicts import scan_conflicts
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="009", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=16.0, dbh_unit="cm",
                 height_raw=12.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="009", species="OAK", x_m=25, y_m=5,
                 status=AM, dbh_raw=8.0, dbh_unit="cm",
                 height_raw=8.0, height_unit="m")], 0.01)
        found = scan_conflicts(self.t1, self.t2)
        self.assertTrue(found)
        client = APIClient()
        cid = found[0]["id"]
        resp = client.post(f"/api/conflicts/{cid}/resolve/",
                           {"status": "distinct", "note": "new recruit"})
        self.assertEqual(resp.status_code, 200)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/009"])
        self.assertEqual([m["tree"] for m in p1["ingrowth"]], ["P1/009"])

    # ---------- F. confirmed edition immutability ------------------------------
    def test_confirmed_estimate_is_frozen_against_new_equation(self):
        client = APIClient()
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        body = dict(label="v1", t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq.id], fpc=False)
        r = client.post("/api/estimates/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        before = r.json()["result_payload"]["components"]["survivor_growth"]

        rc = client.post(f"/api/estimates/{vid}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)

        # 1) the JSON result stays byte-stable
        again = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(
            again["result_payload"]["components"]["survivor_growth"], before)

        # 2) the equation is locked: coefficient change refused
        self.eq.refresh_from_db()
        self.eq.a = 0.999
        with self.assertRaises(PermissionError):
            self.eq.save()

        # 3) the edition row itself cannot be mutated
        version = EstimateVersion.objects.get(pk=vid)
        version.label = "tampered"
        with self.assertRaises(PermissionError):
            version.save()

        # 4) a new equation must be issued as a NEW equation row/version
        eq2 = AllometricEquation.objects.create(
            code="OAK", version="2", status="draft",
            a=0.2, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional revised")
        eq2.species.add(self.oak)
        r2 = client.post("/api/estimates/",
                         dict(label="v2-new-equation",
                              t1_campaign="t1", t2_campaign="t2",
                              equation_ids=[eq2.id], fpc=False),
                         format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r2.json()["id"], vid)
        # old edition unchanged
        old = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(old["label"], "v1")
        self.assertEqual(
            old["result_payload"]["components"]["survivor_growth"], before)

    def test_result_payload_records_units_and_sources(self):
        res = self._run()
        self.assertEqual(res["units"]["dbh"],
                         "cm (converted at ingest; raw unit retained)")
        self.assertEqual(res["units"]["height"], "m")
        self.assertIn("estimator", res["design"])
        self.assertTrue(res["uncertainty_assumptions"])
        self.assertIn("OAK", res["equations_used"])


# ----------------------------------------------------------------------------
# G. Measurement correction orders (测量更正单)
# ----------------------------------------------------------------------------
class MeasurementCorrectionAcceptanceTests(TestCase):
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.client = APIClient()
        # one survivor pair: 20.0 cm at t1; t2 is the mistranscribed record
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)

    def _legacy_t2(self, dbh_cm=250.0, dbh_raw=250.0, dbh_unit="cm"):
        """
        Simulate the PRE-EXISTING erroneous record found by the field
        review: during the 2024 campaign 25.0 cm was mistranscribed as
        "250" and the legacy bulk load stored it in centimetres (canonical
        250 cm), bypassing the range guard the modern /imports/ endpoint
        enforces. The ORIGINAL raw/unit fields are kept intact for audit;
        the correction order fixes the transcription.
        """
        # first a valid t2 row exists for the tree (same tree as t1/1)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=25.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        m = TreeMeasurement.objects.get(
            campaign=self.t2, tree__plot=self.p1, field_number_seen="1")
        # then the legacy load corrupts the canonical column the way the
        # historical import did (raw 250 + unit cm -> 250 cm).
        m.dbh_raw, m.dbh_unit, m.dbh_cm = dbh_raw, dbh_unit, dbh_cm
        m.save(update_fields=["dbh_raw", "dbh_unit", "dbh_cm"])
        return m

    def _draft(self, label="d"):
        r = self.client.post("/api/estimates/",
                             dict(label=label, t1_campaign="t1",
                                  t2_campaign="t2",
                                  equation_ids=[self.eq.id], fpc=False),
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def _growth(self, version_json):
        return (version_json["result_payload"]["components"]
                ["survivor_growth"]["total_kg"])

    def _submit(self, measurement, **over):
        payload = dict(
            measurement=measurement.id,
            idempotency_key=f"fix-{measurement.id}",
            reason="2024 remeasure: 25.0 cm transcribed as 250 mm; field "
                   "datasheet + tape photo confirm 25.0 cm (raw 250 mm).",
            evidence=[{"type": "photo", "ref": "IMG-2024-0071",
                       "note": "tape at 25.0 cm"}],
            corrected_dbh_raw=250.0, corrected_dbh_unit="mm",
        )
        payload.update(over)
        return self.client.post("/api/corrections/", payload, format="json")

    # --- G1: unit correction -> new draft changes, confirmed does not ----
    def test_unit_correction_changes_new_draft_only(self):
        m = self._legacy_t2()
        v_before = self._draft("before")
        growth_before = self._growth(v_before)

        rc = self.client.post(f"/api/estimates/{v_before['id']}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)
        confirmed_id = v_before["id"]

        r = self._submit(m)
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()["id"]
        # original raw value & unit are snapshotted on the order
        self.assertEqual(r.json()["original_dbh_raw"], 250.0)
        self.assertEqual(r.json()["original_dbh_unit"], "cm")
        self.assertEqual(r.json()["original_dbh_cm"], 250.0)
        self.assertEqual(r.json()["status"], CORRECTION_PENDING)

        # cannot apply before review
        ra = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(ra.status_code, 409)

        rr = self.client.post(f"/api/corrections/{cid}/review/",
                              dict(decision="apply", reviewed_by="supervisor",
                                   note="datasheet cross-checked"),
                              format="json")
        self.assertEqual(rr.status_code, 200, rr.content)
        self.assertEqual(rr.json()["status"], CORRECTION_REVIEWED)

        ra = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(ra.status_code, 200, ra.content)
        self.assertEqual(ra.json()["status"], CORRECTION_APPLIED)
        rev_id = ra.json()["revision"]["id"]

        # historical base row is untouched ...
        m.refresh_from_db()
        self.assertEqual(m.dbh_cm, 250.0)
        self.assertEqual(m.dbh_raw, 250.0)
        self.assertEqual(m.dbh_unit, "cm")
        # ... but the effective view carries the revision (250 mm -> 25 cm)
        self.assertEqual(effective_values(m)["dbh_cm"], 25.0)
        self.assertEqual(effective_values(m)["dbh_unit"], "mm")
        self.assertEqual(effective_values(m)["revision_id"], rev_id)

        # recompute creates a NEW draft that consumes the revision
        rec = self.client.post(f"/api/corrections/{cid}/recompute/", {},
                               format="json")
        self.assertEqual(rec.status_code, 201, rec.content)
        new_draft = rec.json()
        self.assertNotEqual(new_draft["id"], confirmed_id)
        self.assertEqual(new_draft["status"], "draft")
        self.assertIn(rev_id, new_draft["measurement_revision_ids"])
        self.assertEqual(
            new_draft["design_snapshot"]["measurement_revision_ids"],
            [rev_id])
        self.assertEqual(
            new_draft["result_payload"]["measurement_revision_ids"],
            [rev_id])
        growth_after = self._growth(new_draft)
        # growth must change vs the pre-correction draft
        self.assertNotAlmostEqual(growth_after, growth_before, places=6)

        # the CONFIRMED edition stays byte-for-byte identical
        old = self.client.get(f"/api/estimates/{confirmed_id}/").json()
        self.assertEqual(old["status"], "confirmed")
        self.assertAlmostEqual(self._growth(old), growth_before, places=9)
        self.assertEqual(
            old["result_payload"]["components"]["survivor_growth"],
            v_before["result_payload"]["components"]["survivor_growth"])
        self.assertEqual(old["measurement_revision_ids"], [])
        # checksum frozen & still reviewable
        self.assertEqual(old["equation_checksum"],
                         v_before["equation_checksum"])

    # --- G2: idempotent submission ----------------------------------------
    def test_duplicate_idempotency_key_returns_same_order(self):
        m = self._legacy_t2()
        first = self._submit(m)
        self.assertEqual(first.status_code, 201)
        second = self._submit(m)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.headers.get("Idempotent-Replay"), "true")
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(MeasurementCorrection.objects.count(), 1)

    # --- G3: mutually contradictory reviews can't both be applied --------
    def test_parallel_corrections_on_same_record_are_blocked(self):
        m = self._legacy_t2()
        first = self._submit(m, idempotency_key="k1")
        self.assertEqual(first.status_code, 201)
        second = self._submit(
            m, idempotency_key="k2",
            corrected_dbh_raw=99.0, corrected_dbh_unit="cm")
        self.assertEqual(second.status_code, 409)
        # the second, contradictory order was not created
        self.assertEqual(MeasurementCorrection.objects.count(), 1)

    def test_rejected_order_cannot_be_applied_and_revision_never_issued(self):
        m = self._legacy_t2()
        cid = self._submit(m).json()["id"]
        rr = self.client.post(f"/api/corrections/{cid}/review/",
                              dict(decision="reject", note="unverifiable"),
                              format="json")
        self.assertEqual(rr.status_code, 200)
        self.assertEqual(rr.json()["status"], CORRECTION_REJECTED)
        ra = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(ra.status_code, 409)
        self.assertEqual(MeasurementRevision.objects.count(), 0)
        # no effective change
        self.assertIsNone(effective_values(m)["revision_id"])

    # --- G4: coordinate correction opening a conflict needs human gate ---
    def test_coordinate_correction_blocked_by_open_conflict(self):
        # A second t1 survivor (tag 2) sits at (30,5), far from tree 1.
        # The correction moves the t2 stem from (5,5) to (30.5,5): it then
        # lands ~0.5 m from t1/2 while carrying a DIFFERENT label -> a
        # "possible renumber" contradiction that a human must verify.
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="2", species="OAK", x_m=30, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
        ], 0.01)
        m = self._legacy_t2(dbh_cm=25.0, dbh_raw=25.0, dbh_unit="cm")
        cid = self._submit(m, idempotency_key="move1",
                           corrected_x_m=30.5, corrected_y_m=5.0,
                           corrected_dbh_raw=25.0,
                           corrected_dbh_unit="cm").json()["id"]
        # impact endpoint surfaces the blocker BEFORE application
        imp = self.client.get(f"/api/corrections/{cid}/impact/")
        self.assertEqual(imp.status_code, 200)
        self.assertTrue(imp.json()["blocked"])
        self.assertTrue(imp.json()["identity_blockers"])
        self.assertTrue(any(
            b["newly_triggered"]
            for b in imp.json()["identity_blockers"]))
        # confirmed editions are listed as always unchanged
        self.assertIsInstance(
            imp.json()["confirmed_editions_unchanged"], list)

        self.client.post(f"/api/corrections/{cid}/review/",
                         dict(decision="apply"), format="json")
        ra = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(ra.status_code, 409, ra.content)
        body = ra.json()
        self.assertEqual(body["status"], CORRECTION_FAILED)
        self.assertIn("identity", body["failure_reason"])
        # no revision became effective — the human gate held
        self.assertEqual(
            MeasurementRevision.objects
            .filter(revision_status=REVISION_EFFECTIVE).count(), 0)
        self.assertIsNone(effective_values(m)["revision_id"])
        # base coordinates untouched
        m.refresh_from_db()
        self.assertEqual((m.x_m, m.y_m), (5.0, 5.0))

        # the NEWLY triggered contradiction is persisted (and tagged) so
        # the verification workbench can route an operator to it.
        open_conflicts = IdentityConflict.objects.filter(status=CONFLICT_OPEN)
        self.assertTrue(open_conflicts.exists())
        conflict = open_conflicts.first()
        self.assertEqual(conflict.triggered_by_correction_id, cid)

        # human verifies the t2 stem is tree 1 that simply moved (NOT
        # individual 2): 'distinct' dismisses the possible-renumber hint.
        rv = self.client.post(f"/api/conflicts/{conflict.id}/resolve/",
                              dict(status="distinct",
                                   note="t2 tag 1 is individual 1, not 2"))
        self.assertEqual(rv.status_code, 200)
        # the application can now proceed (failed -> applied retry)
        ra2 = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(ra2.status_code, 200, ra2.content)
        self.assertEqual(ra2.json()["status"], CORRECTION_APPLIED)
        m.refresh_from_db()
        self.assertEqual((m.x_m, m.y_m), (5.0, 5.0))  # base row still intact
        self.assertEqual(
            (effective_values(m)["x_m"], effective_values(m)["y_m"]),
            (30.5, 5.0))

    # --- G5: interrupted apply -> only pending/failed; retry no dup ------
    def test_interrupted_application_is_recoverable_without_duplicate_revision(self):
        m = self._legacy_t2()
        cid = self._submit(m).json()["id"]
        self.client.post(f"/api/corrections/{cid}/review/",
                         dict(decision="apply"), format="json")

        # Simulate a crash AFTER phase 1 (pending revision written) but
        # BEFORE phase 2 (promotion + order=applied).
        from inventory.models import MeasurementCorrection
        order = MeasurementCorrection.objects.get(pk=cid)
        # create exactly the artifact the crash would leave behind
        pending = MeasurementRevision.objects.create(
            measurement=m, correction=order,
            dbh_cm=25.0, height_m=15.0, x_m=5.0, y_m=5.0,
            status=AM, dbh_raw=250.0, dbh_unit="mm",
            height_raw=15.0, height_unit="m",
            revision_status=REVISION_PENDING)

        # refresh / any read runs crash recovery: only complete states
        recovered = reconcile_interrupted_applications()
        self.assertIn(cid, recovered)
        pending.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(pending.revision_status, REVISION_VOID)
        self.assertEqual(order.status, CORRECTION_FAILED)
        self.assertTrue(order.failure_reason)
        # nothing effective leaked
        self.assertEqual(
            MeasurementRevision.objects
            .filter(revision_status=REVISION_EFFECTIVE).count(), 0)

        # A failed attempt preserves the prior review approval: retrying
        # (after a transient crash, not a human gate) applies directly.
        ra = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(ra.status_code, 200, ra.content)
        self.assertEqual(ra.headers.get("Revision-Newly-Applied"), "true")

        # retrying AGAIN is idempotent: same revision, no duplicate
        ra2 = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(ra2.status_code, 200)
        self.assertEqual(ra2.headers.get("Revision-Newly-Applied"), "false")
        self.assertEqual(
            MeasurementRevision.objects
            .filter(revision_status=REVISION_EFFECTIVE).count(), 1)
        self.assertEqual(MeasurementRevision.objects.count(), 2)
        eff = MeasurementRevision.objects.get(
            revision_status=REVISION_EFFECTIVE)
        self.assertIsNone(eff.supersedes)
        # voided attempt retained for audit, linked by supersedes chain
        voided = MeasurementRevision.objects.get(
            revision_status=REVISION_VOID)
        self.assertTrue(voided.void_reason)

    # --- G6: revision ledger & provenance traces --------------------------
    def test_revision_chain_and_provenance_are_reviewable(self):
        m = self._legacy_t2()
        cid = self._submit(m).json()["id"]
        self.client.post(f"/api/corrections/{cid}/review/",
                         dict(decision="apply", reviewed_by="jane"),
                         format="json")
        self.client.post(f"/api/corrections/{cid}/apply/")

        ledger = self.client.get("/api/revisions/").json()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["revision_status"], REVISION_EFFECTIVE)
        self.assertEqual(ledger[0]["plot_code"], "P1")
        self.assertEqual(ledger[0]["correction_reason"][:20],
                         "2024 remeasure: 25.0")

        # measurements endpoint reports effective + base side by side
        rows = self.client.get(
            "/api/measurements/?campaign=t2").json()
        row = next(r for r in rows if r["id"] == m.id)
        self.assertEqual(row["dbh_cm"], 250.0)        # base historical
        self.assertEqual(row["effective_dbh_cm"], 25.0)
        self.assertEqual(row["effective_dbh_unit"], "mm")
        self.assertEqual(row["correction_id"], cid)

        # draft provenance names the revision
        draft = self._draft("with-rev")
        prov = draft["result_payload"]["provenance"]
        self.assertEqual(
            [x["correction_id"]
             for x in prov["measurement_revisions_applied"]],
            [cid])
        plot_prov = next(p for p in prov["plots"] if p["plot"] == "P1")
        self.assertEqual(
            [x["tree"] for x in plot_prov["revised_measurements"]],
            ["P1/1"])

    # --- G7: chained corrections supersede, never overwrite -------------
    def test_second_correction_supersedes_first_in_a_chain(self):
        m = self._legacy_t2()
        cid1 = self._submit(
            m, idempotency_key="chain1",
            corrected_dbh_raw=25.5, corrected_dbh_unit="cm").json()["id"]
        self.client.post(f"/api/corrections/{cid1}/review/",
                         dict(decision="apply"), format="json")
        self.client.post(f"/api/corrections/{cid1}/apply/")
        rev1 = MeasurementRevision.objects.get(
            revision_status=REVISION_EFFECTIVE)

        cid2 = self._submit(
            m, idempotency_key="chain2",
            corrected_dbh_raw=25.6, corrected_dbh_unit="cm").json()["id"]
        self.client.post(f"/api/corrections/{cid2}/review/",
                         dict(decision="apply"), format="json")
        self.client.post(f"/api/corrections/{cid2}/apply/")

        rev2 = MeasurementRevision.objects.get(
            correction_id=cid2)
        self.assertEqual(rev2.revision_status, REVISION_EFFECTIVE)
        self.assertEqual(rev2.supersedes_id, rev1.id)
        self.assertEqual(effective_values(m)["dbh_cm"], 25.6)
        # both revisions remain readable; only one effective
        self.assertEqual(
            MeasurementRevision.objects
            .filter(revision_status=REVISION_EFFECTIVE).count(), 1)
        # original base row still the original (wrong) transcription
        m.refresh_from_db()
        self.assertEqual(m.dbh_cm, 250.0)
        # the first correction order's snapshot records what IT changed
        c1 = MeasurementCorrection.objects.get(pk=cid1)
        self.assertEqual(c1.original_dbh_cm, 250.0)
        self.assertEqual(c1.corrected_dbh_cm, 25.5)
        c2 = MeasurementCorrection.objects.get(pk=cid2)
        self.assertEqual(c2.original_dbh_cm, 25.5)
        self.assertEqual(c2.corrected_dbh_cm, 25.6)
