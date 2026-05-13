import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ExternalLink, Eye, Layers, LineChart, LocateFixed, Map as MapIcon, Play, RefreshCw, Square, Tags, Trash2, Upload } from "lucide-react";
import {
  buildDem,
  deleteGeneratedKmlLayer,
  deleteJobRecord,
  importRopewikiKml,
  listJobSpecs,
  listJobs,
  runDemJob,
  runRavineSpine,
  runTrackProfile,
  snapKml,
  uploadKmlFile,
  type Bounds,
  type Job,
  type JobSpec,
  type JobSpecVisualization,
  type KmlSnapTarget,
  type TrackCategory
} from "./api";
import { MapView } from "./MapView";
import { useAppStore } from "./state";

function jobBoundsWgs84(job: Job): Bounds | undefined {
  const arr = job.result?.bounds_wgs84 as number[] | undefined;
  if (!arr || arr.length !== 4) return undefined;
  return { west: arr[0], south: arr[1], east: arr[2], north: arr[3] };
}

type PreviewLink = { key: string; label: string; url: string };

function previewsFor(job: Job, spec: JobSpec): PreviewLink[] {
  const result = (job.result ?? {}) as Record<string, unknown>;
  const out: PreviewLink[] = [];
  for (const vis of spec.visualizations) {
    const url = result[vis.url_field];
    if (typeof url === "string") out.push({ key: vis.key, label: vis.label, url });
  }
  return out;
}

type Area = {
  demJob: Job;
  name: string;
  bounds: Bounds;
  jobsBySpec: Map<string, Job[]>;
  spineJobsByTpi: Map<string, Job[]>;
};

function tpiJobIdFromSpine(job: Job): string | undefined {
  const params = job.params as { tpi_job_id?: string; compare_job_id?: string };
  return params.tpi_job_id ?? params.compare_job_id;
}

function groupAreas(jobs: Job[], specs: JobSpec[]): Area[] {
  const dems = jobs.filter((j) => j.kind === "build-dem" && j.status === "succeeded");
  const specKeys = new Set(specs.map((s) => s.key));
  const areas: Area[] = [];
  for (const dem of dems) {
    const bounds = jobBoundsWgs84(dem);
    if (!bounds) continue;
    const jobsBySpec = new Map<string, Job[]>();
    for (const spec of specs) jobsBySpec.set(spec.key, []);
    const spineJobsByTpi = new Map<string, Job[]>();
    for (const j of jobs) {
      const demId = (j.params as { dem_job_id?: string }).dem_job_id;
      if (specKeys.has(j.kind) && demId === dem.id) {
        jobsBySpec.get(j.kind)?.push(j);
      }
      if (j.kind === "ravine-spine") {
        const tpiId = tpiJobIdFromSpine(j);
        if (tpiId) {
          if (!spineJobsByTpi.has(tpiId)) spineJobsByTpi.set(tpiId, []);
          spineJobsByTpi.get(tpiId)?.push(j);
        }
      }
    }
    const name = (dem.params as { name?: string }).name ?? dem.id;
    areas.push({ demJob: dem, name, bounds, jobsBySpec, spineJobsByTpi });
  }
  return areas;
}

function ravineSpineLayerJobs(jobs: Job[]): Job[] {
  const layers: Job[] = [];
  for (const job of jobs) {
    if (job.kind !== "ravine-spine" || job.status !== "succeeded" || !job.result) continue;
    const fallbackBounds = job.result.bounds_wgs84;
    const parentKmlJobId = job.params.parent_kml_job_id ?? job.result.parent_kml_job_id;
    const tpiJobId = tpiJobIdFromSpine(job);
    const compareJob = tpiJobId ? jobs.find((candidate) => candidate.id === tpiJobId) : undefined;
    const containerDemJobId = compareJob?.params.dem_job_id;
    for (const [key, label] of [
      ["spines_layer", "Ravine spines"],
      ["rappel_candidates_layer", "Rappel candidates"]
    ] as const) {
      const layer = job.result[key];
      if (!layer || typeof layer !== "object") continue;
      const result = layer as Record<string, unknown>;
      layers.push({
        id: `${job.id}:${key}`,
        kind: "kml-layer",
        status: "succeeded",
        created_at: job.created_at,
        updated_at: job.updated_at,
        params: {
          source_job_id: job.id,
          layer: key,
          parent_kml_job_id: parentKmlJobId,
          container_dem_job_id: containerDemJobId
        },
        result: {
          ...result,
          name: `${label} ${job.id}`,
          bounds_wgs84: result.bounds_wgs84 ?? fallbackBounds,
          parent_kml_job_id: parentKmlJobId,
          container_dem_job_id: containerDemJobId,
          source: { kind: "ravine-spine", job_id: job.id, layer: key, parent_kml_job_id: parentKmlJobId }
        }
      });
    }
  }
  return layers;
}

function boundsContains(outer: Bounds, inner: Bounds): boolean {
  const eps = 1e-9;
  return (
    inner.west >= outer.west - eps &&
    inner.south >= outer.south - eps &&
    inner.east <= outer.east + eps &&
    inner.north <= outer.north + eps
  );
}

function kmlContainerDemId(job: Job): string | undefined {
  const resultId = job.result?.container_dem_job_id;
  if (typeof resultId === "string" && resultId) return resultId;
  const paramId = job.params.container_dem_job_id;
  return typeof paramId === "string" && paramId ? paramId : undefined;
}

type KmlContainerGroups = {
  groups: Array<{ area: Area; kmls: Job[]; generated: Job[] }>;
  loose: Job[];
  looseGenerated: Job[];
};

function parentKmlJobId(job: Job): string | undefined {
  const paramId = job.params.parent_kml_job_id;
  if (typeof paramId === "string" && paramId) return paramId;
  const resultId = job.result?.parent_kml_job_id;
  return typeof resultId === "string" && resultId ? resultId : undefined;
}

function groupKmlContainers(areas: Area[], kmlJobs: Job[], generatedKmlJobs: Job[]): KmlContainerGroups {
  const byArea = new Map<string, Job[]>();
  const generatedByArea = new Map<string, Job[]>();
  const loose: Job[] = [];
  const looseGenerated: Job[] = [];
  for (const area of areas) byArea.set(area.demJob.id, []);
  for (const area of areas) generatedByArea.set(area.demJob.id, []);
  for (const job of kmlJobs) {
    const explicitDemId = kmlContainerDemId(job);
    if (explicitDemId && byArea.has(explicitDemId)) {
      byArea.get(explicitDemId)?.push(job);
      continue;
    }
    const bounds = jobBoundsWgs84(job);
    const containingArea = bounds ? areas.find((area) => boundsContains(area.bounds, bounds)) : undefined;
    if (containingArea) {
      byArea.get(containingArea.demJob.id)?.push(job);
    } else {
      loose.push(job);
    }
  }
  for (const job of generatedKmlJobs) {
    const explicitDemId = kmlContainerDemId(job);
    if (explicitDemId && generatedByArea.has(explicitDemId)) {
      generatedByArea.get(explicitDemId)?.push(job);
      continue;
    }
    const bounds = jobBoundsWgs84(job);
    const containingArea = bounds ? areas.find((area) => boundsContains(area.bounds, bounds)) : undefined;
    if (containingArea) {
      generatedByArea.get(containingArea.demJob.id)?.push(job);
    } else {
      looseGenerated.push(job);
    }
  }
  return {
    groups: areas.map((area) => ({
      area,
      kmls: byArea.get(area.demJob.id) ?? [],
      generated: generatedByArea.get(area.demJob.id) ?? []
    })),
    loose,
    looseGenerated
  };
}

function childLayersFor(parent: Job, generated: Job[], siblingKmls: Job[]): Job[] {
  return generated.filter((layer) => {
    const explicitParent = parentKmlJobId(layer);
    if (explicitParent) return explicitParent === parent.id;
    return siblingKmls.length === 1 && siblingKmls[0].id === parent.id;
  });
}

function unparentedGeneratedLayers(generated: Job[], siblingKmls: Job[]): Job[] {
  return generated.filter((layer) => {
    if (parentKmlJobId(layer)) return false;
    return siblingKmls.length !== 1;
  });
}

function formatBounds(b: Bounds): string {
  return `${b.west.toFixed(4)}, ${b.south.toFixed(4)} → ${b.east.toFixed(4)}, ${b.north.toFixed(4)}`;
}

function formatMeters(meters?: number | null): string {
  if (typeof meters !== "number" || !Number.isFinite(meters)) return "-";
  if (Math.abs(meters) < 1000) return `${Math.round(meters)} m`;
  return `${(meters / 1000).toFixed(Math.abs(meters) < 10000 ? 2 : 1)} km`;
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

function ProgressTail({ job, onDelete }: { job: Job; onDelete?: (job: Job) => void }) {
  if (job.status !== "running" && job.status !== "queued" && job.status !== "failed") return null;
  const tailCount = job.status === "failed" ? 20 : 4;
  const tail = (job.progress ?? []).slice(-tailCount);
  if (tail.length === 0 && !job.error) return null;
  if (job.status === "failed") {
    return (
      <details className="jobLogDetails">
        <summary>Error log</summary>
        {onDelete && (
          <button className="danger miniButton" onClick={() => onDelete(job)}>
            <Trash2 size={12} />
            Delete failed job
          </button>
        )}
        {tail.length > 0 && (
          <pre className="progressTail failed">
            {tail.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </pre>
        )}
        {job.error && <pre className="jobError">{job.error}</pre>}
      </details>
    );
  }
  return (
    <pre className="progressTail">
      {tail.map((line, i) => (
        <div key={i}>{line}</div>
      ))}
    </pre>
  );
}

function isTpiProducer(job: Job): boolean {
  return typeof (job.result as Record<string, unknown> | undefined)?.b_tpi_path === "string";
}

function DerivedRow({
  job,
  spec,
  demJob,
  onDelete
}: {
  job: Job;
  spec?: JobSpec;
  demJob?: Job;
  onDelete?: (job: Job) => void;
}) {
  const setActiveDemJob = useAppStore((s) => s.setActiveDemJob);
  const setActiveCompareJob = useAppStore((s) => s.setActiveCompareJob);
  const setOverlay = useAppStore((s) => s.setOverlay);
  const overlay = useAppStore((s) => s.overlay);
  const activeCompareId = useAppStore((s) => s.activeCompareJob?.id);
  const previews = spec ? previewsFor(job, spec) : [];
  const bounds = jobBoundsWgs84(job);
  const canBeTpi = isTpiProducer(job);
  const isActiveTpi = canBeTpi && activeCompareId === job.id;
  const useAsTpi = () => {
    if (demJob) setActiveDemJob(demJob);
    setActiveCompareJob(job);
  };
  return (
    <div className={`derivedRow ${job.status} ${isActiveTpi ? "active" : ""}`}>
      <div className="derivedRowHead">
        <code>{job.id}</code>
        <StatusBadge status={job.status} />
      </div>
      <div className="derivedRowActions">
        {canBeTpi && job.status === "succeeded" && (
          <button className={isActiveTpi ? "toggled" : ""} disabled={isActiveTpi} onClick={useAsTpi}>
            {isActiveTpi ? "Active TPI" : "Use as TPI"}
          </button>
        )}
        {bounds &&
          job.status === "succeeded" &&
          previews.map((preview) => {
            const overlayId = `${job.id}:${preview.key}`;
            const isActive = overlay?.id === overlayId;
            return (
              <button
                key={preview.key}
                className={isActive ? "toggled" : ""}
                onClick={() =>
                  isActive
                    ? setOverlay(undefined)
                    : setOverlay({
                        id: overlayId,
                        name: preview.label,
                        url: preview.url,
                        bounds
                      })
                }
              >
                <Eye size={12} />
                {preview.label}
              </button>
            );
          })}
      </div>
      <ProgressTail job={job} onDelete={onDelete} />
    </div>
  );
}

function JobSpecSection({
  area,
  spec,
  runs,
  spineJobsByTpi,
  onRunJob,
  onDeleteJob,
  isPending
}: {
  area: Area;
  spec: JobSpec;
  runs: Job[];
  spineJobsByTpi: Map<string, Job[]>;
  onRunJob: (demJob: Job, spec: JobSpec) => void;
  onDeleteJob?: (job: Job) => void;
  isPending: boolean;
}) {
  const setOverlay = useAppStore((s) => s.setOverlay);
  const overlay = useAppStore((s) => s.overlay);
  const hasRuns = runs.length > 0;
  const unavailable = !spec.available;
  const runButtonLabel = hasRuns ? "Run again" : "Run";
  const triggerRun = () => {
    if (!unavailable && !isPending) onRunJob(area.demJob, spec);
  };

  return (
    <details className="derivedSection jobSpec" open={hasRuns}>
      <summary>
        <span className="jobSpecLabel">{spec.label}</span>
        {hasRuns && <span className="jobSpecCount">({runs.length})</span>}
        <button
          className="miniButton"
          disabled={unavailable || isPending}
          title={unavailable ? spec.unavailable_reason ?? "Unavailable" : spec.description}
          onClick={(e) => {
            e.preventDefault();
            triggerRun();
          }}
        >
          <Play size={12} />
          {unavailable ? "Unavailable" : runButtonLabel}
        </button>
      </summary>
      <div className="derivedList">
        {!hasRuns && (
          <ul className="visListMuted" onClick={triggerRun}>
            {spec.visualizations.map((v: JobSpecVisualization) => (
              <li key={v.key} className="muted" title="Click to run this job">
                {v.label}
              </li>
            ))}
          </ul>
        )}
        {runs.map((job) => {
          const isTpi = isTpiProducer(job);
          const spineJobs = isTpi ? spineJobsByTpi.get(job.id) ?? [] : [];
          return (
            <div key={job.id} className="jobRunGroup">
              <DerivedRow
                job={job}
                spec={spec}
                demJob={area.demJob}
                onDelete={onDeleteJob}
              />
              {spineJobs.length > 0 && (
                <div className="spineNest">
                  {spineJobs.map((s) => (
                    <DerivedRow key={s.id} job={s} onDelete={onDeleteJob} />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}

function AreaCard({
  area,
  specs,
  onRunJob,
  pendingKey,
  onDeleteJob
}: {
  area: Area;
  specs: JobSpec[];
  onRunJob: (demJob: Job, spec: JobSpec) => void;
  pendingKey?: string;
  onDeleteJob?: (job: Job) => void;
}) {
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

      <div className="jobSpecList">
        {specs.map((spec) => {
          const runs = area.jobsBySpec.get(spec.key) ?? [];
          const key = `${area.demJob.id}:${spec.key}`;
          return (
            <JobSpecSection
              key={spec.key}
              area={area}
              spec={spec}
              runs={runs}
              spineJobsByTpi={area.spineJobsByTpi}
              onRunJob={onRunJob}
              onDeleteJob={onDeleteJob}
              isPending={pendingKey === key}
            />
          );
        })}
      </div>
    </div>
  );
}

function KmlImportRow({
  job,
  activeDemJob,
  activeCompareJob,
  childLayers = [],
  onSnap,
  onDelete
}: {
  job: Job;
  activeDemJob?: Job;
  activeCompareJob?: Job;
  childLayers?: Job[];
  onSnap?: (job: Job, target: KmlSnapTarget) => void;
  onDelete?: (job: Job) => void;
}) {
  const bounds = jobBoundsWgs84(job);
  const setBounds = useAppStore((s) => s.setBounds);
  const requestPan = useAppStore((s) => s.requestPan);
  const toggleKml = useAppStore((s) => s.toggleKml);
  const toggleKmlLabels = useAppStore((s) => s.toggleKmlLabels);
  const visible = useAppStore((s) => Boolean(s.visibleKmls[job.id]));
  const labelsVisible = useAppStore((s) => Boolean(s.kmlLabels[job.id]));
  const name = (job.result?.name as string | undefined) ?? (job.params.name as string | undefined) ?? job.id;
  const kmlUrl = job.result?.kml_url as string | undefined;
  const canDelete = Boolean(onDelete) && job.status !== "queued" && job.status !== "running";
  const canDeleteLayer =
    canDelete && (job.kind === "kml-import" || job.kind === "kml-snap" || job.kind === "kml-layer");

  return (
    <div className={`kmlImport ${job.status}`}>
      <div className="derivedRowHead">
        <div>
          <strong>{name}</strong>
          <code>{job.id}</code>
        </div>
        <StatusBadge status={job.status} />
      </div>
      {bounds && <div className="areaBounds">{formatBounds(bounds)}</div>}
      <div className="derivedRowActions">
        {bounds && job.status === "succeeded" && (
          <>
            <button className={visible ? "toggled" : ""} onClick={() => toggleKml(job.id)}>
              <Eye size={12} />
              {visible ? "Hide" : "Show"}
            </button>
            <button
              className={labelsVisible ? "toggled" : ""}
              disabled={!visible}
              onClick={() => toggleKmlLabels(job.id)}
            >
              <Tags size={12} />
              Labels
            </button>
            <button onClick={() => setBounds(bounds)}>Use bounds for AOI</button>
            <button onClick={() => requestPan(bounds)}>
              <LocateFixed size={12} />
              Pan to
            </button>
            {onSnap && job.kind === "kml-import" && (
              <>
                <button disabled={!activeDemJob} onClick={() => onSnap(job, "dem")}>
                  Snap DEM
                </button>
                <button disabled={!activeCompareJob} onClick={() => onSnap(job, "tpi")}>
                  Snap TPI
                </button>
                <button disabled={!activeCompareJob} onClick={() => onSnap(job, "geomorphon")}>
                  Snap Geom
                </button>
                <button disabled={!activeCompareJob} onClick={() => onSnap(job, "curvature")}>
                  Snap Curv
                </button>
              </>
            )}
          </>
        )}
        {kmlUrl && (
          <a className="linkButton" href={kmlUrl} target="_blank" rel="noreferrer">
            <ExternalLink size={12} />
            KML
          </a>
        )}
        {canDeleteLayer && (
          <button className="danger miniButton" onClick={() => onDelete?.(job)}>
            <Trash2 size={12} />
            Delete
          </button>
        )}
      </div>
      {childLayers.length > 0 && (
        <div className="spineNest">
          {childLayers.map((child) => (
            <KmlImportRow
              key={child.id}
              job={child}
              activeDemJob={activeDemJob}
              activeCompareJob={activeCompareJob}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
      <ProgressTail job={job} onDelete={onDelete} />
    </div>
  );
}

type ProfileSample = {
  distance_m: number;
  elevation_m: number | null;
  slope?: number | null;
};

type RappelCandidate = {
  distance_m: number;
  elevation_m: number | null;
  drop_m: number;
  length_m: number;
  slope: number;
};

function profileSamples(job?: Job): ProfileSample[] {
  const samples = job?.result?.samples;
  return Array.isArray(samples) ? (samples as ProfileSample[]) : [];
}

function rappelCandidates(job?: Job): RappelCandidate[] {
  const rappels = job?.result?.rappel_candidates;
  return Array.isArray(rappels) ? (rappels as RappelCandidate[]) : [];
}

function ProfileChart({ samples, rappels }: { samples: ProfileSample[]; rappels: RappelCandidate[] }) {
  const points = samples.filter(
    (sample) =>
      typeof sample.distance_m === "number" &&
      typeof sample.elevation_m === "number" &&
      Number.isFinite(sample.distance_m) &&
      Number.isFinite(sample.elevation_m)
  );
  if (points.length < 2) return <div className="empty">No valid DEM samples on this track.</div>;
  const width = 320;
  const height = 120;
  const pad = 12;
  const maxDistance = Math.max(...points.map((p) => p.distance_m));
  const minElevation = Math.min(...points.map((p) => p.elevation_m as number));
  const maxElevation = Math.max(...points.map((p) => p.elevation_m as number));
  const elevationSpan = Math.max(1, maxElevation - minElevation);
  const x = (distance: number) => pad + (distance / Math.max(1, maxDistance)) * (width - pad * 2);
  const y = (elevation: number) =>
    height - pad - ((elevation - minElevation) / elevationSpan) * (height - pad * 2);
  const d = points
    .map((point, idx) => `${idx === 0 ? "M" : "L"} ${x(point.distance_m).toFixed(1)} ${y(point.elevation_m as number).toFixed(1)}`)
    .join(" ");
  return (
    <svg className="profileChart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Elevation profile">
      <path className="profileFill" d={`${d} L ${x(maxDistance)} ${height - pad} L ${pad} ${height - pad} Z`} />
      <path className="profileLine" d={d} />
      {rappels.map((rappel, idx) => (
        <line
          key={idx}
          className="profileRappel"
          x1={x(rappel.distance_m)}
          x2={x(rappel.distance_m)}
          y1={pad}
          y2={height - pad}
        />
      ))}
    </svg>
  );
}

function TrackProfilePanel({
  selected,
  activeDemJob,
  profileJob,
  isPending,
  onRun
}: {
  selected?: { kmlJobId: string; category: TrackCategory; trackIndex: number; name: string };
  activeDemJob?: Job;
  profileJob?: Job;
  isPending: boolean;
  onRun: () => void;
}) {
  const clearSelected = useAppStore((s) => s.setSelectedKmlTrack);
  const samples = profileSamples(profileJob);
  const rappels = rappelCandidates(profileJob);
  return (
    <div className="trackPanel">
      <div className="jobsHeader">
        <LineChart size={16} />
        <h2>Track profile</h2>
      </div>
      {!selected && <div className="empty">Click a visible KML line to select a track.</div>}
      {selected && (
        <>
          <div className="selection">
            <div>
              <span>Track</span>
              <strong>{selected.name}</strong>
            </div>
            <div>
              <span>Type</span>
              <strong>
                {selected.category} #{selected.trackIndex + 1}
              </strong>
            </div>
          </div>
          <button disabled={!activeDemJob || isPending} onClick={onRun}>
            <LineChart size={14} />
            Build elevation profile
          </button>
          <button onClick={() => clearSelected(undefined)}>Clear selected track</button>
        </>
      )}
      {profileJob?.status === "running" && <ProgressTail job={profileJob} />}
      {profileJob?.status === "succeeded" && (
        <div className="profileResult">
          <div className="profileStats">
            <span>Length {formatMeters(profileJob.result?.length_m as number | undefined)}</span>
            <span>Drop {formatMeters(profileJob.result?.total_drop_m as number | undefined)}</span>
            <span>Rappels {rappels.length}</span>
          </div>
          <ProfileChart samples={samples} rappels={rappels} />
          {rappels.length > 0 && (
            <div className="rappelList">
              {rappels.map((rappel, idx) => (
                <div key={idx} className="rappelRow">
                  <strong>R{idx + 1}</strong>
                  <span>{formatMeters(rappel.distance_m)}</span>
                  <span>{rappel.drop_m.toFixed(1)} m drop</span>
                  <span>{Math.round(rappel.slope * 100)}%</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {profileJob?.status === "failed" && <ProgressTail job={profileJob} />}
    </div>
  );
}

export function App() {
  const queryClient = useQueryClient();
  const bounds = useAppStore((state) => state.bounds);
  const activeDemJob = useAppStore((state) => state.activeDemJob);
  const activeCompareJob = useAppStore((state) => state.activeCompareJob);
  const selectedKmlTrack = useAppStore((state) => state.selectedKmlTrack);
  const overlay = useAppStore((state) => state.overlay);
  const setBounds = useAppStore((state) => state.setBounds);
  const setOverlay = useAppStore((state) => state.setOverlay);
  const clearFootprints = useAppStore((state) => state.clearFootprints);
  const footprintCount = useAppStore((state) => Object.keys(state.footprints).length);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const ropewikiDialogRef = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("aoi");
  const [bufferM, setBufferM] = useState(200);
  const [snapRadiusM, setSnapRadiusM] = useState(30);
  const [snapStepM, setSnapStepM] = useState(10);
  const [openMapTool, setOpenMapTool] = useState<"aoi" | "kml" | "ravine" | undefined>();
  const [ropewikiSource, setRopewikiSource] = useState("");

  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: listJobs,
    refetchInterval: 2000
  });

  const specsQuery = useQuery({
    queryKey: ["job-specs"],
    queryFn: listJobSpecs,
    staleTime: Infinity,
    gcTime: Infinity
  });

  const jobs = jobsQuery.data ?? [];
  const specs = useMemo(() => specsQuery.data ?? [], [specsQuery.data]);
  const areas = useMemo(() => groupAreas(jobs, specs), [jobs, specs]);
  const baseKmlJobs = useMemo(
    () => jobs.filter((j) => j.kind === "kml-import" || j.kind === "kml-snap"),
    [jobs]
  );
  const generatedKmlJobs = useMemo(() => ravineSpineLayerJobs(jobs), [jobs]);
  const kmlImports = useMemo(() => [...baseKmlJobs, ...generatedKmlJobs], [baseKmlJobs, generatedKmlJobs]);
  const kmlContainers = useMemo(
    () => groupKmlContainers(areas, baseKmlJobs, generatedKmlJobs),
    [areas, baseKmlJobs, generatedKmlJobs]
  );
  const profileJobs = useMemo(() => jobs.filter((j) => j.kind === "track-profile"), [jobs]);
  const selectedProfileJob = useMemo(
    () =>
      selectedKmlTrack
        ? profileJobs.find((job) => {
            const params = job.params as {
              kml_job_id?: string;
              category?: string;
              track_index?: number;
              dem_job_id?: string;
            };
            return (
              params.kml_job_id === selectedKmlTrack.kmlJobId &&
              params.category === selectedKmlTrack.category &&
              params.track_index === selectedKmlTrack.trackIndex &&
              Boolean(activeDemJob) &&
              params.dem_job_id === activeDemJob?.id
            );
          })
        : undefined,
    [activeDemJob, profileJobs, selectedKmlTrack]
  );

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["jobs"] });
  const buildMutation = useMutation({
    mutationFn: (payload: { name: string; bounds: Bounds; bufferM: number }) =>
      buildDem(payload.name, payload.bounds, payload.bufferM),
    onSuccess: invalidate
  });
  const [pendingRunKey, setPendingRunKey] = useState<string | undefined>();
  const runJobMutation = useMutation({
    mutationFn: ({ demJob, spec }: { demJob: Job; spec: JobSpec }) =>
      runDemJob(demJob.id, spec.key, {}),
    onMutate: ({ demJob, spec }) => {
      setPendingRunKey(`${demJob.id}:${spec.key}`);
    },
    onSettled: () => {
      setPendingRunKey(undefined);
      invalidate();
    }
  });
  const onRunJob = (demJob: Job, spec: JobSpec) =>
    runJobMutation.mutate({ demJob, spec });
  const spineMutation = useMutation({
    mutationFn: () => {
      if (!activeCompareJob) throw new Error("Select a completed TPI job");
      return runRavineSpine(activeCompareJob.id, selectedKmlTrack?.kmlJobId);
    },
    onSuccess: invalidate
  });
  const uploadKmlMutation = useMutation({
    mutationFn: (file: File) => uploadKmlFile(file, activeDemJob?.id),
    onSuccess: () => {
      invalidate();
    }
  });
  const ropewikiKmlMutation = useMutation({
    mutationFn: (source: string) => {
      if (!source) throw new Error("Enter a Ropewiki URL or title");
      return importRopewikiKml(source, activeDemJob?.id);
    },
    onSuccess: invalidate
  });
  const snapKmlMutation = useMutation({
    mutationFn: ({
      job,
      target,
      demJob,
      searchRadiusM,
      sampleStepM
    }: {
      job: Job;
      target: KmlSnapTarget;
      demJob?: Job;
      searchRadiusM: number;
      sampleStepM: number;
    }) => {
      if (target === "dem") {
        const targetDemJob = demJob ?? activeDemJob;
        if (!targetDemJob) throw new Error("Select a completed DEM job first");
        return snapKml(job.id, {
          target,
          dem_job_id: targetDemJob.id,
          search_radius_m: searchRadiusM,
          sample_step_m: sampleStepM
        });
      }
      if (!activeCompareJob) throw new Error("Select a completed TPI job first");
      return snapKml(job.id, {
        target,
        compare_job_id: activeCompareJob.id,
        search_radius_m: searchRadiusM,
        sample_step_m: sampleStepM
      });
    },
    onSuccess: invalidate
  });
  const trackProfileMutation = useMutation({
    mutationFn: () => {
      if (!selectedKmlTrack) throw new Error("Click a visible KML track first");
      if (!activeDemJob) throw new Error("Select a completed DEM job first");
      return runTrackProfile(selectedKmlTrack.kmlJobId, {
        dem_job_id: activeDemJob.id,
        category: selectedKmlTrack.category,
        track_index: selectedKmlTrack.trackIndex
      });
    },
    onSuccess: invalidate
  });
  const deleteJobMutation = useMutation({
    mutationFn: (job: Job) => {
      if (job.kind === "kml-layer") {
        const sourceJobId = job.params.source_job_id;
        const layer = job.params.layer;
        if (typeof sourceJobId !== "string" || typeof layer !== "string") {
          throw new Error("Generated KML layer is missing source metadata");
        }
        return deleteGeneratedKmlLayer(sourceJobId, layer);
      }
      return deleteJobRecord(job.id);
    },
    onSuccess: invalidate
  });
  const deleteJob = (job: Job) => deleteJobMutation.mutate(job);

  const promptNumber = (label: string, current: number, min: number): number | undefined => {
    const raw = window.prompt(label, String(current));
    if (raw === null) return undefined;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < min) {
      window.alert(`${label} must be ${min > 0 ? "a positive number" : "zero or greater"}.`);
      return undefined;
    }
    return value;
  };

  const runBuildDem = () => {
    if (!bounds) {
      window.alert("Draw or select an AOI first.");
      return;
    }
    const nextName = window.prompt("Area name", name)?.trim();
    if (nextName === undefined) return;
    const nextBuffer = promptNumber("DEM buffer meters", bufferM, 0);
    if (nextBuffer === undefined) return;
    const finalName = nextName || "aoi";
    setName(finalName);
    setBufferM(nextBuffer);
    buildMutation.mutate({ name: finalName, bounds, bufferM: nextBuffer });
  };

  const runRopewikiImport = () => {
    const source = ropewikiSource.trim();
    if (!source) return;
    ropewikiKmlMutation.mutate(source, {
      onSuccess: () => {
        setRopewikiSource("");
        ropewikiDialogRef.current?.close();
      }
    });
  };

  const openRopewikiDialog = () => {
    const dialog = ropewikiDialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
  };

  const runSnap = (job: Job, target: KmlSnapTarget, demJob?: Job) => {
    const searchRadiusM = promptNumber("Snap radius meters", snapRadiusM, 1);
    if (searchRadiusM === undefined) return;
    const sampleStepM = promptNumber("Snap sample step meters", snapStepM, 1);
    if (sampleStepM === undefined) return;
    setSnapRadiusM(searchRadiusM);
    setSnapStepM(sampleStepM);
    snapKmlMutation.mutate({ job, target, demJob, searchRadiusM, sampleStepM });
  };

  const selectedAoiText = bounds ? formatBounds(bounds) : "No AOI selected";

  const error = useMemo(() => {
    return (
      buildMutation.error ??
      runJobMutation.error ??
      spineMutation.error ??
      uploadKmlMutation.error ??
      ropewikiKmlMutation.error ??
      snapKmlMutation.error ??
      trackProfileMutation.error ??
      deleteJobMutation.error ??
      jobsQuery.error
    );
  }, [
    buildMutation.error,
    runJobMutation.error,
    spineMutation.error,
    uploadKmlMutation.error,
    ropewikiKmlMutation.error,
    snapKmlMutation.error,
    trackProfileMutation.error,
    deleteJobMutation.error,
    jobsQuery.error
  ]);

  return (
    <main className="shell">
      <section className="toolbar">
        <div className="brand">
          <MapIcon size={18} />
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
            <h1>Jobs & Layers</h1>
          </div>

          <TrackProfilePanel
            selected={selectedKmlTrack}
            activeDemJob={activeDemJob}
            profileJob={selectedProfileJob}
            isPending={trackProfileMutation.isPending}
            onRun={() => trackProfileMutation.mutate()}
          />

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
              <MapIcon size={16} />
              <h2>Containers & KML</h2>
            </div>
            {areas.length === 0 && kmlImports.length === 0 && (
              <div className="empty">No DEM containers or KMLs yet.</div>
            )}
            {kmlContainers.groups.map(({ area, kmls, generated }) => {
              const unparentedGenerated = unparentedGeneratedLayers(generated, kmls);
              return (
              <details key={area.demJob.id} open={kmls.length > 0} className="derivedSection">
                <summary>
                  {area.name} ({kmls.length} KML)
                </summary>
                <div className="derivedList">
                  <AreaCard
                    area={area}
                    specs={specs}
                    onRunJob={onRunJob}
                    pendingKey={pendingRunKey}
                    onDeleteJob={deleteJob}
                  />
                  {kmls.length === 0 && <div className="empty">No KMLs in this DEM container.</div>}
                  {kmls.map((job) => (
                    <KmlImportRow
                      key={job.id}
                      job={job}
                      childLayers={childLayersFor(job, generated, kmls)}
                      activeDemJob={area.demJob}
                      activeCompareJob={activeCompareJob}
                      onSnap={(sourceJob, target) => runSnap(sourceJob, target, area.demJob)}
                      onDelete={deleteJob}
                    />
                  ))}
                  {unparentedGenerated.length > 0 && (
                    <details open className="derivedSection">
                      <summary>Generated KML ({unparentedGenerated.length})</summary>
                      <div className="derivedList">
                        {unparentedGenerated.map((job) => (
                          <KmlImportRow
                            key={job.id}
                            job={job}
                            activeDemJob={area.demJob}
                            activeCompareJob={activeCompareJob}
                            onDelete={deleteJob}
                          />
                        ))}
                      </div>
                    </details>
                  )}
                </div>
              </details>
            );
            })}
            {(kmlContainers.loose.length > 0 || kmlContainers.looseGenerated.length > 0) && (
              <details open className="derivedSection">
                <summary>
                  KML-first / unassigned ({kmlContainers.loose.length + kmlContainers.looseGenerated.length})
                </summary>
                <div className="derivedList">
                  {kmlContainers.loose.map((job) => (
                    <KmlImportRow
                      key={job.id}
                      job={job}
                      childLayers={childLayersFor(job, kmlContainers.looseGenerated, kmlContainers.loose)}
                      activeDemJob={activeDemJob}
                      activeCompareJob={activeCompareJob}
                      onSnap={(sourceJob, target) => runSnap(sourceJob, target)}
                      onDelete={deleteJob}
                    />
                  ))}
                  {unparentedGeneratedLayers(kmlContainers.looseGenerated, kmlContainers.loose).map((job) => (
                    <KmlImportRow
                      key={job.id}
                      job={job}
                      activeDemJob={activeDemJob}
                      activeCompareJob={activeCompareJob}
                      onDelete={deleteJob}
                    />
                  ))}
                </div>
              </details>
            )}
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
                  <div className="jobFooter">
                    <StatusBadge status={job.status} />
                    {job.status === "failed" && (
                      <button className="danger miniButton" onClick={() => deleteJob(job)}>
                        <Trash2 size={12} />
                        Delete
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </details>
        </aside>

        <section className="mapFrame">
          <div className="mapToolDock">
            <div className="mapToolGroup">
              <button
                className={`mapToolIcon ${openMapTool === "aoi" ? "toggled" : ""}`}
                title="AOI tools"
                aria-label="AOI tools"
                onClick={() => setOpenMapTool(openMapTool === "aoi" ? undefined : "aoi")}
              >
                <Layers size={14} />
              </button>
              {openMapTool === "aoi" && (
                <div className="mapToolPopover">
                  <div className="mapToolHead">
                    <Layers size={14} />
                    <span>AOI</span>
                  </div>
                  <div className="mapToolMeta">{selectedAoiText}</div>
                  <button className="primary" disabled={!bounds || buildMutation.isPending} onClick={runBuildDem}>
                    <Play size={14} />
                    DEM Mosaic
                  </button>
                </div>
              )}
            </div>

            <div className="mapToolGroup">
              <button
                className={`mapToolIcon ${openMapTool === "kml" ? "toggled" : ""}`}
                title="KML import"
                aria-label="KML import"
                onClick={() => setOpenMapTool(openMapTool === "kml" ? undefined : "kml")}
              >
                <Upload size={14} />
              </button>
              <input
                ref={fileInputRef}
                className="hiddenFileInput"
                type="file"
                accept=".kml,application/vnd.google-earth.kml+xml"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) uploadKmlMutation.mutate(file);
                  event.currentTarget.value = "";
                }}
              />
              {openMapTool === "kml" && (
                <div className="mapToolPopover">
                  <div className="mapToolHead">
                    <Upload size={14} />
                    <span>KML</span>
                  </div>
                  {activeDemJob && <div className="mapToolMeta">Container {activeDemJob.id}</div>}
                  <div className="mapToolButtons">
                    <button disabled={uploadKmlMutation.isPending} onClick={() => fileInputRef.current?.click()}>
                      Upload
                    </button>
                    <button
                      disabled={ropewikiKmlMutation.isPending}
                      onClick={openRopewikiDialog}
                    >
                      Ropewiki
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="mapToolGroup">
              <button
                className={`mapToolIcon ${openMapTool === "ravine" ? "toggled" : ""}`}
                title="Ravine tools"
                aria-label="Ravine tools"
                onClick={() => setOpenMapTool(openMapTool === "ravine" ? undefined : "ravine")}
              >
                <Activity size={14} />
              </button>
              {openMapTool === "ravine" && (
                <div className="mapToolPopover">
                  <div className="mapToolHead">
                    <Activity size={14} />
                    <span>Ravine</span>
                  </div>
                  <button disabled={!activeCompareJob || spineMutation.isPending} onClick={() => spineMutation.mutate()}>
                    Ravine Spine
                  </button>
                </div>
              )}
            </div>
          </div>
          <dialog
            ref={ropewikiDialogRef}
            className="appDialog"
          >
            <form
              method="dialog"
              onSubmit={(event) => {
                event.preventDefault();
                runRopewikiImport();
              }}
            >
              <div className="dialogHead">
                <Upload size={16} />
                <strong>Import Ropewiki KML</strong>
              </div>
              {activeDemJob && <div className="mapToolMeta">Container {activeDemJob.id}</div>}
              <label>
                Ropewiki URL or title
                <input
                  autoFocus
                  value={ropewikiSource}
                  onChange={(event) => setRopewikiSource(event.target.value)}
                />
              </label>
              <div className="dialogActions">
                <button type="button" onClick={() => ropewikiDialogRef.current?.close()}>
                  Cancel
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={!ropewikiSource.trim() || ropewikiKmlMutation.isPending}
                >
                  Import
                </button>
              </div>
            </form>
          </dialog>
          <MapView kmlImports={kmlImports} profileJob={selectedProfileJob} />
        </section>
      </section>
    </main>
  );
}
