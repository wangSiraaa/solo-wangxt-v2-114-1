import React, { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import PlotMap from "./components/PlotMap.jsx";
import PlotDetail from "./components/PlotDetail.jsx";
import ConflictsWorkbench from "./components/ConflictsWorkbench.jsx";
import CorrectionsWorkbench from "./components/CorrectionsWorkbench.jsx";
import EstimatePanel from "./components/EstimatePanel.jsx";

const TABS = ["map", "conflicts", "corrections", "estimates"];

export default function App() {
  const [tab, setTab] = useState("map");
  const [plots, setPlots] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  const [t1, setT1] = useState(null);
  const [t2, setT2] = useState(null);
  const [m1, setM1] = useState([]);
  const [m2, setM2] = useState([]);
  const [selectedPlot, setSelectedPlot] = useState(null);
  const [conflicts, setConflicts] = useState([]);
  const [corrections, setCorrections] = useState([]);
  const [error, setError] = useState("");

  async function refreshConflicts() {
    setConflicts(await api.conflicts("open"));
  }

  async function refreshCorrections() {
    setCorrections(await api.corrections());
  }

  useEffect(() => {
    (async () => {
      try {
        const [ps, cs] = await Promise.all([api.plots(), api.campaigns()]);
        setPlots(ps);
        setCampaigns(cs);
        const ordered = [...cs].sort((a, b) =>
          a.measured_on.localeCompare(b.measured_on));
        if (ordered.length >= 2) {
          setT1(ordered[0].code);
          setT2(ordered[ordered.length - 1].code);
        }
        const [oc, cc] = await Promise.all([
          api.conflicts("open"), api.corrections()]);
        setConflicts(oc);
        setCorrections(cc);
      } catch (e) {
        setError(e.message);
      }
    })();
  }, []);

  const [measurementVersion, setMeasurementVersion] = useState(0);
  useEffect(() => {
    if (!t1 || !t2) return;
    (async () => {
      const [a, b] = await Promise.all([
        api.measurements(t1), api.measurements(t2)]);
      setM1(a);
      setM2(b);
    })();
  }, [t1, t2, measurementVersion]);

  const ctx = useMemo(() => ({
    plots, campaigns, t1, t2, m1, m2, conflicts, corrections,
    setSelectedPlot, refreshConflicts, refreshCorrections,
    reloadMeasurements: () => setMeasurementVersion((v) => v + 1),
  }), [plots, campaigns, t1, t2, m1, m2, conflicts, corrections]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Fixed-plot remeasurement station</h1>
        <div className="meta">
          {campaigns.map((c) => (
            <span key={c.code} className="chip">
              {c.code} · {c.measured_on}
            </span>
          ))}
          <span className="chip warn-chip">
            {conflicts.length} open identity conflict
            {conflicts.length === 1 ? "" : "s"}
          </span>
          {(() => {
            const openCorr = corrections.filter(
              (c) => c.status !== "applied" && c.status !== "rejected");
            return openCorr.length > 0 ? (
              <span className="chip corr-chip">
                {openCorr.length} open correction order
                {openCorr.length === 1 ? "" : "s"}
              </span>
            ) : null;
          })()}
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t} className={tab === t ? "tab active" : "tab"}
                  onClick={() => setTab(t)}>
            {t === "map" ? "Plots & individuals"
              : t === "conflicts"
                  ? `Identity conflicts (${conflicts.length})`
              : t === "corrections"
                  ? `Measurement corrections (${corrections.filter(
                      (c) => c.status !== "applied"
                            && c.status !== "rejected").length})`
              : "Estimates"}
          </button>
        ))}
      </nav>

      <main>
        {tab === "map" && (
          selectedPlot
            ? <PlotDetail plotCode={selectedPlot} ctx={ctx}
                          onBack={() => setSelectedPlot(null)} />
            : <PlotMap ctx={ctx} onSelect={setSelectedPlot} />
        )}
        {tab === "conflicts" && (
          <ConflictsWorkbench ctx={ctx}
                              onChanged={async () => {
                                setConflicts(await api.conflicts("open"));
                              }} />
        )}
        {tab === "corrections" && (
          <CorrectionsWorkbench onChanged={async () => {
            setCorrections(await api.corrections());
            ctx.reloadMeasurements();
          }} />
        )}
        {tab === "estimates" && <EstimatePanel ctx={ctx} />}
      </main>

      <footer>
        Fictional demonstration data · coordinates EPSG:{plots[0]?.crs_epsg}
        {" "}· dbh cm (raw unit retained) · height m · areas in hectares
      </footer>
    </div>
  );
}
