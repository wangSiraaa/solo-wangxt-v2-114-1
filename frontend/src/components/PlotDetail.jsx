import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

/**
 * One plot: boundary + every individual's t1 -> t2 remeasurement.
 * Identity is the internal tree row; labels shown are what was on the tag.
 * Correction orders and their revision chain are shown per measurement;
 * open identity conflicts on this plot are the blocked items.
 */
export default function PlotDetail({ plotCode, ctx, onBack }) {
  const { plots, m1, m2, t1, t2, conflicts } = ctx;
  const plot = plots.find((p) => p.code === plotCode);
  const [corrections, setCorrections] = useState([]);

  useEffect(() => {
    api.corrections(`?plot=${encodeURIComponent(plotCode)}`)
      .then(setCorrections).catch(() => setCorrections([]));
  }, [plotCode]);

  const rows = useMemo(() => {
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

  // measurement id -> its correction orders (the revision chain)
  const corrByMeasurement = useMemo(() => {
    const map = new Map();
    corrections.forEach((c) => {
      const list = map.get(c.measurement) || [];
      list.push(c);
      map.set(c.measurement, list);
    });
    return map;
  }, [corrections]);

  const blocked = useMemo(
    () => (conflicts || []).filter((c) => c.plot === plot?.id),
    [conflicts, plot]);

  if (!plot) return null;
  const xs = plot.boundary.map(([x]) => x);
  const ys = plot.boundary.map(([, y]) => y);
  const W = 640, H = 420, PAD = 40;
  const sx = (x) => PAD + (x - Math.min(...xs)) /
    (Math.max(...xs) - Math.min(...xs)) * (W - 2 * PAD);
  const sy = (y) => H - PAD - (y - Math.min(...ys)) /
    (Math.max(...ys) - Math.min(...ys)) * (H - 2 * PAD);

  function rowClass(r) {
    if (r.t1 && r.t2) {
      if (r.t2.status === "dead") return "mortality";
      if (r.t2.status === "missing_tree") return "missing";
      if (r.t2.status === "alive_not_measured") return "notmeasured";
      const d = Math.abs((r.t2.dbh_cm ?? 0) - (r.t1.dbh_cm ?? 0));
      if (d <= 0.15) return "zero";
      return "growth";
    }
    return r.t2 ? "ingrowth" : "lost";
  }

  function revisionBadges(m) {
    if (!m) return null;
    const list = corrByMeasurement.get(m.id) || [];
    return list.map((c) => (
      <span key={c.id}
            className={`rev-badge corr-status-${c.status}`}
            title={`${c.reason} — 证据: ${c.evidence}`}>
        ✎#{c.id} {c.status}
        {c.revision ? ` rev${c.revision.sequence}` : ""}
      </span>));
  }

  return (
    <div>
      <button className="back" onClick={onBack}>← all plots</button>
      <h2>Plot {plot.code}
        <small> {plot.declared_area_ha} ha · stratum {plot.stratum_code}
          {" "}· polygon {plot.area_polygon_ha.toFixed(4)} ha</small>
      </h2>

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
                  <line x1={sx(t1m.x_m)} y1={sy(t1m.y_m)}
                        x2={sx(t2m.x_m)} y2={sy(t2m.y_m)}
                        className="move-line" />
                  <circle cx={sx(t2m.x_m)} cy={sy(t2m.y_m)} r={6}
                          className={`stem ${rowClass(r)}`} />
                </g>
              );
            }
            const m = t2m || t1m;
            return <circle key={m.id} cx={sx(m.x_m)} cy={sy(m.y_m)} r={6}
                           className={`stem ${rowClass(r)}`} />;
          })}
        </svg>

        <table className="tree-table">
          <thead>
            <tr>
              <th>tag t1 → t2</th>
              <th>dbh {t1} cm</th>
              <th>dbh {t2} cm</th>
              <th>Δ dbh</th>
              <th>status / source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const label1 = r.t1?.field_number ?? "—";
              const label2 = r.t2?.field_number ?? "—";
              const d1 = r.t1?.dbh_cm;
              const d2 = r.t2?.dbh_cm;
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
                  <td>{d1 ?? "—"}
                    {r.t1?.dbh_unit && r.t1.dbh_unit !== "cm" &&
                      <small> ({r.t1.dbh_raw} {r.t1.dbh_unit})</small>}
                    {revisionBadges(r.t1)}
                  </td>
                  <td>{d2 ?? "—"}
                    {r.t2?.dbh_unit && r.t2.dbh_unit !== "cm" &&
                      <small> ({r.t2.dbh_raw} {r.t2.dbh_unit})</small>}
                    {revisionBadges(r.t2)}
                    {r.t2 && <small className="meas-id">
                      {" "}meas #{r.t2.id}</small>}
                  </td>
                  <td>{delta}</td>
                  <td>{source}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {(corrections.length > 0 || blocked.length > 0) && (
        <div className="plot-audit">
          {corrections.length > 0 && (
            <section>
              <h3>Revision chain (测量更正单)</h3>
              <table className="correction-table">
                <thead>
                  <tr><th>order</th><th>measurement</th><th>before → after</th>
                  <th>status</th><th>reason / evidence</th></tr>
                </thead>
                <tbody>
                  {corrections.map((c) => (
                    <tr key={c.id} className={`corr-${c.status}`}>
                      <td>#{c.id}</td>
                      <td>{c.field_number} @{c.campaign_code}
                        <br /><small>meas #{c.measurement}</small></td>
                      <td>
                        <ChainDiff c={c} />
                        {c.revision &&
                          <span className="rev-badge">
                            revision #{c.revision.sequence} — historical row
                            preserved</span>}
                      </td>
                      <td><span className={`badge corr-status-${c.status}`}>
                        {c.status}</span></td>
                      <td><small>{c.reason}<br />证据: {c.evidence}</small></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
          {blocked.length > 0 && (
            <section>
              <h3>Blocked identity items (awaiting human verification)</h3>
              {blocked.map((c) => (
                <span key={c.id} className="conflict-chip">
                  {c.field_number} · {c.distance_m?.toFixed(2)} m — excluded
                  from every component until verified
                </span>
              ))}
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function ChainDiff({ c }) {
  const snap = c.original_snapshot || {};
  const cor = c.corrected || {};
  const out = [];
  if (cor.dbh_raw !== undefined || cor.dbh_unit !== undefined) {
    out.push(
      <div key="dbh">
        dbh <s>{snap.dbh_raw} {snap.dbh_unit}</s> →{" "}
        <strong>{cor.dbh_raw ?? snap.dbh_raw}{" "}
          {cor.dbh_unit ?? snap.dbh_unit}</strong>
        {c.revision &&
          <small> (canonical {snap.dbh_cm} → {c.revision.dbh_cm} cm)</small>}
      </div>);
  }
  if (cor.x_m !== undefined || cor.y_m !== undefined) {
    out.push(
      <div key="xy">
        pos <s>{snap.x_m}, {snap.y_m}</s> →{" "}
        <strong>{cor.x_m ?? snap.x_m}, {cor.y_m ?? snap.y_m}</strong>
      </div>);
  }
  return out.length ? <>{out}</> : <small>—</small>;
}
