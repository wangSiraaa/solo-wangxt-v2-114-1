"""
Acceptance tests for the permanent-plot station.

Covers the acceptance checks specified by the station:
  A. remeasurement renumber handling;
  B. unequal plot areas in population expansion;
  C. measurement-unit mistakes rejected at ingest;
  D. real zero growth vs missing data vs mortality kept distinct;
  E. same number + contradictory position never auto-merged;
  F. confirmed estimate editions cannot be silently changed by a new
     allometric equation.
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
    EstimateVersion,
    Plot,
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


class CorrectionAcceptanceTests(TestCase):
    """
    Acceptance tests for the measurement correction order (测量更正单)
    closed loop: pending -> reviewed -> applied/rejected, append-only
    revisions, identity re-scan, frozen confirmed editions.
    """

    def setUp(self):
        self.client = APIClient()
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=1.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)

    # ---- helpers -------------------------------------------------------------
    def _import(self, campaign, rows):
        return import_campaign_rows(campaign, rows, 0.01)

    def _measurement(self, campaign, field_number):
        return TreeMeasurement.objects.get(
            campaign=campaign, field_number_seen=field_number)

    def _run_estimate(self, label="v"):
        r = self.client.post("/api/estimates/",
                             dict(label=label, t1_campaign="t1",
                                  t2_campaign="t2",
                                  equation_ids=[self.eq.id], fpc=False),
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def _file_correction(self, measurement, key, corrected, **kw):
        body = dict(measurement_id=measurement.id, idempotency_key=key,
                    corrected=corrected,
                    reason=kw.get("reason", "field re-check"),
                    evidence=kw.get("evidence", "sheet FC-1"))
        return self.client.post("/api/corrections/", body, format="json")

    def _review_apply(self, cid):
        r = self.client.post(f"/api/corrections/{cid}/review/",
                             {"decision": "approve"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.client.post(f"/api/corrections/{cid}/apply/")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def _survivor_growth(self, version_json):
        return (version_json["result_payload"]["components"]
                ["survivor_growth"]["total_kg"])

    # ---- 1. applied unit correction: new draft moves, confirmed frozen -------
    def test_applied_unit_correction_changes_new_draft_confirmed_untouched(self):
        # t2 dbh recorded as "25.0 mm" -> canonical 2.5 cm (unit mis-entry);
        # the field sheet said 25.0 cm.
        self._import(self.t1, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=24.0, dbh_unit="cm",
            height_raw=15.0, height_unit="m")])
        self._import(self.t2, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=25.0, dbh_unit="mm",
            height_raw=15.5, height_unit="m")])
        m = self._measurement(self.t2, "1")
        self.assertAlmostEqual(m.dbh_cm, 2.5)

        v1 = self._run_estimate("confirmed-with-wrong-unit")
        self.client.post(f"/api/estimates/{v1['id']}/confirm/")
        frozen_payload = self.client.get(
            f"/api/estimates/{v1['id']}/").json()["result_payload"]

        cid = self._file_correction(
            m, "fc-1", {"dbh_raw": 25.0, "dbh_unit": "cm"},
            reason="sheet reads 25.0 cm, mis-keyed as 25.0 mm").json()["id"]
        applied = self._review_apply(cid)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["revision"]["sequence"], 1)
        self.assertEqual(applied["revision"]["dbh_cm"], 25.0)
        # the historical row is NOT overwritten
        m.refresh_from_db()
        self.assertEqual((m.dbh_raw, m.dbh_unit, m.dbh_cm),
                         (25.0, "mm", 2.5))

        v2 = self._run_estimate("draft-after-correction")
        self.assertNotEqual(self._survivor_growth(v2),
                            self._survivor_growth(v1))
        # the draft records exactly which revision it used
        revs = v2["result_payload"]["provenance"]["applied_revisions"]
        self.assertEqual([r["correction_id"] for r in revs], [cid])
        self.assertEqual(
            v2["design_snapshot"]["applied_revision_ids"],
            [applied["revision"]["id"]])

        # the confirmed edition is byte-stable and still re-readable
        again = self.client.get(f"/api/estimates/{v1['id']}/").json()
        self.assertEqual(again["status"], "confirmed")
        self.assertEqual(again["result_payload"], frozen_payload)
        self.assertEqual(again["equation_checksum"], v1["equation_checksum"])

    # ---- 2. idempotent submission ---------------------------------------------
    def test_same_idempotency_key_files_one_order_only(self):
        self._import(self.t2, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=25.0, dbh_unit="cm",
            height_raw=15.0, height_unit="m")])
        m = self._measurement(self.t2, "1")
        r1 = self._file_correction(m, "dup-key", {"dbh_raw": 26.0,
                                                  "dbh_unit": "cm"})
        r2 = self._file_correction(m, "dup-key", {"dbh_raw": 26.0,
                                                  "dbh_unit": "cm"})
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertFalse(r2.json()["created"])
        from inventory.models import MeasurementCorrection
        self.assertEqual(MeasurementCorrection.objects.count(), 1)
        # the same key with a DIFFERENT payload is a conflict, not an order
        r3 = self._file_correction(m, "dup-key", {"dbh_raw": 99.0,
                                                  "dbh_unit": "cm"})
        self.assertEqual(r3.status_code, 409)
        self.assertEqual(MeasurementCorrection.objects.count(), 1)

    # ---- 3. contradictory reviews cannot both be applied ----------------------
    def test_contradictory_corrections_cannot_both_be_applied(self):
        self._import(self.t2, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=25.0, dbh_unit="cm",
            height_raw=15.0, height_unit="m")])
        m = self._measurement(self.t2, "1")
        a = self._file_correction(m, "corr-a", {"dbh_raw": 26.0,
                                                "dbh_unit": "cm"}).json()
        b = self._file_correction(m, "corr-b", {"dbh_raw": 27.0,
                                                "dbh_unit": "cm"}).json()
        for cid in (a["id"], b["id"]):
            r = self.client.post(f"/api/corrections/{cid}/review/",
                                 {"decision": "approve"}, format="json")
            self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/corrections/{a['id']}/apply/")
        self.assertEqual(r.status_code, 200, r.content)
        # B was filed against the same original snapshot, now stale
        r = self.client.post(f"/api/corrections/{b['id']}/apply/")
        self.assertEqual(r.status_code, 409, r.content)
        b_state = self.client.get(f"/api/corrections/{b['id']}/").json()
        self.assertEqual(b_state["status"], "reviewed")  # not applied
        from inventory.models import MeasurementRevision
        self.assertEqual(
            MeasurementRevision.objects.filter(measurement=m).count(), 1)
        self.assertEqual(
            MeasurementRevision.objects.get(measurement=m).dbh_cm, 26.0)

    # ---- 4. coordinate correction opens a conflict; no bypass -----------------
    def test_coordinate_correction_opens_conflict_and_stays_blocked(self):
        # t1 tree 7 at (10,10); not re-found at t2. t2 tree 8 recorded far
        # away at (30,10) -> ingrowth candidate.
        self._import(self.t1, [dict(
            plot="P1", field_number="7", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=20.0, dbh_unit="cm",
            height_raw=14.0, height_unit="m")])
        self._import(self.t2, [dict(
            plot="P1", field_number="8", species="OAK", x_m=30, y_m=10,
            status=AM, dbh_raw=12.0, dbh_unit="cm",
            height_raw=10.0, height_unit="m")])
        before = self._run_estimate("before-move")
        p1 = next(p for p in before["result_payload"]["provenance"]["plots"]
                  if p["plot"] == "P1")
        self.assertEqual([i["tree"] for i in p1["ingrowth"]], ["P1/8"])

        # field re-check: tree 8's true position is 0.45 m from tree 7's
        # t1 position -> possible unrecorded renumber, must be verified.
        m8 = self._measurement(self.t2, "8")
        cid = self._file_correction(
            m8, "fc-coord", {"x_m": 10.4, "y_m": 10.2},
            reason="GPS offset; true fix next to t1 tag 7").json()["id"]
        applied = self._review_apply(cid)
        rescan = applied["identity_rescan"]
        self.assertTrue(any(c["created"] for c in rescan), rescan)

        from inventory.models import IdentityConflict
        conflict = IdentityConflict.objects.get(status="open")
        self.assertEqual(conflict.t2_measurement_id, m8.id)

        # the pair is now BLOCKED: out of every component until a human
        # verifies — the correction did not merge or count anything.
        after = self._run_estimate("after-move")
        prov = after["result_payload"]["provenance"]
        self.assertEqual(len(prov["open_conflicts"]), 1)
        self.assertEqual(prov["open_conflicts"][0]["hint"],
                         "possible_renumber")
        p1 = next(p for p in prov["plots"] if p["plot"] == "P1")
        self.assertEqual(p1["ingrowth"], [])
        self.assertEqual(p1["mortality"], [])
        self.assertEqual(p1["kg"]["survivor_growth"], 0.0)
        self.assertEqual(
            [c["tree"] for c in p1["excluded_identity_conflicts"]],
            ["P1/7"])

    # ---- 5. interrupted apply: clean state, safe retry ------------------------
    def test_failed_apply_rolls_back_and_retry_never_duplicates(self):
        self._import(self.t2, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=25.0, dbh_unit="cm",
            height_raw=15.0, height_unit="m")])
        m = self._measurement(self.t2, "1")
        cid = self._file_correction(m, "fc-fail", {"dbh_raw": 26.0,
                                                   "dbh_unit": "cm"}).json()
        self.client.post(f"/api/corrections/{cid['id']}/review/",
                         {"decision": "approve"}, format="json")

        # simulate a crash mid-apply (identity re-scan blows up)
        import inventory.services.corrections as corr_mod
        original = corr_mod.scan_conflicts
        corr_mod.scan_conflicts = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("simulated power cut"))
        try:
            r = self.client.post(f"/api/corrections/{cid['id']}/apply/")
        finally:
            corr_mod.scan_conflicts = original
        self.assertEqual(r.status_code, 500)

        # after "refresh": a complete failed state, nothing half-written
        from inventory.models import MeasurementRevision
        state = self.client.get(f"/api/corrections/{cid['id']}/").json()
        self.assertEqual(state["status"], "failed")
        self.assertIsNone(state["revision"])
        self.assertEqual(MeasurementRevision.objects.count(), 0)
        m.refresh_from_db()
        self.assertEqual(m.dbh_cm, 25.0)  # base row untouched

        # retry succeeds and creates exactly ONE effective revision
        r = self.client.post(f"/api/corrections/{cid['id']}/apply/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(MeasurementRevision.objects.count(), 1)
        # applying again is idempotent — still one revision
        r = self.client.post(f"/api/corrections/{cid['id']}/apply/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(MeasurementRevision.objects.count(), 1)
        self.assertEqual(MeasurementRevision.objects.get().sequence, 1)

    # ---- 6. impact view + recompute-by-order ----------------------------------
    def test_impact_view_and_recompute_by_correction(self):
        self._import(self.t1, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=24.0, dbh_unit="cm",
            height_raw=15.0, height_unit="m")])
        self._import(self.t2, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=25.0, dbh_unit="mm",
            height_raw=15.5, height_unit="m")])
        m = self._measurement(self.t2, "1")
        v1 = self._run_estimate("confirmed")
        self.client.post(f"/api/estimates/{v1['id']}/confirm/")

        cid = self._file_correction(
            m, "fc-impact", {"dbh_raw": 25.0, "dbh_unit": "cm"}).json()["id"]

        # impact BEFORE apply: shows the pending diff and the frozen edition
        imp = self.client.get(f"/api/corrections/{cid}/impact/").json()
        self.assertEqual(imp["current_effective"]["dbh_cm"], 2.5)
        self.assertEqual(imp["projected_after_apply"]["dbh_cm"], 25.0)
        self.assertIn("dbh_cm", imp["changed_fields"])
        self.assertEqual(imp["estimate_versions"][0]["id"], v1["id"])
        self.assertIn("frozen", imp["estimate_versions"][0]["effect"])

        # recompute is refused before the order is applied
        r = self.client.post(f"/api/corrections/{cid}/recompute/",
                             {"equation_ids": [self.eq.id]}, format="json")
        self.assertEqual(r.status_code, 409)

        self._review_apply(cid)
        r = self.client.post(f"/api/corrections/{cid}/recompute/",
                             {"equation_ids": [self.eq.id],
                              "label": "after unit fix"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        new_version = r.json()["version"]
        diff = r.json()["diff"]
        self.assertEqual(new_version["status"], "draft")
        self.assertEqual(diff["compared_to"]["id"], v1["id"])
        delta = diff["components"]["survivor_growth"]["delta_kg"]
        self.assertNotEqual(delta, 0.0)
        self.assertEqual(diff["components"]["survivor_growth"]
                         ["before_total_kg"],
                         round(self._survivor_growth(v1), 3))
        # confirmed edition still untouched after the recompute
        again = self.client.get(f"/api/estimates/{v1['id']}/").json()
        self.assertEqual(self._survivor_growth(again),
                         self._survivor_growth(v1))

    # ---- guards ----------------------------------------------------------------
    def test_correction_validates_values_and_workflow(self):
        self._import(self.t2, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=25.0, dbh_unit="cm",
            height_raw=15.0, height_unit="m")])
        m = self._measurement(self.t2, "1")
        # 250 cm fails the plausible-range check, same as at ingest
        r = self._file_correction(m, "bad-1", {"dbh_raw": 250.0,
                                               "dbh_unit": "cm"})
        self.assertEqual(r.status_code, 400)
        # coordinates outside the plot boundary are refused
        r = self._file_correction(m, "bad-2", {"x_m": 500.0, "y_m": 500.0})
        self.assertEqual(r.status_code, 400)
        # a bare number without its unit is never accepted
        r = self._file_correction(m, "bad-3", {"dbh_raw": 26.0,
                                               "dbh_unit": None})
        self.assertEqual(r.status_code, 400)
        # apply before review is a workflow conflict
        ok = self._file_correction(m, "ok-1", {"dbh_raw": 26.0,
                                               "dbh_unit": "cm"}).json()
        r = self.client.post(f"/api/corrections/{ok['id']}/apply/")
        self.assertEqual(r.status_code, 409)
        # rejected orders cannot be applied either
        self.client.post(f"/api/corrections/{ok['id']}/review/",
                         {"decision": "reject"}, format="json")
        r = self.client.post(f"/api/corrections/{ok['id']}/apply/")
        self.assertEqual(r.status_code, 409)
        from inventory.models import MeasurementRevision
        self.assertEqual(MeasurementRevision.objects.count(), 0)

    def test_revision_is_append_only(self):
        self._import(self.t2, [dict(
            plot="P1", field_number="1", species="OAK", x_m=10, y_m=10,
            status=AM, dbh_raw=25.0, dbh_unit="cm",
            height_raw=15.0, height_unit="m")])
        m = self._measurement(self.t2, "1")
        cid = self._file_correction(m, "fc-immutable",
                                    {"dbh_raw": 26.0,
                                     "dbh_unit": "cm"}).json()["id"]
        self._review_apply(cid)
        from inventory.models import MeasurementRevision
        rev = MeasurementRevision.objects.get()
        rev.dbh_cm = 99.0
        with self.assertRaises(PermissionError):
            rev.save()
        with self.assertRaises(PermissionError):
            rev.delete()
