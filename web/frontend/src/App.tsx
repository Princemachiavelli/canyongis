import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ExternalLink, Eye, Layers, Map, Play, RefreshCw, Square } from "lucide-react";
import { buildDem, listJobs, runDrainage, runRavineCompare, runRavineSpine, type Bounds, type Job } from "./api";
import { MapView } from "./MapView";
import { useAppStore } from "./state";

function jobBoundsWgs84(job: Job): Bounds | undefined {
  const arr = job.result?.bounds_wgs84 as number[] | undefined;
  if (!arr || arr.length !== 4) return undefined;
  return { west: arr[0], south: arr[1], east: arr[2], north: arr[3] };
}

function previewUrl(job: Job): { label: string; url: string } | undefined {
  const result = job.result ?? {};
  if (typeof result.stream_preview_url === "string") {
    return { label: "Stream network", url: result.stream_preview_url };
  }
  if (typeof result.flow_preview_url === "string") {
    return { label: "Flow accumulation", url: result.flow_preview_url };
  }
  if (typeof result.panel_b_tpi_url === "string") {
    return { label: "TPI", url: result.panel_b_tpi_url };
  }
  if (typeof result.compare_png_url === "string") {
    return { label: "Ravine compare", url: result.compare_png_url };
  }
  return undefined;
}

type Area = {
  demJob: Job;
  name: string;
  bounds: Bounds;
  drainageJobs: Job[];
  compareGroups: Array<{ job: Job; spineJobs: Job[] }>;
};

function groupAreas(jobs: Job[]): Area[] {
  const dems = jobs.filter((j) => j.kind === "build-dem" && j.status === "succeeded");
  const areas: Area[] = [];
  for (const dem of dems) {
    const bounds = jobBoundsWgs84(dem);
    if (!bounds) continue;
    const drainage = jobs.filter(
      (j) => j.kind === "drainage" && (j.params as { dem_job_id?: string }).dem_job_id === dem.id
    );
    const compares = jobs.filter(
      (j) => j.kind === "ravine-compare" && (j.params as { dem_job_id?: string }).dem_job_id === dem.id
    );
    const compareGroups = compares.map((c) => ({
      job: c,
      spineJobs: jobs.filter(
        (j) =>
          j.kind === "ravine-spine" &&
          (j.params as { compare_job_id?: string }).compare_job_id === c.id
      )
    }));
    const name = (dem.params as { name?: string }).name ?? dem.id;
    areas.push({ demJob: dem, name, bounds, drainageJobs: drainage, compareGroups });
  }
  return areas;
}

function formatBounds(b: Bounds): string {
  return `${b.west.toFixed(4)}, ${b.south.toFixed(4)} → ${b.east.toFixed(4)}, ${b.north.toFixed(4)}`;
}

function aoiLongEdgeKm(b: Bounds): number {
  const midLat = (b.north + b.south) / 2;
  const dLatKm = (b.north - b.south) * 111;
  const dLonKm = (b.east - b.west) * 111 * Math.cos((midLat * Math.PI) / 180);
  return Math.max(dLatKm, dLonKm);
}

function nominatimZoom(km: number): number {
  if (km < 2) return 16;
  if (km < 10) return 13;
  if (km < 50) return 10;
  if (km < 200) return 8;
  return 6;
}

function useAoiName(bounds: Bounds) {
  const lat = (bounds.north + bounds.south) / 2;
  const lon = (bounds.east + bounds.west) / 2;
  const zoom = nominatimZoom(aoiLongEdgeKm(bounds));
  return useQuery({
    queryKey: ["aoi-name", lat.toFixed(3), lon.toFixed(3), zoom],
    queryFn: async () => {
      const url = `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lon}&zoom=${zoom}`;
      const res = await fetch(url, { headers: { "Accept-Language": "en" } });
      if (!res.ok) throw new Error(`nominatim ${res.status}`);
      const data = (await res.json()) as { display_name?: string };
      return String(data.display_name ?? "").split(",")[0].trim() || undefined;
    },
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1
  });
}

function StatusBadge({ status }: { status: Job["status"] }) {
  return <span className={`statusBadge ${status}`}>{status}</span>;
}

function ProgressTail({ job }: { job: Job }) {
  if (!job.progress || job.progress.length === 0) return null;
  if (job.status !== "running" && job.status !== "queued" && job.status !== "failed") return null;
  const tail = job.progress.slice(-4);
  return (
    <pre className="progressTail">
      {tail.map((line, i) => (
        <div key={i}>{line}</div>
      ))}
    </pre>
  );
}

function DerivedRow({ job, role }: { job: Job; role: "drainage" | "compare" | "spine" }) {
  const setActiveCompareJob = useAppStore((s) => s.setActiveCompareJob);
  const setOverlay = useAppStore((s) => s.setOverlay);
  const preview = previewUrl(job);
  const bounds = jobBoundsWgs84(job);
  const spinesUrl = job.result?.spines_kml_url as string | undefined;
  const rappelUrl = job.result?.rappel_candidates_kml_url as string | undefined;
  return (
    <div className={`derivedRow ${job.status}`}>
      <div className="derivedRowHead">
        <code>{job.id}</code>
        <StatusBadge status={job.status} />
      </div>
      <div className="derivedRowActions">
        {role === "compare" && job.status === "succeeded" && (
          <button onClick={() => setActiveCompareJob(job)}>Use as TPI</button>
        )}
        {preview && bounds && job.status === "succeeded" && (
          <button
            onClick={() =>
              setOverlay({ id: job.id, name: preview.label, url: preview.url, bounds })
            }
          >
            <Eye size={12} />
            Show {preview.label}
          </button>
        )}
        {spinesUrl && (
          <a className="linkButton" href={spinesUrl} target="_blank" rel="noreferrer">
            <ExternalLink size={12} />
            Spines KML
          </a>
        )}
        {rappelUrl && (
          <a className="linkButton" href={rappelUrl} target="_blank" rel="noreferrer">
            <ExternalLink size={12} />
            Rappels KML
          </a>
        )}
      </div>
      <ProgressTail job={job} />
    </div>
  );
}

function AreaCard({ area }: { area: Area }) {
  const setActiveDemJob = useAppStore((s) => s.setActiveDemJob);
  const toggleFootprint = useAppStore((s) => s.toggleFootprint);
  const requestPan = useAppStore((s) => s.requestPan);
  const footprintVisible = useAppStore((s) => Boolean(s.footprints[area.demJob.id]));
  const activeDemId = useAppStore((s) => s.activeDemJob?.id);
  const isActive = activeDemId === area.demJob.id;
  const friendly = useAoiName(area.bounds);
  const label = friendly.data ?? area.name;

  return (
    <div className={`area ${isActive ? "active" : ""}`}>
      <div className="areaHead">
        <div>
          <button
            type="button"
            className="areaNameButton"
            onClick={() => requestPan(area.bounds)}
            title={`Snap map to ${label}`}
          >
            {label}
          </button>
          {friendly.data && area.name !== friendly.data && (
            <code className="areaSubName">{area.name}</code>
          )}
          <code>{area.demJob.id}</code>
        </div>
      </div>
      <div className="areaBounds">{formatBounds(area.bounds)}</div>
      <div className="areaActions">
        <button
          className={footprintVisible ? "toggled" : ""}
          onClick={() =>
            toggleFootprint({ id: area.demJob.id, name: area.name, bounds: area.bounds })
          }
        >
          <Square size={12} />
          {footprintVisible ? "Hide footprint" : "Show footprint"}
        </button>
        <button onClick={() => setActiveDemJob(area.demJob)} disabled={isActive}>
          {isActive ? "Active DEM" : "Use as DEM"}
        </button>
      </div>

      {area.compareGroups.length > 0 && (
        <details open className="derivedSection">
          <summary>TPI / Compare ({area.compareGroups.length})</summary>
          <div className="derivedList">
            {area.compareGroups.map((group) => (
              <div key={group.job.id} className="compareGroup">
                <DerivedRow job={group.job} role="compare" />
                {group.spineJobs.length > 0 && (
                  <div className="spineNest">
                    {group.spineJobs.map((s) => (
                      <DerivedRow key={s.id} job={s} role="spine" />
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </details>
      )}

      {area.drainageJobs.length > 0 && (
        <details className="derivedSection">
          <summary>Drainage ({area.drainageJobs.length})</summary>
          <div className="derivedList">
            {area.drainageJobs.map((j) => (
              <DerivedRow key={j.id} job={j} role="drainage" />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function App() {
  const queryClient = useQueryClient();
  const bounds = useAppStore((state) => state.bounds);
  const activeDemJob = useAppStore((state) => state.activeDemJob);
  const activeCompareJob = useAppStore((state) => state.activeCompareJob);
  const overlay = useAppStore((state) => state.overlay);
  const setBounds = useAppStore((state) => state.setBounds);
  const setOverlay = useAppStore((state) => state.setOverlay);
  const clearFootprints = useAppStore((state) => state.clearFootprints);
  const footprintCount = useAppStore((state) => Object.keys(state.footprints).length);
  const [name, setName] = useState("aoi");
  const [bufferM, setBufferM] = useState(200);

  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: listJobs,
    refetchInterval: 2000
  });

  const jobs = jobsQuery.data ?? [];
  const areas = useMemo(() => groupAreas(jobs), [jobs]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["jobs"] });
  const buildMutation = useMutation({
    mutationFn: () => {
      if (!bounds) throw new Error("Draw or select an AOI first");
      return buildDem(name, bounds, bufferM);
    },
    onSuccess: invalidate
  });
  const drainageMutation = useMutation({
    mutationFn: () => {
      if (!activeDemJob) throw new Error("Select a completed DEM job");
      return runDrainage(activeDemJob.id);
    },
    onSuccess: invalidate
  });
  const compareMutation = useMutation({
    mutationFn: () => {
      if (!activeDemJob) throw new Error("Select a completed DEM job");
      return runRavineCompare(activeDemJob.id);
    },
    onSuccess: invalidate
  });
  const spineMutation = useMutation({
    mutationFn: () => {
      if (!activeCompareJob) throw new Error("Select a completed TPI job");
      return runRavineSpine(activeCompareJob.id);
    },
    onSuccess: invalidate
  });

  const error = useMemo(() => {
    return (
      buildMutation.error ??
      drainageMutation.error ??
      compareMutation.error ??
      spineMutation.error ??
      jobsQuery.error
    );
  }, [buildMutation.error, drainageMutation.error, compareMutation.error, spineMutation.error, jobsQuery.error]);

  return (
    <main className="shell">
      <section className="toolbar">
        <div className="brand">
          <Map size={18} />
          <span>canyongis</span>
        </div>
        <button onClick={() => jobsQuery.refetch()}>
          <RefreshCw size={16} />
        </button>
      </section>

      <section className="workspace">
        <aside className="panel">
          <div className="panelHeader">
            <Layers size={16} />
            <h1>AOI Tools</h1>
          </div>

          <label>
            Area name
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <label>
            DEM buffer meters
            <input
              type="number"
              min={0}
              step={50}
              value={bufferM}
              onChange={(event) => setBufferM(Number(event.target.value))}
            />
          </label>
          <button className="primary" disabled={!bounds || buildMutation.isPending} onClick={() => buildMutation.mutate()}>
            <Play size={16} />
            Build DEM Mosaic
          </button>

          <div className="toolGroup">
            <button disabled={!activeDemJob || drainageMutation.isPending} onClick={() => drainageMutation.mutate()}>
              Flow Preview
            </button>
            <button disabled={!activeDemJob || compareMutation.isPending} onClick={() => compareMutation.mutate()}>
              TPI Preview
            </button>
            <button disabled={!activeCompareJob || spineMutation.isPending} onClick={() => spineMutation.mutate()}>
              Ravine Spine
            </button>
          </div>

          <div className="selection">
            <div>
              <span>AOI</span>
              <strong>{bounds ? `${bounds.west.toFixed(4)}, ${bounds.south.toFixed(4)}` : "none"}</strong>
            </div>
            {bounds && <button onClick={() => setBounds(undefined)}>Clear AOI</button>}
            <div>
              <span>DEM job</span>
              <strong>{activeDemJob?.id ?? "none"}</strong>
            </div>
            <div>
              <span>TPI job</span>
              <strong>{activeCompareJob?.id ?? "none"}</strong>
            </div>
            <div>
              <span>Overlay</span>
              <strong>{overlay?.name ?? "none"}</strong>
            </div>
            {overlay && <button onClick={() => setOverlay(undefined)}>Clear overlay</button>}
            {footprintCount > 0 && (
              <button onClick={() => clearFootprints()}>Hide all footprints ({footprintCount})</button>
            )}
          </div>

          {error && <pre className="error">{String(error)}</pre>}

          <div className="areas">
            <div className="jobsHeader">
              <Layers size={16} />
              <h2>Areas</h2>
            </div>
            {areas.length === 0 && <div className="empty">No DEMs built yet.</div>}
            {areas.map((area) => (
              <AreaCard key={area.demJob.id} area={area} />
            ))}
          </div>

          <details className="allJobs">
            <summary>
              <Activity size={14} />
              All jobs ({jobs.length})
            </summary>
            <div className="jobs">
              {jobs.map((job) => (
                <div key={job.id} className={`job ${job.status}`}>
                  <div>
                    <strong>{job.kind}</strong>
                    <code>{job.id}</code>
                  </div>
                  <StatusBadge status={job.status} />
                </div>
              ))}
            </div>
          </details>
        </aside>

        <section className="mapFrame">
          <MapView />
        </section>
      </section>
    </main>
  );
}
