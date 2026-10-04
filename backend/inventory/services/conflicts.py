"""
Scan two campaigns for identity conflicts and persist them.

A conflict = same field number in one plot at both campaigns but the
recorded stem positions contradict "same individual" (distance beyond
tolerance), or a close-position pair carrying different labels that might
be an unrecorded renumber. Nothing is merged automatically.

The scanner reads the EFFECTIVE measurement view (see services/revisions):
after a coordinate correction is applied, contradictions are re-scanned
against corrected positions, never against stale base rows.
"""
from inventory.models import IdentityConflict
from inventory.services.identity import (
    POSITION_TOLERANCE_M,
    RENUMBER_SEARCH_RADIUS_M,
    _distance_m,
)
from inventory.services.revisions import effective_campaign_rows


def _is_resolved(t1_id, t2_id):
    return IdentityConflict.objects.filter(
        t1_measurement_id=t1_id, t2_measurement_id=t2_id,
        status__in=["renumber", "distinct"],
    ).exists()


def _is_verified_successor(t2_row):
    """
    A human has verified this t2 individual renumbered from some t1 tree
    (successor link on the tree row), or a conflict decision on the exact
    (t1 stem, t2 stem) geometry already exists.
    """
    if t2_row.tree.superseded_tree_id:
        return True
    # any human decision (renumber OR distinct) involving this t2 stem and
    # a DIFFERENT t1 stem settles the "who are you near?" question.
    return IdentityConflict.objects.filter(
        t2_measurement_id=t2_row.id,
        status__in=["renumber", "distinct"],
    ).exists()


def find_identity_contradictions(rows_t1, rows_t2,
                                 tolerance_m=POSITION_TOLERANCE_M,
                                 search_radius_m=RENUMBER_SEARCH_RADIUS_M):
    """
    Pure geometry: which (t1_measurement_id, t2_measurement_id) pairs
    contradict "same individual" under the effective positions/labels?

    Already human-resolved pairs are skipped. Returns dicts sorted so the
    caller can diff against persisted IdentityConflict rows.
    """
    from collections import defaultdict

    t1_by_plot_num, t2_by_plot_num = defaultdict(list), defaultdict(list)
    for r in rows_t1:
        t1_by_plot_num[(r.tree.plot_id, r.field_number_seen)].append(r)
    for r in rows_t2:
        t2_by_plot_num[(r.tree.plot_id, r.field_number_seen)].append(r)

    contradictions = []

    # same label, contradictory position
    for key, m2_list in t2_by_plot_num.items():
        for m2 in m2_list:
            t1_list = t1_by_plot_num.get(key, [])
            if not t1_list:
                continue
            nearest = min(
                ((_distance_m(m1.x_m, m1.y_m, m2.x_m, m2.y_m), m1)
                 for m1 in t1_list),
                key=lambda t: t[0],
            )
            d, m1 = nearest
            # A verified successor already has an authoritative predecessor
            # (same row or field-book link): it cannot simultaneously be a
            # same-number contradiction.
            if (d > tolerance_m
                    and not _is_resolved(m1.id, m2.id)
                    and not (m1.tree_id == m2.tree_id
                             or _is_verified_successor(m2))):
                contradictions.append({
                    "t1_measurement_id": m1.id,
                    "t2_measurement_id": m2.id,
                    "t1_tree_id": m1.tree_id, "t2_tree_id": m2.tree_id,
                    "plot_id": m1.tree.plot_id,
                    "field_number": m1.field_number_seen,
                    "t2_field_number": m2.field_number_seen,
                    "distance_m": round(d, 3),
                    "hint": "same_number_position_mismatch",
                })

    # different labels, close position -> possible unrecorded renumber.
    # Dismissed once a human has recorded ANY decision about this exact
    # geometry (renumber/distinct) or the t2 individual is a verified
    # successor of some t1 tree.
    t2_by_plot = defaultdict(list)
    for r in rows_t2:
        t2_by_plot[r.tree.plot_id].append(r)
    for m1 in rows_t1:
        for m2 in t2_by_plot.get(m1.tree.plot_id, []):
            if m1.tree_id == m2.tree_id:
                continue
            if m1.field_number_seen == m2.field_number_seen:
                continue
            d = _distance_m(m1.x_m, m1.y_m, m2.x_m, m2.y_m)
            if (d <= search_radius_m
                    and not _is_resolved(m1.id, m2.id)
                    and not _is_verified_successor(m2)):
                contradictions.append({
                    "t1_measurement_id": m1.id,
                    "t2_measurement_id": m2.id,
                    "t1_tree_id": m1.tree_id, "t2_tree_id": m2.tree_id,
                    "plot_id": m1.tree.plot_id,
                    "field_number": m1.field_number_seen,
                    "t2_field_number": m2.field_number_seen,
                    "distance_m": round(d, 3),
                    "hint": "possible_renumber",
                })

    # de-duplicate by measurement pair (both predicates could match once)
    seen, unique = set(), []
    for c in contradictions:
        key = (c["t1_measurement_id"], c["t2_measurement_id"])
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def scan_conflicts(t1_campaign, t2_campaign,
                   tolerance_m=POSITION_TOLERANCE_M,
                   search_radius_m=RENUMBER_SEARCH_RADIUS_M,
                   triggered_by_correction=None):
    """
    Persist current identity contradictions under EFFECTIVE positions.

    Re-running this after a correction re-scans everything: newly exposed
    contradictions are inserted (and tagged with the correction that
    exposed them); contradictions that no longer exist geometrically are
    left untouched if a human already resolved them, but open rows whose
    contradiction vanished are marked resolved with a note? — No. Open
    rows are never auto-closed; they simply stop being reported if the
    geometry clears.
    """
    rows_t1 = effective_campaign_rows(t1_campaign)
    rows_t2 = effective_campaign_rows(t2_campaign)
    contradictions = find_identity_contradictions(
        rows_t1, rows_t2, tolerance_m, search_radius_m)

    found = []
    for c in contradictions:
        from inventory.models import TreeMeasurement
        m1 = TreeMeasurement.objects.get(pk=c["t1_measurement_id"])
        m2 = TreeMeasurement.objects.get(pk=c["t2_measurement_id"])
        obj, created = IdentityConflict.objects.get_or_create(
            t1_measurement=m1, t2_measurement=m2,
            defaults={
                "plot_id": c["plot_id"],
                "field_number": c["field_number"],
                "t1_campaign": t1_campaign,
                "t2_campaign": t2_campaign,
                "distance_m": c["distance_m"],
                "resolution_note": c["hint"],
                "triggered_by_correction": triggered_by_correction,
            },
        )
        if created and triggered_by_correction is not None:
            obj.triggered_by_correction = triggered_by_correction
            obj.save(update_fields=["triggered_by_correction"])
        found.append({
            "id": obj.id, "created": created,
            "plot": m1.tree.plot.code,
            "field_number": c["field_number"],
            "t2_field_number": c["t2_field_number"],
            "distance_m": round(c["distance_m"], 2),
            "hint": c["hint"] if created else obj.get_status_display(),
        })
    return found


def correction_preflight(correction):
    """
    Simulate applying ONE correction and return identity blockers.

    A blocker = an unresolved identity contradiction that the corrected
    measurement participates in under the simulated positions. Coordinate
    corrections that would open (or keep the stem inside) an OPEN
    IdentityConflict cannot be applied: the field office must resolve the
    contradiction by hand first. Unit-only corrections never move a stem,
    so they cannot trigger geometric contradictions.
    """
    c = correction
    m = c.measurement
    t1_campaign, t2_campaign = _campaign_pair(m.campaign)

    overlay = {m.id: {
        "x_m": c.corrected_x_m, "y_m": c.corrected_y_m,
        "field_number_seen": m.field_number_seen,
    }}
    rows_t1 = effective_campaign_rows(t1_campaign,
                                      extra_overlay=overlay if m.campaign_id
                                      == t1_campaign.id else None)
    rows_t2 = effective_campaign_rows(t2_campaign,
                                      extra_overlay=overlay if m.campaign_id
                                      == t2_campaign.id else None)

    simulated = find_identity_contradictions(rows_t1, rows_t2)
    involved = [x for x in simulated
                if m.id in (x["t1_measurement_id"], x["t2_measurement_id"])]

    # Which of these already exist as OPEN persisted conflicts?
    current_open = {
        (x.t1_measurement_id, x.t2_measurement_id)
        for x in IdentityConflict.objects.filter(status="open")
    }
    blockers = []
    for x in involved:
        pair = (x["t1_measurement_id"], x["t2_measurement_id"])
        blockers.append({
            **x,
            "newly_triggered": pair not in current_open,
        })
    return blockers


def _campaign_pair(campaign):
    """The other campaign and this one, ordered t1 < t2."""
    from inventory.models import Campaign
    others = Campaign.objects.exclude(pk=campaign.pk).order_by("measured_on")
    other = others.first()
    if other is None:
        return campaign, campaign
    t1, t2 = sorted([campaign, other], key=lambda c: c.measured_on)
    return t1, t2
