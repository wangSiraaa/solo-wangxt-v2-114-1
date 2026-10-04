from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    Campaign,
    EstimateVersion,
    IdentityConflict,
    MeasurementCorrection,
    MeasurementRevision,
    Plot,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)


class StratumSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stratum
        fields = ["id", "code", "name", "area_ha"]


class SpeciesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Species
        fields = ["id", "code", "name", "family"]


class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ["id", "code", "measured_on", "description"]


class PlotSerializer(serializers.ModelSerializer):
    stratum_code = serializers.CharField(source="stratum.code", read_only=True)
    stratum_name = serializers.CharField(source="stratum.name", read_only=True)
    crs_epsg = serializers.SerializerMethodField()

    class Meta:
        model = Plot
        fields = [
            "id", "code", "stratum", "stratum_code", "stratum_name",
            "x_m", "y_m", "declared_area_ha", "area_polygon_ha",
            "boundary", "crs_epsg",
        ]

    def get_crs_epsg(self, _obj):
        from django.conf import settings
        return settings.SURVEY_CRS_EPSG


class EquationSerializer(serializers.ModelSerializer):
    species_codes = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="code", source="species"
    )

    class Meta:
        model = AllometricEquation
        fields = [
            "id", "code", "version", "species_codes", "status", "form",
            "a", "b", "c", "dbh_min_cm", "dbh_max_cm",
            "height_required", "residual_sigma", "citation", "created_at",
        ]


class TreeSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    species_code = serializers.CharField(source="species.code", read_only=True)
    supersedes = serializers.PrimaryKeyRelatedField(
        source="superseded_tree", read_only=True
    )

    class Meta:
        model = Tree
        fields = [
            "id", "plot", "plot_code", "species_code",
            "current_field_number", "first_campaign", "supersedes",
        ]


class MeasurementSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="tree.plot.code", read_only=True)
    field_number = serializers.CharField(source="field_number_seen")
    # Effective (calculation-facing) values: base row unless an applied
    # correction order revised this measurement.
    effective_dbh_cm = serializers.SerializerMethodField()
    effective_height_m = serializers.SerializerMethodField()
    effective_x_m = serializers.SerializerMethodField()
    effective_y_m = serializers.SerializerMethodField()
    effective_status = serializers.SerializerMethodField()
    effective_dbh_raw = serializers.SerializerMethodField()
    effective_dbh_unit = serializers.SerializerMethodField()
    revision_id = serializers.SerializerMethodField()
    correction_id = serializers.SerializerMethodField()

    class Meta:
        model = TreeMeasurement
        fields = [
            "id", "tree", "campaign", "plot_code", "field_number",
            "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "notes",
            # effective view (== base when no revision exists)
            "effective_dbh_cm", "effective_height_m",
            "effective_x_m", "effective_y_m", "effective_status",
            "effective_dbh_raw", "effective_dbh_unit",
            "revision_id", "correction_id",
        ]

    def _eff(self, obj):
        from inventory.services.revisions import effective_values
        return effective_values(obj, getattr(obj, "_effective_revision", None))

    def get_effective_dbh_cm(self, obj):
        return self._eff(obj)["dbh_cm"]

    def get_effective_height_m(self, obj):
        return self._eff(obj)["height_m"]

    def get_effective_x_m(self, obj):
        return self._eff(obj)["x_m"]

    def get_effective_y_m(self, obj):
        return self._eff(obj)["y_m"]

    def get_effective_status(self, obj):
        return self._eff(obj)["status"]

    def get_effective_dbh_raw(self, obj):
        return self._eff(obj)["dbh_raw"]

    def get_effective_dbh_unit(self, obj):
        return self._eff(obj)["dbh_unit"]

    def get_revision_id(self, obj):
        return self._eff(obj)["revision_id"]

    def get_correction_id(self, obj):
        return self._eff(obj)["correction_id"]


class ConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityConflict
        fields = [
            "id", "plot", "field_number", "t1_campaign", "t2_campaign",
            "t1_measurement", "t2_measurement", "distance_m",
            "status", "resolution_note", "resolved_at",
            "triggered_by_correction",
        ]
        read_only_fields = ["distance_m", "resolved_at",
                            "triggered_by_correction"]


class ConflictResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["renumber", "distinct"])
    note = serializers.CharField(required=False, allow_blank=True)


class EstimateVersionSerializer(serializers.ModelSerializer):
    measurement_revision_ids = serializers.SerializerMethodField()

    class Meta:
        model = EstimateVersion
        fields = [
            "id", "label", "t1_campaign", "t2_campaign", "status",
            "design_snapshot", "result_payload", "equation_checksum",
            "created_at", "confirmed_at", "measurement_revision_ids",
        ]
        read_only_fields = [
            "status", "design_snapshot", "result_payload",
            "equation_checksum", "confirmed_at",
        ]

    def get_measurement_revision_ids(self, obj):
        if hasattr(obj, "_prefetched_revision_ids"):
            return obj._prefetched_revision_ids
        return list(obj.measurement_revisions.values_list("id", flat=True))


class MeasurementRevisionSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(
        source="measurement.tree.plot.code", read_only=True)
    field_number = serializers.CharField(
        source="measurement.field_number_seen", read_only=True)
    campaign = serializers.CharField(
        source="measurement.campaign.code", read_only=True)
    correction_reason = serializers.CharField(
        source="correction.reason", read_only=True)
    supersedes = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = MeasurementRevision
        fields = [
            "id", "measurement", "correction", "supersedes",
            "plot_code", "field_number", "campaign",
            "dbh_cm", "height_m", "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "height_raw", "height_unit",
            "revision_status", "created_at", "effective_at",
            "void_reason", "correction_reason",
        ]


class MeasurementCorrectionSerializer(serializers.ModelSerializer):
    revision = MeasurementRevisionSerializer(read_only=True)
    plot_code = serializers.CharField(
        source="measurement.tree.plot.code", read_only=True)
    field_number = serializers.CharField(
        source="measurement.field_number_seen", read_only=True)
    campaign = serializers.CharField(
        source="measurement.campaign.code", read_only=True)

    class Meta:
        model = MeasurementCorrection
        fields = [
            "id", "measurement", "plot_code", "field_number", "campaign",
            "idempotency_key", "reason", "evidence", "status",
            "original_dbh_raw", "original_dbh_unit", "original_dbh_cm",
            "original_height_raw", "original_height_unit",
            "original_height_m", "original_x_m", "original_y_m",
            "original_status", "based_on_revision",
            "corrected_dbh_raw", "corrected_dbh_unit", "corrected_dbh_cm",
            "corrected_height_raw", "corrected_height_unit",
            "corrected_height_m", "corrected_x_m", "corrected_y_m",
            "corrected_status",
            "submitted_by", "submitted_at",
            "reviewed_by", "reviewed_at", "review_note",
            "applied_at", "failure_reason", "revision",
        ]
        read_only_fields = [
            "status",
            # canonical values are DERIVED from corrected raw+unit, never
            # accepted straight from the client.
            "original_dbh_cm", "original_height_m",
            "original_status", "based_on_revision", "submitted_at",
            "reviewed_by", "reviewed_at", "review_note", "applied_at",
            "failure_reason",
        ]
        extra_kwargs = {
            # idempotency key is required ON CREATE; it identifies repeats.
            "idempotency_key": {"required": True},
            "reason": {"required": True},
            "measurement": {"required": True},
            "evidence": {"required": True},
            # the pre-review snapshot is entirely server-derived
            "original_dbh_raw": {"read_only": True},
            "original_dbh_unit": {"read_only": True},
            "original_height_raw": {"read_only": True},
            "original_height_unit": {"read_only": True},
            "original_x_m": {"read_only": True},
            "original_y_m": {"read_only": True},
            # corrected fields are optional individually: omitted fields
            # keep the current effective value.
            "corrected_dbh_raw": {"required": False, "allow_null": True},
            "corrected_dbh_unit": {"required": False, "allow_null": True},
            "corrected_height_raw": {"required": False, "allow_null": True},
            "corrected_height_unit": {"required": False, "allow_null": True},
            "corrected_x_m": {"required": False},
            "corrected_y_m": {"required": False},
        }


class CorrectionReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["apply", "reject"])
    reviewed_by = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)


class MeasurementImportRowSerializer(serializers.Serializer):
    """One raw field row. Units are mandatory with every value."""

    plot = serializers.CharField()
    field_number = serializers.CharField()
    species = serializers.CharField()
    x_m = serializers.FloatField()
    y_m = serializers.FloatField()
    status = serializers.ChoiceField(
        choices=["alive_measured", "alive_not_measured", "dead", "missing_tree"]
    )
    dbh_raw = serializers.FloatField(required=False, allow_null=True)
    dbh_unit = serializers.ChoiceField(choices=["cm", "mm", "in"],
                                       required=False, allow_null=True)
    height_raw = serializers.FloatField(required=False, allow_null=True)
    height_unit = serializers.ChoiceField(choices=["m"],
                                          required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # Used ONLY to record a field-book verified renumber. Never inferred.
    verified_renumber_of_tree = serializers.IntegerField(
        required=False, allow_null=True
    )


class MeasurementImportSerializer(serializers.Serializer):
    campaign = serializers.CharField()
    rows = MeasurementImportRowSerializer(many=True)
