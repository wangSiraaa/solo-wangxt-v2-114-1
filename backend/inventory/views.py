"""
REST API:

GET  /plots/                        plot positions + boundaries (React map)
GET  /trees/?campaign=CODE          individuals and remeasurement status
GET  /measurements/?campaign=CODE   one row per tree/occasion, with the
                                    EFFECTIVE view (revisions overlaid)
GET  /conflicts/                    same-number position contradictions
POST /conflicts/{id}/resolve/       human verification only
POST /imports/                      ingest a campaign's field rows
POST /estimates/                    run (or rerun) a DRAFT estimate
POST /estimates/{id}/confirm/       freeze forever; locks equations
GET  /estimates/{id}/               frozen result with provenance

Measurement correction orders (测量更正单):
GET  /corrections/                  list/filter correction orders
POST /corrections/                  submit (pending; idempotency key)
POST /corrections/{id}/review/      supervisor apply/reject decision
GET  /corrections/{id}/impact/      impact scope + identity blockers
POST /corrections/{id}/apply/       append the effective revision (retryable)
POST /corrections/{id}/recompute/   new DRAFT edition using that revision
GET  /revisions/                    traceable revision ledger
"""
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    CONFLICT_RENUMBER,
    EstimateVersion,
    IdentityConflict,
    MeasurementCorrection,
    MeasurementRevision,
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
    CorrectionReviewSerializer,
    EquationSerializer,
    EstimateVersionSerializer,
    MeasurementCorrectionSerializer,
    MeasurementImportSerializer,
    MeasurementRevisionSerializer,
    MeasurementSerializer,
    PlotSerializer,
    SpeciesSerializer,
    StratumSerializer,
    TreeSerializer,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.corrections import (
    CorrectionError,
    apply_correction,
    impact_summary,
    recompute_with_correction,
    review_correction,
    submit_correction,
)
from inventory.services.ingest import import_campaign_rows
from inventory.services.revisions import (
    annotate_queryset,
    reconcile_interrupted_applications,
)


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
        qs = TreeMeasurement.objects.select_related(
            "tree", "tree__plot", "campaign")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        plot = self.request.query_params.get("plot")
        if plot:
            qs = qs.filter(tree__plot__code=plot)
        return qs

    def list(self, request, *args, **kwargs):
        # attach effective revisions in one pass (never N+1 per row)
        instances = annotate_queryset(list(self.get_queryset()))
        ser = self.get_serializer(instances, many=True)
        return Response(ser.data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        annotate_queryset([instance])
        return Response(self.get_serializer(instance).data)


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
        qs = list(EstimateVersion.objects.all().order_by("-created_at"))
        rev_ids = {
            v.id: list(v.measurement_revisions.values_list("id", flat=True))
            for v in qs
        }
        data = EstimateVersionSerializer(qs, many=True).data
        for row in data:
            row["measurement_revision_ids"] = rev_ids.get(row["id"], [])
        return Response(data)

    def retrieve(self, request, pk=None):
        version = _get_version(pk)
        ser = EstimateVersionSerializer(version)
        payload = ser.data
        payload["measurement_revision_ids"] = list(
            version.measurement_revisions.values_list("id", flat=True))
        return Response(payload)

    def create(self, request):
        """
        Body: {"label": ..., "t1_campaign": CODE, "t2_campaign": CODE,
               "equation_ids": [...], "fpc": true}
        Creates (or recomputes) a DRAFT from current EFFECTIVE measurements.
        Confirmation is a separate action.
        """
        t1 = Campaign.objects.filter(
            code=request.data.get("t1_campaign")).first()
        t2 = Campaign.objects.filter(
            code=request.data.get("t2_campaign")).first()
        if not t1 or not t2:
            return Response(
                {"detail": "need t1 earlier than t2 campaign codes"},
                status=status.HTTP_400_BAD_REQUEST)
        eq_ids = request.data.get("equation_ids", [])
        from inventory.services.estimates_run import run_draft
        try:
            version = run_draft(
                label=request.data.get("label", "draft estimate"),
                t1=t1, t2=t2, equation_ids=eq_ids,
                fpc=bool(request.data.get("fpc", True)))
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
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
            from inventory.services.estimator import equation_checksum
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


def _correction_error_response(exc):
    return Response({"detail": exc.detail}, status=exc.status_code)


class CorrectionViewSet(viewsets.ViewSet):
    """
    Measurement correction orders (测量更正单).

    submit -> review (apply/reject) -> apply (append revision, re-scan
    identity) -> recompute a NEW draft. Confirmed editions are never
    touched by any of these actions.
    """

    def list(self, request):
        # Any read heals interrupted applications first: a refresh must
        # expose only complete pending/failed states.
        reconcile_interrupted_applications()
        qs = (
            MeasurementCorrection.objects
            .select_related(
                "measurement", "measurement__tree",
                "measurement__tree__plot", "measurement__campaign")
            .prefetch_related("revision_attempts")
            .order_by("-submitted_at"))
        state = request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        measurement = request.query_params.get("measurement")
        if measurement:
            qs = qs.filter(measurement_id=measurement)
        return Response(
            MeasurementCorrectionSerializer(list(qs), many=True).data)

    def retrieve(self, request, pk=None):
        reconcile_interrupted_applications()
        order = get_object_or_404(MeasurementCorrection, pk=pk)
        return Response(MeasurementCorrectionSerializer(order).data)

    def create(self, request):
        """Submit a correction order (pending). Idempotent by key."""
        data = request.data
        measurement_id = data.get("measurement")
        measurement = (
            TreeMeasurement.objects
            .select_related("tree__plot").filter(pk=measurement_id).first())
        if measurement is None:
            return Response({"detail": "measurement not found"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            order, created = submit_correction(
                measurement, data,
                submitted_by=str(data.get("submitted_by", "")))
        except CorrectionError as exc:
            return _correction_error_response(exc)
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        if not created:
            # idempotent repeat: the SAME order, never a second one.
            resp = Response(MeasurementCorrectionSerializer(order).data,
                            status=code)
            resp["Idempotent-Replay"] = "true"
            return resp
        return Response(MeasurementCorrectionSerializer(order).data,
                        status=code)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        reconcile_interrupted_applications()
        order = get_object_or_404(MeasurementCorrection, pk=pk)
        ser = CorrectionReviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            order = review_correction(
                order,
                ser.validated_data["decision"],
                reviewed_by=ser.validated_data.get("reviewed_by", ""),
                note=ser.validated_data.get("note", ""))
        except CorrectionError as exc:
            return _correction_error_response(exc)
        return Response(MeasurementCorrectionSerializer(order).data)

    @action(detail=True, methods=["get"], url_path="impact")
    def impact(self, request, pk=None):
        """Impact scope: old/new diff, frozen confirmed editions, blockers."""
        reconcile_interrupted_applications()
        order = get_object_or_404(MeasurementCorrection, pk=pk)
        return Response(impact_summary(order))

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        """
        Append the effective revision and re-scan identity. Retryable:
        a repeated/retried call never creates a second effective revision.
        Blocked with 409 when the corrected coordinates sit in an OPEN
        identity contradiction — a human must verify that first.
        """
        order = get_object_or_404(MeasurementCorrection, pk=pk)
        try:
            _rev, newly_applied = apply_correction(order)
        except CorrectionError as exc:
            order.refresh_from_db()
            payload = MeasurementCorrectionSerializer(order).data
            payload["detail"] = exc.detail
            return Response(payload, status=exc.status_code)
        order.refresh_from_db()
        resp = Response(MeasurementCorrectionSerializer(order).data)
        resp["Revision-Newly-Applied"] = "true" if newly_applied else "false"
        return resp

    @action(detail=True, methods=["post"], url_path="recompute")
    def recompute(self, request, pk=None):
        """Produce a NEW draft EstimateVersion using this revision."""
        order = get_object_or_404(MeasurementCorrection, pk=pk)
        try:
            version = recompute_with_correction(
                order,
                label=request.data.get("label"),
                equation_ids=request.data.get("equation_ids"),
                fpc=request.data.get("fpc"))
        except CorrectionError as exc:
            return _correction_error_response(exc)
        except ValueError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(EstimateVersionSerializer(version).data,
                        status=status.HTTP_201_CREATED)


class RevisionViewSet(viewsets.ReadOnlyModelViewSet):
    """Traceable revision ledger (effective + voided, never deleted)."""
    serializer_class = MeasurementRevisionSerializer

    def get_queryset(self):
        reconcile_interrupted_applications()
        qs = (
            MeasurementRevision.objects
            .select_related("measurement", "measurement__tree",
                            "measurement__tree__plot",
                            "measurement__campaign", "correction")
            .order_by("created_at"))
        measurement = self.request.query_params.get("measurement")
        if measurement:
            qs = qs.filter(measurement_id=measurement)
        revision_state = self.request.query_params.get("revision_status")
        if revision_state:
            qs = qs.filter(revision_status=revision_state)
        return qs
