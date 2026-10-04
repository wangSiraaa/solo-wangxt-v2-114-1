import React, { useEffect, useState } from "react";
import { api } from "../api.js";

/**
 * Measurement correction orders (测量更正单) workbench.
 *
 * submit (pending) -> review (apply/reject) -> apply (append revision,
 * re-scan identity) -> recompute a NEW draft. Confirmed editions are never
 * touched. A coordinate correction that sits in an OPEN identity
 * contradiction fails application and routes the operator to the identity
 * workbench for manual verification.
 */
const FLOW = ["pending", "reviewed", "applied", "rejected", "failed"];

function fmt(v, digits = 2) {
  return v == null ? "—" : Number(v).toFixed(digits);
}

function OldNew({ oldv, newv, digits = 2, unit = "" }) {
  const changed = Number(oldv) !== Number(newv);
  return (
    <span className={changed ? "oldnew changed" : "oldnew"}>
      <span className="old">{fmt(oldv, digits)}{unit}</span>
      {" → "}
      <span className="new">{fmt(newv, digits)}{unit}</span>
      {changed && <span className="delta-badge"> corrected</span>}
    </span>
  );
}

export default function CorrectionsWorkbench({ onChanged }) {
  const [orders, setOrders] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [impact, setImpact] = useState(null);
  const [busy, setBusy] = useState(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [newDraftId, setNewDraftId] = useState(null);

  async function load() {
    setOrders(await api.corrections());
  }
  useEffect(() => { load(); }, []);

  async function act(id, fn, { keepOpen = true } = {}) {
    setBusy(id); setErr(""); setNote("");
    try {
      const res = await fn();
      await load();
      if (keepOpen) {
        setOpenId(id);
        setImpact(await api.correctionImpact(id).catch(() => null));
      }
      return res;
    } catch (e) {
      // 409 from apply carries the updated order in the response body; the
      // fetch wrapper only keeps the message, so re-list to reflect state.
      await load();
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <h2>Measurement correction orders (测量更正单)</h2>
      <p className="hint">
        A correction points at the ORIGINAL measurement and snapshots its
        raw value, unit, coordinates, reason and evidence. Applying appends
        a traceable revision — the historical row is never overwritten —
        then identity is re-scanned. Only a NEW draft estimate consumes the
        revision; confirmed editions stay frozen.
      </p>
      {note && <div className="ok">{note}</div>}
      {err && <div className="error">{err}</div>}
      {newDraftId && (
        <div className="ok">
          New draft edition #{newDraftId} created from the revision —
          open it under Estimates. Confirmed editions were not touched.
        </div>
      )}

      <table className="conflict-table">
        <thead>
          <tr><th>#</th><th>tree</th><th>status</th><th>dbh (old → new)</th>
              <th>coordinates (old → new)</th><th></th></tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id}
                className={o.status === "open" ? "open" : o.status}>
              <td>#{o.id}</td>
              <td>{o.plot_code}/{o.field_number} · {o.campaign}</td>
              <td><span className={`badge status-${o.status}`}>
                    {o.status}
                  </span>
                  {o.failure_reason &&
                    <small className="block-reason">
                      <br />{o.failure_reason}
                    </small>}
              </td>
              <td>
                <OldNew oldv={o.original_dbh_cm} newv={o.corrected_dbh_cm}
                        unit=" cm" />
                <small> raw {o.original_dbh_raw} {o.original_dbh_unit}
                  {" → "}
                  {o.corrected_dbh_raw} {o.corrected_dbh_unit}</small>
              </td>
              <td>
                <OldNew oldv={o.original_x_m} newv={o.corrected_x_m}
                        digits={1} unit="" /> ,{" "}
                <OldNew oldv={o.original_y_m} newv={o.corrected_y_m}
                        digits={1} unit="" />
              </td>
              <td>
                <button disabled={busy === o.id}
                        onClick={() => {
                          setOpenId(openId === o.id ? null : o.id);
                          setNewDraftId(null);
                          if (openId !== o.id) {
                            api.correctionImpact(o.id)
                              .then(setImpact).catch(() => setImpact(null));
                          }
                        }}>
                  {openId === o.id ? "close" : "open"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {openId && (() => {
        const o = orders.find((x) => x.id === openId);
        if (!o) return null;
        return (
          <section className="correction-detail">
            <h3>Correction #{o.id} — {o.reason}</h3>
            <p className="hint">
              submitted {new Date(o.submitted_at).toLocaleString()}
              {o.submitted_by && ` by ${o.submitted_by}`}
              {o.reviewed_at &&
                ` · reviewed ${new Date(o.reviewed_at).toLocaleString()}`
                + (o.reviewed_by ? ` by ${o.reviewed_by}` : "")}
              {o.applied_at &&
                ` · applied ${new Date(o.applied_at).toLocaleString()}`}
            </p>
            <div className="two-col">
              <div>
                <h4>Evidence</h4>
                <ul>
                  {(o.evidence || []).map((e, i) => (
                    <li key={i}>
                      <strong>{e.type}</strong>: {e.ref || "(no ref)"}
                      {e.note && ` — ${e.note}`}
                    </li>
                  ))}
                </ul>
                <h4>Original transcription (snapshot)</h4>
                <pre>{JSON.stringify({
                  dbh: [o.original_dbh_raw, o.original_dbh_unit],
                  canonical_dbh_cm: o.original_dbh_cm,
                  height: [o.original_height_raw, o.original_height_unit],
                  x_m: o.original_x_m, y_m: o.original_y_m,
                  status: o.original_status,
                  based_on_revision: o.based_on_revision,
                }, null, 2)}</pre>
              </div>
              <div>
                <h4>Impact scope</h4>
                {impact ? (
                  <>
                    {impact.blocked ? (
                      <div className="error">
                        BLOCKED by unresolved identity contradiction(s) —
                        a human must verify before this can apply:
                        <ul>
                          {impact.identity_blockers.map((b, i) => (
                            <li key={i}>
                              {b.field_number} → {b.t2_field_number} ·
                              {" "}{b.distance_m} m · {b.hint} ·
                              {" "}{b.newly_triggered
                                ? "NEWLY triggered" : "pre-existing open"}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : (
                      <div className="ok">
                        No open identity contradiction in the corrected
                        geometry — safe to apply.
                      </div>
                    )}
                    <p className="hint">
                      {impact.confirmed_editions_unchanged.length} confirmed
                      edition(s) stay frozen &amp; unchanged;
                      {" "}{impact.draft_editions_to_recompute.length} draft(s)
                      should be recomputed.
                    </p>
                  </>
                ) : <p className="hint">loading…</p>}
              </div>
            </div>

            <div className="action-row">
              {o.status === "pending" && (
                <>
                  <button disabled={busy === o.id}
                    onClick={() => act(o.id, () => api.reviewCorrection(
                      o.id, { decision: "apply", note: "checked evidence" }))}>
                    review: approve (→ reviewed)
                  </button>
                  <button className="danger" disabled={busy === o.id}
                    onClick={() => act(o.id, () => api.reviewCorrection(
                      o.id, { decision: "reject", note: "unverifiable" }))}>
                    review: reject
                  </button>
                </>
              )}
              {(o.status === "reviewed" || o.status === "failed") && (
                <button disabled={busy === o.id}
                  onClick={() => act(o.id, async () => {
                    await api.applyCorrection(o.id);
                    onChanged?.();
                  })}>
                  {o.status === "failed"
                    ? "retry application (append revision)"
                    : "apply correction (append revision)"}
                </button>
              )}
              {o.status === "applied" && (
                <>
                  <span className="ok-inline">
                    applied · revision #{o.revision?.id}
                    {" "}{o.revision?.revision_status}
                  </span>
                  <button disabled={busy === o.id}
                    onClick={async () => {
                      setBusy(o.id);
                      try {
                        const d = await api.recomputeCorrection(o.id);
                        setNewDraftId(d.id);
                        onChanged?.();
                      } catch (e) { setErr(e.message); }
                      finally { setBusy(null); }
                    }}>
                    recompute → NEW draft estimate
                  </button>
                </>
              )}
              {o.status === "rejected" &&
                <span className="badge status-rejected">rejected — closed</span>}
            </div>
          </section>
        );
      })()}
    </div>
  );
}
