"""
Shared access to the applied measurement-revision overlay.

The historical TreeMeasurement rows are never rewritten; corrections
append MeasurementRevision rows. Anything that reasons about "where is
this stem / what is its dbh NOW" — the estimator, the identity
contradiction scan — must read through this overlay so corrected values
take effect everywhere consistently.
"""


def latest_revision_map(campaign):
    """Latest applied MeasurementRevision per measurement of a campaign."""
    from inventory.models import MeasurementRevision
    latest = {}
    qs = (MeasurementRevision.objects
          .filter(measurement__campaign=campaign)
          .select_related("correction")
          .order_by("measurement_id", "sequence"))
    for rev in qs:  # ascending sequence -> last write per measurement wins
        latest[rev.measurement_id] = rev
    return latest


def effective_positions(measurements):
    """{measurement_id: (x_m, y_m)} with applied revisions overlaid."""
    by_campaign = {}
    pos = {}
    for m in measurements:
        if m.campaign_id not in by_campaign:
            by_campaign[m.campaign_id] = latest_revision_map(m.campaign)
        rev = by_campaign[m.campaign_id].get(m.id)
        pos[m.id] = ((rev.x_m, rev.y_m) if rev is not None
                     else (m.x_m, m.y_m))
    return pos
