"""
REST API:

GET  /plots/                        plot positions + boundaries (React map)
GET  /trees/?campaign=CODE          individuals and remeasurement status
GET  /conflicts/                    same-number position contradictions
POST /conflicts/{id}/resolve/       human verification only
POST /imports/                      ingest a campaign's field rows
POST /estimates/                    run (or rerun) a DRAFT estimate
POST /estimates/{id}/confirm/       freeze forever; locks equations
GET  /estimates/{id}/               frozen result with provenance
POST /corrections/                  file a measurement correction order
POST /corrections/{id}/review/      approve (-> reviewed) or reject
POST /corrections/{id}/apply/       append revision + rescan identity
GET  /corrections/{id}/impact/      before/after, blocked items, editions
POST /corrections/{id}/recompute/   new DRAFT using the revision + diff
"""
from django.conf import settings
from django.core.exceptions import ValidationError as DjValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_DISTINCT,
    CONFLICT_OPEN,
    CONFLICT_RENUMBER,
    CORRECTION_APPLIED,
    CORRECTION_FAILED,
    EstimateVersion,
    IdentityConflict,
    MeasurementCorrection,
    Plot,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
    VERSION_CONFIRMED,
)
from inventory.serializers import (
    CampaignSerializer,
    ConflictResolveSerializer,
    ConflictSerializer,
    CorrectionRecomputeSerializer,
    CorrectionReviewSerializer,
    CorrectionSubmitSerializer,
    EquationSerializer,
    EstimateVersionSerializer,
    MeasurementCorrectionSerializer,
    MeasurementImportSerializer,
    MeasurementSerializer,
    PlotSerializer,
    SpeciesSerializer,
    StratumSerializer,
    TreeSerializer,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.corrections import (
    CorrectionStateError,
    StaleSnapshotError,
    apply_correction,
    impact_report,
    mark_apply_failed,
    review_correction,
    submit_correction,
)
from inventory.services.estimates import (
    diff_result_payloads,
    run_draft_estimate,
)
from inventory.services.estimator import equation_checksum
from inventory.services.ingest import import_campaign_rows


class StratumViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Stratum.objects.all()
    serializer_class = StratumSerializer


class SpeciesViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Species.objects.all()
    serializer_class = SpeciesSerializer


class CampaignViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Campaign.objects.all()
    serializer_class = CampaignSerializer


class EquationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AllometricEquation.objects.prefetch_related("species").all()
    serializer_class = EquationSerializer


class PlotViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Plot.objects.select_related("stratum").all()
    serializer_class = PlotSerializer


class TreeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TreeSerializer

    def get_queryset(self):
        qs = Tree.objects.select_related("plot", "species", "superseded_tree")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(measurements__campaign__code=campaign).distinct()
        plot = self.request.query_params.get("plot")
        if plot:
            qs = qs.filter(plot__code=plot)
        return qs


class MeasurementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MeasurementSerializer

    def get_queryset(self):
        qs = TreeMeasurement.objects.select_related("tree", "tree__plot",
                                                    "campaign")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        return qs


class ConflictViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ConflictSerializer

    def get_queryset(self):
        qs = IdentityConflict.objects.select_related("plot")
        state = self.request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Human-in-the-loop resolution. Nothing here is automatic."""
        conflict = self.get_object()
        ser = ConflictResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if conflict.status != CONFLICT_OPEN:
            return Response(
                {"detail": f"conflict already resolved as {conflict.status}; "
                           "verification cannot be undone here."},
                status=status.HTTP_409_CONFLICT,
            )
        decision = ser.validated_data["status"]
        with transaction.atomic():
            if decision == CONFLICT_RENUMBER:
                # same individual: t2's tree row becomes a successor of t1's
                t2_tree = conflict.t2_measurement.tree
                t1_tree = conflict.t1_measurement.tree
                if t2_tree != t1_tree:
                    t2_tree.superseded_tree = t1_tree
                    t2_tree.current_field_number = (
                        conflict.t2_measurement.field_number_seen
                    )
                    t2_tree.save(update_fields=["superseded_tree",
                                               "current_field_number"])
            # distinct: do nothing — t1 and t2 rows stay separate and enter
            # mortality / ingrowth candidates respectively.
            conflict.status = decision
            conflict.resolution_note = ser.validated_data.get("note", "")
            conflict.resolved_at = timezone.now()
            conflict.save()
        return Response(ConflictSerializer(conflict).data)


class ImportViewSet(viewsets.ViewSet):
    def create(self, request):
        ser = MeasurementImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        campaign = Campaign.objects.filter(
            code=ser.validated_data["campaign"]
        ).first()
        if campaign is None:
            return Response({"detail": "unknown campaign"},
                            status=status.HTTP_404_NOT_FOUND)
        result = import_campaign_rows(
            campaign, ser.validated_data["rows"],
            area_tolerance=settings.PLOT_AREA_TOLERANCE,
        )

        # re-scan identity contradictions against the other campaign
        other = Campaign.objects.exclude(pk=campaign.pk).order_by(
            "measured_on").first()
        if other:
            t1, t2 = sorted([campaign, other], key=lambda c: c.measured_on)
            result["conflicts"] = scan_conflicts(t1, t2)
        return Response(result,
                        status=status.HTTP_207_MULTI_STATUS if result["rejected"]
                        else status.HTTP_200_OK)


class EstimateViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = EstimateVersion.objects.all().order_by("-created_at")
        return Response(EstimateVersionSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(
            EstimateVersionSerializer(_get_version(pk)).data
        )

    def create(self, request):
        """
        Body: {"label": ..., "t1_campaign": CODE, "t2_campaign": CODE,
               "equation_ids": [...], "fpc": true}
        Creates (or recomputes) a DRAFT. Confirmation is a separate action.
        """
        label = request.data.get("label", "draft estimate")
        t1 = Campaign.objects.filter(
            code=request.data.get("t1_campaign")).first()
        t2 = Campaign.objects.filter(
            code=request.data.get("t2_campaign")).first()
        if not t1 or not t2 or t1.measured_on >= t2.measured_on:
            return Response(
                {"detail": "need t1 earlier than t2 campaign codes"},
                status=status.HTTP_400_BAD_REQUEST)
        eq_ids = request.data.get("equation_ids", [])
        equations_qs = AllometricEquation.objects.filter(
            id__in=eq_ids
        ).prefetch_related("species")
        if equations_qs.count() != len(eq_ids) or not eq_ids:
            return Response({"detail": "equation_ids invalid/empty"},
                            status=status.HTTP_400_BAD_REQUEST)

        version = run_draft_estimate(
            label, t1, t2, equations_qs,
            fpc=bool(request.data.get("fpc", True)),
        )
        return Response(EstimateVersionSerializer(version).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Freeze the edition forever and lock its equations."""
        version = _get_version(pk)
        if version.status == VERSION_CONFIRMED:
            return Response({"detail": "already confirmed"},
                            status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            # re-verify checksum: equations must not have drifted since run
            from inventory.services.estimator import build_measurement_table
            eqs = version.equations.all().prefetch_related("species")
            equations = {}
            for e in eqs:
                for sp in e.species.all():
                    equations[sp.code] = {
                        "code": e.code, "version": e.version,
                        "a": e.a, "b": e.b, "c": e.c,
                        "dbh_min_cm": e.dbh_min_cm,
                        "dbh_max_cm": e.dbh_max_cm,
                        "height_required": e.height_required,
                        "residual_sigma": e.residual_sigma,
                        "citation": e.citation,
                    }
            current = equation_checksum(equations)
            if current != version.equation_checksum:
                return Response(
                    {"detail": "equations changed since the run; create a "
                               "new version rather than confirming stale "
                               "numbers."},
                    status=status.HTTP_409_CONFLICT)
            version.status = VERSION_CONFIRMED
            version.confirmed_at = timezone.now()
            version.save()
            # Lock the equations: a confirmed edition's equation is frozen
            # and a new coefficient set must be issued as a new equation row.
            from inventory.models import EQUATION_CONFIRMED
            eqs.update(status=EQUATION_CONFIRMED)
        return Response(EstimateVersionSerializer(version).data)


def _get_version(pk):
    from django.shortcuts import get_object_or_404
    return get_object_or_404(
        EstimateVersion.objects.prefetch_related("equations"), pk=pk)


class CorrectionViewSet(viewsets.GenericViewSet,
                        viewsets.mixins.ListModelMixin,
                        viewsets.mixins.RetrieveModelMixin):
    """
    Measurement correction orders (测量更正单). The historical
    TreeMeasurement rows are never edited; an applied order appends an
    immutable revision that only NEW draft estimates pick up.
    """
    serializer_class = MeasurementCorrectionSerializer

    def get_queryset(self):
        qs = (MeasurementCorrection.objects
              .select_related("measurement__tree__plot",
                              "measurement__campaign")
              .prefetch_related("revision"))
        state = self.request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        measurement = self.request.query_params.get("measurement")
        if measurement:
            qs = qs.filter(measurement_id=measurement)
        plot = self.request.query_params.get("plot")
        if plot:
            qs = qs.filter(measurement__tree__plot__code=plot)
        return qs

    def create(self, request):
        """File an order. Same idempotency_key -> same order, never two."""
        ser = CorrectionSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        # Idempotency is checked BEFORE anything else: a replayed
        # submission returns the original order; reusing the key with a
        # DIFFERENT payload is a conflict, never a second order.
        existing = MeasurementCorrection.objects.filter(
            idempotency_key=data["idempotency_key"]).first()
        if existing is not None:
            same = (existing.measurement_id == data["measurement_id"]
                    and existing.corrected == data["corrected"])
            if not same:
                return Response(
                    {"detail": "idempotency_key already filed with a "
                               "different measurement/payload"},
                    status=status.HTTP_409_CONFLICT)
            payload = MeasurementCorrectionSerializer(existing).data
            payload["created"] = False
            return Response(payload, status=status.HTTP_200_OK)

        measurement = TreeMeasurement.objects.filter(
            pk=data["measurement_id"]).first()
        if measurement is None:
            return Response({"detail": "unknown measurement"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            order, created = submit_correction(
                measurement,
                idempotency_key=data["idempotency_key"],
                corrected=data["corrected"],
                reason=data["reason"],
                evidence=data["evidence"],
            )
        except DjValidationError as exc:
            return Response({"detail": "; ".join(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)
        payload = MeasurementCorrectionSerializer(order).data
        payload["created"] = created
        return Response(payload,
                        status=status.HTTP_201_CREATED if created
                        else status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """Human review: {"decision": "approve"|"reject", "note": ...}."""
        order = self.get_object()
        ser = CorrectionReviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            review_correction(
                order,
                approve=ser.validated_data["decision"] == "approve",
                note=ser.validated_data.get("note", ""),
            )
        except CorrectionStateError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        return Response(MeasurementCorrectionSerializer(order).data)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        """
        Append the revision and re-scan identity contradictions.
        Atomic: a failure leaves NO partial revision, the order is marked
        failed, and retrying never creates a second revision.
        """
        order = self.get_object()
        if order.status == CORRECTION_APPLIED:
            # idempotent: already applied, return the existing revision
            return Response(MeasurementCorrectionSerializer(order).data)
        try:
            revision, conflicts = apply_correction(order)
        except StaleSnapshotError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        except CorrectionStateError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        except DjValidationError as exc:
            return Response({"detail": "; ".join(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # rolled back; record a clean failure
            order = mark_apply_failed(order, exc)
            return Response(
                {"detail": f"apply failed and was rolled back cleanly: "
                           f"{exc}",
                 "status": CORRECTION_FAILED},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        order.refresh_from_db()
        payload = MeasurementCorrectionSerializer(order).data
        payload["identity_rescan"] = conflicts
        return Response(payload)

    @action(detail=True, methods=["get"])
    def impact(self, request, pk=None):
        """Before/after values, blocked identity items, affected editions."""
        return Response(impact_report(self.get_object()))

    @action(detail=True, methods=["post"])
    def recompute(self, request, pk=None):
        """
        Run a NEW draft estimate that uses this order's revision, and
        diff it against a frozen edition. Confirmed versions are never
        recomputed — their payload, checksum and provenance stay frozen.
        """
        order = self.get_object()
        if order.status != CORRECTION_APPLIED:
            return Response(
                {"detail": "recompute requires an applied correction "
                           f"(status is {order.status})."},
                status=status.HTTP_409_CONFLICT)
        ser = CorrectionRecomputeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        eq_ids = data["equation_ids"]
        equations_qs = AllometricEquation.objects.filter(
            id__in=eq_ids).prefetch_related("species")
        if equations_qs.count() != len(eq_ids):
            return Response({"detail": "equation_ids invalid"},
                            status=status.HTTP_400_BAD_REQUEST)

        campaign = order.measurement.campaign
        other = Campaign.objects.exclude(pk=campaign.pk).order_by(
            "measured_on").first()
        if other is None:
            return Response({"detail": "need a second campaign to compare"},
                            status=status.HTTP_400_BAD_REQUEST)
        t1, t2 = sorted([campaign, other], key=lambda c: c.measured_on)

        label = data.get("label") or (
            f"recompute after correction #{order.pk}")
        version = run_draft_estimate(label, t1, t2, equations_qs,
                                     fpc=data.get("fpc", True))

        compare_to = None
        if data.get("compare_to"):
            compare_to = EstimateVersion.objects.filter(
                pk=data["compare_to"]).first()
        if compare_to is None:
            compare_to = (EstimateVersion.objects
                          .filter(status=VERSION_CONFIRMED)
                          .order_by("-confirmed_at").first())
        diff = None
        if compare_to is not None and compare_to.result_payload:
            diff = diff_result_payloads(compare_to.result_payload,
                                        version.result_payload)
            diff["compared_to"] = {"id": compare_to.id,
                                   "label": compare_to.label,
                                   "status": compare_to.status}
        return Response({
            "version": EstimateVersionSerializer(version).data,
            "diff": diff,
        }, status=status.HTTP_201_CREATED)
