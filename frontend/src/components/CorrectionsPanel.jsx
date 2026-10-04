import React, { useEffect, useState } from "react";
import { api } from "../api.js";

/**
 * Measurement correction orders (测量更正单) — the closed loop:
 * file -> review -> apply -> recompute. Applying never rewrites the
 * historical measurement row; it appends a traceable revision that only
 * NEW draft editions use. Confirmed editions stay frozen.
 */
export default function CorrectionsPanel({ equations, onEstimateCreated }) {
  const [orders, setOrders] = useState([]);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(null);
  const [impact, setImpact] = useState(null);      // {id, data}
  const [recomputed, setRecomputed] = useState(null); // {version, diff}
  const [form, setForm] = useState({
    measurement_id: "", idempotency_key: "",
    dbh_raw: "", dbh_unit: "", x_m: "", y_m: "",
    reason: "", evidence: "",
  });

  async function load() {
    setOrders(await api.corrections());
  }
  useEffect(() => { load().catch((e) => setErr(e.message)); }, []);

  function setF(k, v) { setForm((f) => ({ ...f, [k]: v })); }

  async function submit() {
    setBusy("submit"); setErr(""); setNote("");
    try {
      const corrected = {};
      if (form.dbh_raw !== "") corrected.dbh_raw = parseFloat(form.dbh_raw);
      if (form.dbh_unit !== "") corrected.dbh_unit = form.dbh_unit;
      if (form.x_m !== "") corrected.x_m = parseFloat(form.x_m);
      if (form.y_m !== "") corrected.y_m = parseFloat(form.y_m);
      const r = await api.submitCorrection({
        measurement_id: parseInt(form.measurement_id, 10),
        idempotency_key: form.idempotency_key,
        corrected,
        reason: form.reason,
        evidence: form.evidence,
      });
      setNote(r.created
        ? `correction #${r.id} filed (pending review)`
        : `idempotency key already filed as correction #${r.id} — no duplicate created`);
      setForm((f) => ({ ...f, idempotency_key: "" }));
      await load();
    } catch (e) { setErr(e.message); }
    finally { setBusy(null); }
  }

  async function act(id, fn, msg) {
    setBusy(id); setErr(""); setNote("");
    try {
      await fn();
      setNote(msg);
      await load();
    } catch (e) { setErr(e.message); }
    finally { setBusy(null); }
  }

  async function showImpact(id) {
    setErr("");
    try {
      setImpact({ id, data: await api.correctionImpact(id) });
    } catch (e) { setErr(e.message); }
  }

  async function recompute(id) {
    setBusy(id); setErr(""); setNote(""); setRecomputed(null);
    try {
      const r = await api.recomputeCorrection(id, {
        equation_ids: equations.map((e) => e.id),
        label: `recompute after correction #${id}`,
        fpc: true,
      });
      setRecomputed(r);
      onEstimateCreated?.(r.version);
    } catch (e) { setErr(e.message); }
    finally { setBusy(null); }
  }

  const fmtVal = (s, k) => (s && s[k] != null ? s[k] : "—");

  return (
    <section className="corrections">
      <h3>Measurement correction orders (测量更正单)</h3>
      <p className="hint">
        A correction never edits the historical measurement row. Applying
        appends an immutable revision; only NEW draft editions use it —
        confirmed editions keep their frozen numbers, checksum and
        provenance. Coordinate corrections re-scan identity contradictions
        and can never bypass human verification.
      </p>
      {err && <div className="error">{err}</div>}
      {note && <div className="ok">{note}</div>}

      <details className="file-correction">
        <summary>File a correction order</summary>
        <div className="corr-form">
          <label>measurement id
            <input value={form.measurement_id}
                   onChange={(e) => setF("measurement_id", e.target.value)}
                   placeholder="see plot detail table" /></label>
          <label>idempotency key
            <input value={form.idempotency_key}
                   onChange={(e) => setF("idempotency_key", e.target.value)}
                   placeholder="fieldcheck-2026-…" /></label>
          <label>corrected dbh raw
            <input value={form.dbh_raw}
                   onChange={(e) => setF("dbh_raw", e.target.value)}
                   placeholder="25.0" /></label>
          <label>corrected dbh unit
            <select value={form.dbh_unit}
                    onChange={(e) => setF("dbh_unit", e.target.value)}>
              <option value="">— keep —</option>
              <option>cm</option><option>mm</option><option>in</option>
            </select></label>
          <label>corrected x [m]
            <input value={form.x_m}
                   onChange={(e) => setF("x_m", e.target.value)} /></label>
          <label>corrected y [m]
            <input value={form.y_m}
                   onChange={(e) => setF("y_m", e.target.value)} /></label>
          <label className="wide">reason
            <input value={form.reason}
                   onChange={(e) => setF("reason", e.target.value)}
                   placeholder="外业复核：…" /></label>
          <label className="wide">evidence
            <input value={form.evidence}
                   onChange={(e) => setF("evidence", e.target.value)}
                   placeholder="复核单号 / 照片 / 仪器日志" /></label>
          <button disabled={busy === "submit" || !form.measurement_id
                             || !form.idempotency_key || !form.reason}
                  onClick={submit}>
            submit (idempotent)
          </button>
        </div>
      </details>

      <table className="correction-table">
        <thead>
          <tr><th>#</th><th>measurement</th><th>correction</th>
          <th>reason / evidence</th><th>status</th><th>actions</th></tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id} className={`corr-${o.status}`}>
              <td>#{o.id}</td>
              <td>{o.plot_code}/{o.field_number} @{o.campaign_code}
                <br /><small>meas #{o.measurement} · tree {o.tree_id}</small>
              </td>
              <td>
                <CorrectionDiff o={o} />
                {o.revision &&
                  <div><span className="rev-badge">
                    revision #{o.revision.sequence} appended
                  </span></div>}
              </td>
              <td><small>{o.reason}<br />证据: {o.evidence}</small></td>
              <td>
                <span className={`badge corr-status-${o.status}`}>
                  {o.status}</span>
                {o.failure_detail &&
                  <small className="fail">{o.failure_detail}</small>}
              </td>
              <td>
                {o.status === "pending" && (
                  <>
                    <button disabled={busy === o.id}
                            onClick={() => act(o.id, () =>
                              api.reviewCorrection(o.id,
                                { decision: "approve" }),
                              `#${o.id} reviewed — approved`)}>
                      approve</button>
                    <button className="danger" disabled={busy === o.id}
                            onClick={() => act(o.id, () =>
                              api.reviewCorrection(o.id,
                                { decision: "reject" }),
                              `#${o.id} rejected`)}>
                      reject</button>
                  </>)}
                {(o.status === "reviewed" || o.status === "failed") && (
                  <button disabled={busy === o.id}
                          onClick={() => act(o.id, () =>
                            api.applyCorrection(o.id),
                            `#${o.id} applied — revision appended, identity re-scanned`)}>
                    apply{o.status === "failed" ? " (retry)" : ""}</button>)}
                {o.status === "applied" && (
                  <button disabled={busy === o.id}
                          onClick={() => recompute(o.id)}>
                    recompute new draft</button>)}
                <button onClick={() => showImpact(o.id)}>impact</button>
              </td>
            </tr>
          ))}
          {orders.length === 0 &&
            <tr><td colSpan={6}><small>no correction orders yet</small></td></tr>}
        </tbody>
      </table>

      {impact && (
        <div className="impact-panel">
          <h4>Impact of correction #{impact.id}
            <button className="back" onClick={() => setImpact(null)}>
              close</button></h4>
          <ImpactView data={impact.data} fmtVal={fmtVal} />
        </div>)}

      {recomputed && recomputed.diff && (
        <div className="impact-panel">
          <h4>Old vs new — draft #{recomputed.version.id} vs{" "}
            {recomputed.diff.compared_to.status} edition{" "}
            #{recomputed.diff.compared_to.id}
            <button className="back"
                    onClick={() => setRecomputed(null)}>close</button></h4>
          <table>
            <thead><tr><th>component</th><th>before kg</th>
              <th>after kg</th><th>Δ kg</th></tr></thead>
            <tbody>
              {Object.entries(recomputed.diff.components).map(([k, c]) => (
                <tr key={k}>
                  <td>{k.replace("_", " ")}</td>
                  <td>{c.before_total_kg.toFixed(1)}</td>
                  <td>{c.after_total_kg.toFixed(1)}</td>
                  <td className={c.delta_kg ? "delta-nonzero" : ""}>
                    {c.delta_kg > 0 ? "+" : ""}{c.delta_kg.toFixed(1)}</td>
                </tr>
              ))}
              <tr className="net">
                <td>net change</td>
                <td>{recomputed.diff.net_change.before_total_kg.toFixed(1)}</td>
                <td>{recomputed.diff.net_change.after_total_kg.toFixed(1)}</td>
                <td>{recomputed.diff.net_change.delta_kg > 0 ? "+" : ""}
                  {recomputed.diff.net_change.delta_kg.toFixed(1)}</td>
              </tr>
            </tbody>
          </table>
          <p className="hint">{recomputed.diff.note}; the compared edition
            stays frozen and re-readable.</p>
        </div>)}
    </section>
  );
}

function CorrectionDiff({ o }) {
  const snap = o.original_snapshot || {};
  const cor = o.corrected || {};
  const rows = [];
  const pair = (label, before, after, unit) => {
    if (after === undefined) return;
    rows.push(
      <div key={label} className="corr-diff-row">
        {label}: <s>{before ?? "—"}{unit && ` ${unit}`}</s> →{" "}
        <strong>{after ?? "—"}{unit && ` ${unit}`}</strong>
      </div>);
  };
  pair("dbh raw", snap.dbh_raw, cor.dbh_raw, cor.dbh_unit || snap.dbh_unit);
  if (cor.dbh_unit && cor.dbh_raw === undefined)
    pair("dbh unit", snap.dbh_unit, cor.dbh_unit);
  pair("x", snap.x_m, cor.x_m, "m");
  pair("y", snap.y_m, cor.y_m, "m");
  if (!rows.length) return <small>—</small>;
  return <>{rows}</>;
}

function ImpactView({ data, fmtVal }) {
  const fields = ["dbh_raw", "dbh_unit", "dbh_cm", "height_raw",
                  "height_unit", "height_m", "x_m", "y_m"];
  return (
    <div>
      <p>
        <strong>{data.measurement.plot}/{data.measurement.field_number}
        </strong> @{data.measurement.campaign} · status{" "}
        <span className={`badge corr-status-${data.status}`}>
          {data.status}</span>
        {data.validation_error &&
          <span className="error"> {data.validation_error}</span>}
      </p>
      <table>
        <thead><tr><th>field</th><th>filed against</th>
          <th>current effective</th><th>after apply</th></tr></thead>
        <tbody>
          {fields.map((f) => (
            <tr key={f}
                className={data.changed_fields?.includes(f) ? "changed" : ""}>
              <td>{f}</td>
              <td>{fmtVal(data.filed_against, f)}</td>
              <td>{fmtVal(data.current_effective, f)}</td>
              <td>{fmtVal(data.projected_after_apply, f)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.open_identity_conflicts_on_tree.length > 0 && (
        <p className="blocked">
          blocked identity items on this tree:{" "}
          {data.open_identity_conflicts_on_tree.map((c) => (
            <span key={c.id} className="conflict-chip">
              conflict #{c.id} ({c.field_number}, {c.distance_m} m) —{" "}
              {c.note}
            </span>))}
        </p>)}
      <h5>Affected estimate editions</h5>
      <ul className="impact-versions">
        {data.estimate_versions.map((v) => (
          <li key={v.id}>#{v.id} {v.label}
            {" "}<span className={`status-${v.status}`}>{v.status}</span>
            {" "}— {v.effect}</li>))}
        {data.estimate_versions.length === 0 &&
          <li>no estimate edition involves this campaign yet</li>}
      </ul>
    </div>
  );
}
