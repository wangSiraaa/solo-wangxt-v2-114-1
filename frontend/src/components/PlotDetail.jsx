import React, { useMemo } from "react";

/**
 * One plot: boundary + every individual's t1 -> t2 remeasurement.
 * Identity is the internal tree row; labels shown are what was on the tag.
 *
 * Tables/maps use the EFFECTIVE measurement view (base row unless an
 * applied correction order revised it). The base raw value and the
 * revision marker are shown alongside so old → new differences are always
 * auditable.
 */
export default function PlotDetail({ plotCode, ctx, onBack }) {
  const { plots, m1, m2, t1, t2 } = ctx;
  const plot = plots.find((p) => p.code === plotCode);

  const rows = useMemo(() => {
    // Show every tree that has at least one measurement in this plot.
    const a = m1.filter((m) => m.plot_code === plotCode);
    const b = m2.filter((m) => m.plot_code === plotCode);
    const byTree = new Map();
    a.forEach((m) => byTree.set(m.tree, { t1: m }));
    b.forEach((m) => {
      const cur = byTree.get(m.tree) || {};
      cur.t2 = m;
      byTree.set(m.tree, cur);
    });
    return [...byTree.values()].sort((x, y) => {
      const nx = (x.t2 || x.t1).field_number;
      const ny = (y.t2 || y.t1).field_number;
      return nx.localeCompare(ny);
    });
  }, [m1, m2, plotCode]);

  if (!plot) return null;
  const xs = plot.boundary.map(([x]) => x);
  const ys = plot.boundary.map(([, y]) => y);
  const W = 640, H = 420, PAD = 40;
  const sx = (x) => PAD + (x - Math.min(...xs)) /
    (Math.max(...xs) - Math.min(...xs)) * (W - 2 * PAD);
  const sy = (y) => H - PAD - (y - Math.min(...ys)) /
    (Math.max(...ys) - Math.min(...ys)) * (H - 2 * PAD);

  // Classification uses EFFECTIVE values (revisions overlaid).
  function rowClass(r) {
    if (r.t1 && r.t2) {
      if (r.t2.effective_status === "dead") return "mortality";
      if (r.t2.effective_status === "missing_tree") return "missing";
      if (r.t2.effective_status === "alive_not_measured") return "notmeasured";
      const d = Math.abs(
        (r.t2.effective_dbh_cm ?? 0) - (r.t1.effective_dbh_cm ?? 0));
      if (d <= 0.15) return "zero";
      return "growth";
    }
    return r.t2 ? "ingrowth" : "lost";
  }

  const revisedRows = rows.filter(
    (r) => (r.t1 && r.t1.revision_id) || (r.t2 && r.t2.revision_id));

  return (
    <div>
      <button className="back" onClick={onBack}>← all plots</button>
      <h2>Plot {plot.code}
        <small> {plot.declared_area_ha} ha · stratum {plot.stratum_code}
          {" "}· polygon {plot.area_polygon_ha.toFixed(4)} ha</small>
      </h2>

      {revisedRows.length > 0 && (
        <div className="revision-banner">
          {revisedRows.length} measurement(s) on this plot carry an applied
          correction order. Bold positions/dbh below are the EFFECTIVE
          (revised) values; the historical base value is shown struck
          through. Chain visible under Estimates → provenance.
        </div>
      )}

      <div className="detail-grid">
        <svg viewBox={`0 0 ${W} ${H}`} className="plot-map">
          <polygon
            points={plot.boundary.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
            className="boundary" />
          {rows.map((r) => {
            const t1m = r.t1, t2m = r.t2;
            if (t1m && t2m) {
              return (
                <g key={t1m.tree + (t2m?.id ?? "")}>
                  <line x1={sx(t1m.effective_x_m)} y1={sy(t1m.effective_y_m)}
                        x2={sx(t2m.effective_x_m)} y2={sy(t2m.effective_y_m)}
                        className="move-line" />
                  <circle cx={sx(t2m.effective_x_m)}
                          cy={sy(t2m.effective_y_m)} r={6}
                          className={`stem ${rowClass(r)}`} />
                </g>
              );
            }
            const m = t2m || t1m;
            return <circle key={m.id} cx={sx(m.effective_x_m)}
                           cy={sy(m.effective_y_m)} r={6}
                           className={`stem ${rowClass(r)}`} />;
          })}
        </svg>

        <table className="tree-table">
          <thead>
            <tr>
              <th>tag t1 → t2</th>
              <th>dbh {t1} (effective cm)</th>
              <th>dbh {t2} (effective cm)</th>
              <th>Δ dbh</th>
              <th>status / source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const label1 = r.t1?.field_number ?? "—";
              const label2 = r.t2?.field_number ?? "—";
              const d1 = r.t1?.effective_dbh_cm;
              const d2 = r.t2?.effective_dbh_cm;
              const delta = (d1 != null && d2 != null)
                ? (d2 - d1).toFixed(2) : "—";
              const cls = rowClass(r);
              const source = {
                growth: "survivor growth",
                zero: "VERIFIED zero growth (measured, cross-checked)",
                notmeasured: "alive but NOT measured — missing, ratio-imputed",
                mortality: "MORTALITY (dead observation)",
                missing: "not located at t2",
                ingrowth: d2 >= 5 ? "INGROWTH ≥ 5 cm"
                                  : "below recruitment — excluded",
                lost: "t1 only",
              }[cls];
              return (
                <tr key={(r.t1 || r.t2).tree} className={cls}>
                  <td>{label1}{label1 !== label2 ? ` → ${label2}` : ""}
                    {label1 !== label2 &&
                      <span className="renumber-badge"> renumber</span>}
                  </td>
                  <DbhCell m={r.t1} />
                  <DbhCell m={r.t2} />
                  <td>{delta}</td>
                  <td>{source}
                    {(r.t1?.revision_id || r.t2?.revision_id) && (
                      <div className="rev-tags">
                        {r.t1?.revision_id &&
                          <span className="rev-chip">
                            t1 revision #{r.t1.revision_id}
                            (order #{r.t1.correction_id})
                          </span>}
                        {r.t2?.revision_id &&
                          <span className="rev-chip">
                            t2 revision #{r.t2.revision_id}
                            (order #{r.t2.correction_id})
                          </span>}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * Effective dbh with the historical base shown underneath when a
 * correction revised it. Raw value + declared unit are retained for audit.
 */
function DbhCell({ m }) {
  if (!m) return <td>—</td>;
  const revised = m.revision_id != null;
  const rawLabel = m.effective_dbh_unit && m.effective_dbh_unit !== "cm"
    ? <small> ({m.effective_dbh_raw} {m.effective_dbh_unit})</small>
    : null;
  return (
    <td className={revised ? "revised-cell" : ""}>
      <strong>{m.effective_dbh_cm ?? "—"}{rawLabel}</strong>
      {revised && (
        <small className="base-struck">
          {" "}base {m.dbh_cm} cm
          {m.dbh_unit && m.dbh_unit !== "cm"
            ? ` (raw ${m.dbh_raw} ${m.dbh_unit})`
            : ` (raw ${m.dbh_raw} cm)`}
        </small>
      )}
      {!revised && m.dbh_unit && m.dbh_unit !== "cm" && (
        <small> ({m.dbh_raw} {m.dbh_unit})</small>
      )}
    </td>
  );
}
