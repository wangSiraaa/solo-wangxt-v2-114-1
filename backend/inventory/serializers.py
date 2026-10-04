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

    class Meta:
        model = TreeMeasurement
        fields = [
            "id", "tree", "campaign", "plot_code", "field_number",
            "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "notes",
        ]


class ConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityConflict
        fields = [
            "id", "plot", "field_number", "t1_campaign", "t2_campaign",
            "t1_measurement", "t2_measurement", "distance_m",
            "status", "resolution_note", "resolved_at",
        ]
        read_only_fields = ["distance_m", "resolved_at"]


class ConflictResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["renumber", "distinct"])
    note = serializers.CharField(required=False, allow_blank=True)


class EstimateVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = EstimateVersion
        fields = [
            "id", "label", "t1_campaign", "t2_campaign", "status",
            "design_snapshot", "result_payload", "equation_checksum",
            "created_at", "confirmed_at",
        ]
        read_only_fields = [
            "status", "design_snapshot", "result_payload",
            "equation_checksum", "confirmed_at",
        ]


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


# ------------------------------------------------------- correction orders
class MeasurementRevisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MeasurementRevision
        fields = [
            "id", "measurement", "correction", "sequence",
            "changed_fields", "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "x_m", "y_m",
            "created_at",
        ]


class MeasurementCorrectionSerializer(serializers.ModelSerializer):
    revision = MeasurementRevisionSerializer(read_only=True)
    plot_code = serializers.CharField(source="measurement.tree.plot.code",
                                      read_only=True)
    campaign_code = serializers.CharField(source="measurement.campaign.code",
                                          read_only=True)
    field_number = serializers.CharField(
        source="measurement.field_number_seen", read_only=True)
    tree_id = serializers.IntegerField(source="measurement.tree_id",
                                       read_only=True)

    class Meta:
        model = MeasurementCorrection
        fields = [
            "id", "measurement", "tree_id", "plot_code", "campaign_code",
            "field_number", "idempotency_key", "original_snapshot",
            "corrected", "reason", "evidence", "status", "review_note",
            "failure_detail", "created_at", "reviewed_at", "applied_at",
            "revision",
        ]
        read_only_fields = ["status", "reviewed_at", "applied_at"]


class CorrectionSubmitSerializer(serializers.Serializer):
    """File a correction order. Idempotent on idempotency_key."""

    measurement_id = serializers.IntegerField()
    idempotency_key = serializers.CharField(max_length=80)
    corrected = serializers.DictField()
    reason = serializers.CharField(max_length=300)
    evidence = serializers.CharField(max_length=300)


class CorrectionReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "reject"])
    note = serializers.CharField(required=False, allow_blank=True,
                                 max_length=300)


class CorrectionRecomputeSerializer(serializers.Serializer):
    equation_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False
    )
    label = serializers.CharField(required=False, max_length=120)
    fpc = serializers.BooleanField(required=False, default=True)
    compare_to = serializers.IntegerField(required=False, allow_null=True)
