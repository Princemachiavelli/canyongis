import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ExternalLink,
  Eye,
  GitBranch,
  Layers,
  LineChart,
  LocateFixed,
  Map as MapIcon,
  Play,
  RefreshCw,
  SlidersHorizontal,
  Square,
  Tags,
  Trash2,
  Upload,
} from "lucide-react";
import {
  buildDem,
  cancelJob,
  deleteGeneratedKmlLayer,
  deleteJobRecord,
  importRopewikiKml,
  listJobSpecs,
  listJobs,
  runTool,
  trimJobRuns,
  uploadKmlFile,
  type Bounds,
  type Job,
  type JobSpec,
  type JobSpecVisualization,
  type KmlSnapTarget,
  type TrackCategory,
  type Project,
  logout,
  listProjects,
} from "./api";
import { MapView } from "./MapView";
import { AssetLibrary } from "./AssetLibrary";
import { Inspector, type InspectorTool } from "./Inspector";
import { PipelineView } from "./PipelineView";
import { useAppStore } from "./state";
import { ToolDialog, type ToolDialogResult } from "./ToolDialog";
import { descriptorsFromJob, type LayerDescriptor } from "./layers";

function jobBoundsWgs84(job: Job): Bounds | undefined {
  const arr = job.result?.bounds_wgs84 as number[] | undefined;
  if (!arr || arr.length !== 4) return undefined;
  return { west: arr[0], south: arr[1], east: arr[2], north: arr[3] };
}

type Area = {
  demJob: Job;
  name: string;
  bounds: Bounds;
  jobsBySpec: Map<string, Job[]>;
  spineJobsByTpi: Map<string, Job[]>;
};

function recordedInputs(job: Job): Record<string, unknown> | undefined {
  const value = (job.params as { _inputs?: unknown })._inputs;
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function tpiJobIdFromSpine(job: Job): string | undefined {
  const params = job.params as { tpi_job_id?: string; compare_job_id?: string };
  if (typeof params.tpi_job_id === "string" && params.tpi_job_id)
    return params.tpi_job_id;
  if (typeof params.compare_job_id === "string" && params.compare_job_id)
    return params.compare_job_id;
  const inputs = recordedInputs(job);
  for (const key of ["score", "compare", "tpi"] as const) {
    const value = inputs?.[key];
    if (typeof value === "string" && value) return value;
  }
  const result = job.result as { tpi_job_id?: string } | undefined;
  return typeof result?.tpi_job_id === "string" ? result.tpi_job_id : undefined;
}

function groupAreas(jobs: Job[], specs: JobSpec[]): Area[] {
  const dems = jobs.filter(
    (j) => j.kind === "build-dem" && j.status === "succeeded",
  );
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
    if (
      job.kind !== "ravine-spine" ||
      job.status !== "succeeded" ||
      !job.result
    )
      continue;
    const fallbackBounds = job.result.bounds_wgs84;
    const parentKmlJobId =
      job.params.parent_kml_job_id ?? job.result.parent_kml_job_id;
    const tpiJobId = tpiJobIdFromSpine(job);
    const compareJob = tpiJobId
      ? jobs.find((candidate) => candidate.id === tpiJobId)
      : undefined;
    const containerDemJobId = compareJob?.params.dem_job_id;
    for (const [key, label] of [
      ["spines_layer", "Ravine spines"],
      ["rappel_candidates_layer", "Rappel candidates"],
    ] as const) {
      const layer = job.result[key];
      if (!layer || typeof layer !== "object") continue;
      const result = layer as Record<string, unknown>;
      const sourceLabel = tpiJobId ?? job.id;
      layers.push({
        id: `${job.id}:${key}`,
        kind: "kml-layer",
        status: "succeeded",
        created_at: job.created_at,
        updated_at: job.updated_at,
        params: {
          ...job.params,
          source_job_id: job.id,
          layer: key,
          parent_kml_job_id: parentKmlJobId,
          container_dem_job_id: containerDemJobId,
          score_job_id: tpiJobId,
        },
        result: {
          ...result,
          name: `${label} (from ${sourceLabel})`,
          bounds_wgs84: result.bounds_wgs84 ?? fallbackBounds,
          parent_kml_job_id: parentKmlJobId,
          container_dem_job_id: containerDemJobId,
          source: {
            kind: "ravine-spine",
            job_id: job.id,
            layer: key,
            parent_kml_job_id: parentKmlJobId,
            score_job_id: tpiJobId,
          },
        },
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

function groupKmlContainers(
  areas: Area[],
  kmlJobs: Job[],
  generatedKmlJobs: Job[],
): KmlContainerGroups {
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
    const containingArea = bounds
      ? areas.find((area) => boundsContains(area.bounds, bounds))
      : undefined;
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
    const containingArea = bounds
      ? areas.find((area) => boundsContains(area.bounds, bounds))
      : undefined;
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
      generated: generatedByArea.get(area.demJob.id) ?? [],
    })),
    loose,
    looseGenerated,
  };
}

function childLayersFor(
  parent: Job,
  generated: Job[],
  siblingKmls: Job[],
): Job[] {
  return generated.filter((layer) => {
    const explicitParent = parentKmlJobId(layer);
    if (explicitParent) return explicitParent === parent.id;
    return siblingKmls.length === 1 && siblingKmls[0].id === parent.id;
  });
}

function unparentedGeneratedLayers(
  generated: Job[],
  siblingKmls: Job[],
): Job[] {
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
      return (
        String(data.display_name ?? "")
          .split(",")[0]
          .trim() || undefined
      );
    },
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
  });
}

function StatusBadge({ status }: { status: Job["status"] }) {
  return <span className={`statusBadge ${status}`}>{status}</span>;
}

const HIDDEN_PARAM_KEYS = new Set([
  "_inputs",
  "job_key",
  "source_job_id",
  "layer",
]);

function formatParamValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return `[${value.map((v) => formatParamValue(v)).join(", ")}]`;
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function JobParamsDetails({ job }: { job: Job }) {
  const inputs = useMemo(() => {
    const raw = (job.params as { _inputs?: unknown })._inputs;
    if (!raw || typeof raw !== "object" || Array.isArray(raw))
      return {} as Record<string, unknown>;
    return raw as Record<string, unknown>;
  }, [job.params]);
  const params = useMemo(() => {
    const entries = Object.entries(job.params as Record<string, unknown>)
      .filter(([k, v]) => !HIDDEN_PARAM_KEYS.has(k) && v !== null && v !== undefined)
      .sort(([a], [b]) => a.localeCompare(b));
    return entries;
  }, [job.params]);
  const inputEntries = Object.entries(inputs).filter(
    ([, v]) => v !== null && v !== undefined && v !== "",
  );
  if (params.length === 0 && inputEntries.length === 0) return null;
  return (
    <details className="jobParams">
      <summary>Parameters</summary>
      {inputEntries.length > 0 && (
        <dl className="jobParamsList">
          <dt className="jobParamsHeader">Inputs</dt>
          <dd />
          {inputEntries.map(([k, v]) => (
            <Fragment key={`in-${k}`}>
              <dt>{k}</dt>
              <dd>
                <code>{formatParamValue(v)}</code>
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
      {params.length > 0 && (
        <dl className="jobParamsList">
          {inputEntries.length > 0 && (
            <>
              <dt className="jobParamsHeader">Params</dt>
              <dd />
            </>
          )}
          {params.map(([k, v]) => (
            <Fragment key={`p-${k}`}>
              <dt>{k}</dt>
              <dd>{formatParamValue(v)}</dd>
            </Fragment>
          ))}
        </dl>
      )}
    </details>
  );
}

function ProgressTail({
  job,
  onDelete,
  onCancel,
}: {
  job: Job;
  onDelete?: (job: Job) => void;
  onCancel?: (job: Job) => void;
}) {
  const log = job.progress ?? [];
  const hasLog = log.length > 0 || Boolean(job.error);
  const canCancel = job.status === "running" || job.status === "queued";
  const canDelete = job.status === "failed" || job.status === "canceled";
  if (!hasLog && !canCancel && !canDelete) return null;
  if (!hasLog && canCancel) {
    return onCancel ? (
      <button className="danger miniButton" onClick={() => onCancel(job)}>
        <Square size={12} />
        Cancel job
      </button>
    ) : null;
  }
  const summary =
    job.status === "failed"
      ? "Error log"
      : job.status === "canceled"
        ? "Canceled job log"
        : "Job log";
  return (
    <details className="jobLogDetails" open={canCancel || job.status === "failed"}>
      <summary>
        {summary}
        {log.length > 0 && <span className="jobLogCount">{log.length}</span>}
      </summary>
      {canCancel && onCancel && (
        <button className="danger miniButton" onClick={() => onCancel(job)}>
          <Square size={12} />
          Cancel job
        </button>
      )}
      {canDelete && onDelete && (
        <button className="danger miniButton" onClick={() => onDelete(job)}>
          <Trash2 size={12} />
          Delete {job.status} job
        </button>
      )}
      {log.length > 0 && (
        <pre className={`progressTail ${job.status === "failed" ? "failed" : ""}`}>
          {log.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </pre>
      )}
      {job.error && <pre className="jobError">{job.error}</pre>}
    </details>
  );
}

function DerivedRow({
  job,
  onDelete,
  onCancel,
}: {
  job: Job;
  spec?: JobSpec;
  demJob?: Job;
  onDelete?: (job: Job) => void;
  onCancel?: (job: Job) => void;
}) {
  const setBaseLayer = useAppStore((s) => s.setBaseLayer);
  const setOverlayLayer = useAppStore((s) => s.setOverlayLayer);
  const baseLayerId = useAppStore((s) => s.baseLayerId);
  const overlayLayerId = useAppStore((s) => s.overlayLayerId);
  const descriptors = useMemo(() => descriptorsFromJob(job), [job]);
  const renderable = descriptors.filter(
    (d) => d.kind === "base-raster" || d.kind === "raster-overlay",
  );
  const isActiveBase = renderable.some(
    (d) => d.kind === "base-raster" && d.id === baseLayerId,
  );
  return (
    <div className={`derivedRow ${job.status} ${isActiveBase ? "active" : ""}`}>
      <div className="derivedRowHead">
        <code>{job.id}</code>
        <StatusBadge status={job.status} />
      </div>
      <div className="derivedRowActions">
        {job.status === "succeeded" &&
          renderable.map((descriptor) => {
            const isBase = descriptor.kind === "base-raster";
            const active = isBase
              ? baseLayerId === descriptor.id
              : overlayLayerId === descriptor.id;
            const label = isBase
              ? active
                ? "Hillshade base"
                : "Use as base"
              : descriptor.label;
            return (
              <button
                key={descriptor.id}
                className={active ? "toggled" : ""}
                onClick={() => {
                  if (isBase) setBaseLayer(active ? undefined : descriptor.id);
                  else setOverlayLayer(active ? undefined : descriptor.id);
                }}
              >
                <Eye size={12} />
                {label}
              </button>
            );
          })}
      </div>
      <JobParamsDetails job={job} />
      <ProgressTail job={job} onDelete={onDelete} onCancel={onCancel} />
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
  onCancelJob,
  onTrimRuns,
  isPending,
}: {
  area: Area;
  spec: JobSpec;
  runs: Job[];
  spineJobsByTpi: Map<string, Job[]>;
  onRunJob: (demJob: Job, spec: JobSpec) => void;
  onDeleteJob?: (job: Job) => void;
  onCancelJob?: (job: Job) => void;
  onTrimRuns?: (demJobId: string, jobKey: string) => void;
  isPending: boolean;
}) {
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
          title={
            unavailable
              ? (spec.unavailable_reason ?? "Unavailable")
              : spec.description
          }
          onClick={(e) => {
            e.preventDefault();
            triggerRun();
          }}
        >
          <Play size={12} />
          {unavailable ? "Unavailable" : runButtonLabel}
        </button>
        {hasRuns && onTrimRuns && (
          <button
            className="danger miniButton"
            title="Delete all but the most recent run"
            onClick={(e) => {
              e.preventDefault();
              onTrimRuns(area.demJob.id, spec.key);
            }}
          >
            <Trash2 size={12} />
            Delete old runs
          </button>
        )}
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
          const spineJobs = spineJobsByTpi.get(job.id) ?? [];
          return (
            <div key={job.id} className="jobRunGroup">
              <DerivedRow
                job={job}
                spec={spec}
                demJob={area.demJob}
                onDelete={onDeleteJob}
                onCancel={onCancelJob}
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
  onDeleteJob,
  onCancelJob,
  onTrimRuns,
}: {
  area: Area;
  specs: JobSpec[];
  onRunJob: (demJob: Job, spec: JobSpec) => void;
  pendingKey?: string;
  onDeleteJob?: (job: Job) => void;
  onCancelJob?: (job: Job) => void;
  onTrimRuns?: (demJobId: string, jobKey: string) => void;
}) {
  const setActiveDemJob = useAppStore((s) => s.setActiveDemJob);
  const toggleFootprint = useAppStore((s) => s.toggleFootprint);
  const requestPan = useAppStore((s) => s.requestPan);
  const footprintVisible = useAppStore((s) =>
    Boolean(s.footprints[area.demJob.id]),
  );
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
            toggleFootprint({
              id: area.demJob.id,
              name: area.name,
              bounds: area.bounds,
            })
          }
        >
          <Square size={12} />
          {footprintVisible ? "Hide footprint" : "Show footprint"}
        </button>
        <button
          onClick={() => setActiveDemJob(area.demJob)}
          disabled={isActive}
        >
          {isActive ? "Active container" : "Set active container"}
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
              onCancelJob={onCancelJob}
              onTrimRuns={onTrimRuns}
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
  childLayers = [],
  onSnap,
  onDelete,
  onCancel,
}: {
  job: Job;
  activeDemJob?: Job;
  childLayers?: Job[];
  onSnap?: (job: Job, target: KmlSnapTarget) => void;
  onDelete?: (job: Job) => void;
  onCancel?: (job: Job) => void;
}) {
  const bounds = jobBoundsWgs84(job);
  const setBounds = useAppStore((s) => s.setBounds);
  const requestPan = useAppStore((s) => s.requestPan);
  const toggleLayer = useAppStore((s) => s.toggleLayer);
  const toggleLayerLabels = useAppStore((s) => s.toggleLayerLabels);
  const layerId = job.kind === "kml-layer" ? job.id : `${job.id}:route`;
  const visible = useAppStore((s) => Boolean(s.visibleLayerIds[layerId]));
  const labelsVisible = useAppStore((s) => Boolean(s.labelLayerIds[layerId]));
  const name =
    (job.result?.name as string | undefined) ??
    (job.params.name as string | undefined) ??
    job.id;
  const kmlUrl = job.result?.kml_url as string | undefined;
  const canCancel =
    Boolean(onCancel) && (job.status === "queued" || job.status === "running");
  const canDelete =
    Boolean(onDelete) && job.status !== "queued" && job.status !== "running";

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
            <button
              className={visible ? "toggled" : ""}
              onClick={() => toggleLayer(layerId)}
            >
              <Eye size={12} />
              {visible ? "Hide" : "Show"}
            </button>
            <button
              className={labelsVisible ? "toggled" : ""}
              disabled={!visible}
              onClick={() => toggleLayerLabels(layerId)}
            >
              <Tags size={12} />
              Labels
            </button>
            <button onClick={() => setBounds(bounds)}>
              Use bounds for AOI
            </button>
            <button onClick={() => requestPan(bounds)}>
              <LocateFixed size={12} />
              Pan to
            </button>
            {onSnap && job.kind === "kml-import" && (
              <>
                <button onClick={() => onSnap(job, "dem")}>Snap DEM</button>
                <button onClick={() => onSnap(job, "tpi")}>Snap TPI</button>
                <button onClick={() => onSnap(job, "geomorphon")}>
                  Snap Geom
                </button>
                <button onClick={() => onSnap(job, "curvature")}>
                  Snap Curv
                </button>
              </>
            )}
          </>
        )}
        {kmlUrl && (
          <a
            className="linkButton"
            href={kmlUrl}
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLink size={12} />
            KML
          </a>
        )}
        {canCancel && (
          <button className="danger miniButton" onClick={() => onCancel?.(job)}>
            <Square size={12} />
            Cancel
          </button>
        )}
        {canDelete && (
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
              onDelete={onDelete}
              onCancel={onCancel}
            />
          ))}
        </div>
      )}
      <JobParamsDetails job={job} />
      <ProgressTail job={job} onDelete={onDelete} onCancel={onCancel} />
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

function ProfileChart({
  samples,
  rappels,
}: {
  samples: ProfileSample[];
  rappels: RappelCandidate[];
}) {
  const points = samples.filter(
    (sample) =>
      typeof sample.distance_m === "number" &&
      typeof sample.elevation_m === "number" &&
      Number.isFinite(sample.distance_m) &&
      Number.isFinite(sample.elevation_m),
  );
  if (points.length < 2)
    return <div className="empty">No valid DEM samples on this track.</div>;
  const width = 320;
  const height = 120;
  const pad = 12;
  const maxDistance = Math.max(...points.map((p) => p.distance_m));
  const minElevation = Math.min(...points.map((p) => p.elevation_m as number));
  const maxElevation = Math.max(...points.map((p) => p.elevation_m as number));
  const elevationSpan = Math.max(1, maxElevation - minElevation);
  const x = (distance: number) =>
    pad + (distance / Math.max(1, maxDistance)) * (width - pad * 2);
  const y = (elevation: number) =>
    height -
    pad -
    ((elevation - minElevation) / elevationSpan) * (height - pad * 2);
  const d = points
    .map(
      (point, idx) =>
        `${idx === 0 ? "M" : "L"} ${x(point.distance_m).toFixed(1)} ${y(point.elevation_m as number).toFixed(1)}`,
    )
    .join(" ");
  return (
    <svg
      className="profileChart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Elevation profile"
    >
      <path
        className="profileFill"
        d={`${d} L ${x(maxDistance)} ${height - pad} L ${pad} ${height - pad} Z`}
      />
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
  onRun,
}: {
  selected?: {
    kmlJobId: string;
    category: TrackCategory;
    trackIndex: number;
    name: string;
  };
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
      {!selected && (
        <div className="empty">Click a visible KML line to select a track.</div>
      )}
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
          <button onClick={() => clearSelected(undefined)}>
            Clear selected track
          </button>
        </>
      )}
      {profileJob?.status === "running" && <ProgressTail job={profileJob} />}
      {profileJob?.status === "succeeded" && (
        <div className="profileResult">
          <div className="profileStats">
            <span>
              Length{" "}
              {formatMeters(profileJob.result?.length_m as number | undefined)}
            </span>
            <span>
              Drop{" "}
              {formatMeters(
                profileJob.result?.total_drop_m as number | undefined,
              )}
            </span>
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

function ProjectPicker({
  queryClient: qc,
}: {
  queryClient: ReturnType<typeof useQueryClient>;
}) {
  const { data: projects } = useQuery<Project[]>({
    queryKey: ["projects"],
    queryFn: listProjects,
    staleTime: 60_000,
  });
  const activeProjectId = useAppStore((state) => state.activeProjectId);
  const setActiveProjectId = useAppStore((state) => state.setActiveProjectId);

  const handleChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const id = e.target.value || undefined;
    setActiveProjectId(id);
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["project-assets"] });
  };

  useEffect(() => {
    if (!projects || projects.length === 0) return;
    if (activeProjectId && projects.some((p) => p.id === activeProjectId)) return;
    setActiveProjectId(projects[0].id);
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["project-assets"] });
  }, [activeProjectId, projects, qc, setActiveProjectId]);

  if (!projects || projects.length === 0) return null;

  return (
    <div className="projectPicker">
      <select
        value={activeProjectId || ""}
        onChange={handleChange}
        disabled={projects.length <= 1}
      >
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </div>
  );
}

function UserMenu({
  queryClient: qc,
}: {
  queryClient: ReturnType<typeof useQueryClient>;
}) {
  const user = useAppStore((state) => state.authenticatedUser);
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await logout();
      useAppStore.getState().setAuthenticatedUser(undefined);
      qc.invalidateQueries({ queryKey: ["auth/me"] });
    } catch {
      setLoggingOut(false);
    }
  };

  if (!user) return null;

  return (
    <div className="userMenu">
      <span className="username">{user.username}</span>
      <button disabled={loggingOut} onClick={handleLogout}>
        {loggingOut ? "..." : "Logout"}
      </button>
    </div>
  );
}

function OpacitySlider({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  return (
    <label className="opacityRow">
      <span className="opacityLabel">
        {label}
        <span className="opacityValue">{Math.round(value * 100)}%</span>
      </span>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function OpacitySliders() {
  const osmOpacity = useAppStore((s) => s.osmOpacity);
  const esriOpacity = useAppStore((s) => s.esriOpacity);
  const hillshadeOpacity = useAppStore((s) => s.hillshadeOpacity);
  const overlayOpacity = useAppStore((s) => s.overlayOpacity);
  const setOsmOpacity = useAppStore((s) => s.setOsmOpacity);
  const setEsriOpacity = useAppStore((s) => s.setEsriOpacity);
  const setHillshadeOpacity = useAppStore((s) => s.setHillshadeOpacity);
  const setOverlayOpacity = useAppStore((s) => s.setOverlayOpacity);
  const hasHillshade = Boolean(useAppStore((s) => s.baseLayerId));
  const hasOverlay = Boolean(useAppStore((s) => s.overlayLayerId));
  return (
    <>
      <OpacitySlider label="OpenStreetMap" value={osmOpacity} onChange={setOsmOpacity} />
      <OpacitySlider label="Esri imagery" value={esriOpacity} onChange={setEsriOpacity} />
      <OpacitySlider
        label="Hillshade"
        value={hillshadeOpacity}
        onChange={setHillshadeOpacity}
        disabled={!hasHillshade}
      />
      <OpacitySlider
        label="Feature overlay"
        value={overlayOpacity}
        onChange={setOverlayOpacity}
        disabled={!hasOverlay}
      />
    </>
  );
}

export function App() {
  const queryClient = useQueryClient();
  const useAssetUi = useMemo(
    () => new URLSearchParams(window.location.search).get("ui") !== "legacy",
    [],
  );
  const bounds = useAppStore((state) => state.bounds);
  const activeProjectId = useAppStore((state) => state.activeProjectId);
  const activeDemJob = useAppStore((state) => state.activeDemJob);
  const selectedKmlTrack = useAppStore((state) => state.selectedKmlTrack);
  const baseLayerId = useAppStore((state) => state.baseLayerId);
  const overlayLayerId = useAppStore((state) => state.overlayLayerId);
  const visibleLayerIds = useAppStore((state) => state.visibleLayerIds);
  const labelLayerIds = useAppStore((state) => state.labelLayerIds);
  const footprints = useAppStore((state) => state.footprints);
  const setBounds = useAppStore((state) => state.setBounds);
  const setActiveDemJob = useAppStore((state) => state.setActiveDemJob);
  const setBaseLayer = useAppStore((state) => state.setBaseLayer);
  const setOverlayLayer = useAppStore((state) => state.setOverlayLayer);
  const clearFootprints = useAppStore((state) => state.clearFootprints);
  const footprintCount = useAppStore(
    (state) => Object.keys(state.footprints).length,
  );
  const fileInputRef = useRef<HTMLInputElement>(null);
  const ropewikiDialogRef = useRef<HTMLDialogElement>(null);
  const demDialogRef = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("aoi");
  const [bufferM, setBufferM] = useState(200);
  const [openMapTool, setOpenMapTool] = useState<
    "aoi" | "kml" | "ravine" | "opacity" | undefined
  >();
  const [ropewikiSource, setRopewikiSource] = useState("");

  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: listJobs,
    refetchInterval: 2000,
  });

  const specsQuery = useQuery({
    queryKey: ["job-specs"],
    queryFn: listJobSpecs,
    staleTime: Infinity,
    gcTime: Infinity,
  });

  const jobs = jobsQuery.data ?? [];
  const specs = useMemo(() => specsQuery.data ?? [], [specsQuery.data]);
  const containerSpecs = useMemo(
    () => specs.filter((spec) => spec.runner_kind === "dem"),
    [specs],
  );
  const areas = useMemo(() => groupAreas(jobs, specs), [jobs, specs]);
  const baseKmlJobs = useMemo(
    () => jobs.filter((j) => j.kind === "kml-import" || j.kind === "kml-snap"),
    [jobs],
  );
  const generatedKmlJobs = useMemo(() => ravineSpineLayerJobs(jobs), [jobs]);
  const kmlImports = useMemo(
    () => [...baseKmlJobs, ...generatedKmlJobs],
    [baseKmlJobs, generatedKmlJobs],
  );
  const kmlContainers = useMemo(
    () => groupKmlContainers(areas, baseKmlJobs, generatedKmlJobs),
    [areas, baseKmlJobs, generatedKmlJobs],
  );
  const profileJobs = useMemo(
    () => jobs.filter((j) => j.kind === "track-profile"),
    [jobs],
  );
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
    [activeDemJob, profileJobs, selectedKmlTrack],
  );

  const visibleLayers = useMemo<LayerDescriptor[]>(() => {
    const out: LayerDescriptor[] = [];
    for (const fp of Object.values(footprints)) {
      out.push({
        id: `footprint:${fp.id}`,
        label: fp.name,
        kind: "footprint",
        opacity: 0.04,
        bounds_wgs84: [fp.bounds.west, fp.bounds.south, fp.bounds.east, fp.bounds.north],
      });
    }
    const profileLayerJobId = selectedProfileJob?.status === "succeeded" ? selectedProfileJob.id : undefined;
    for (const job of jobs) {
      for (const desc of descriptorsFromJob(job)) {
        let visible = false;
        if (desc.kind === "base-raster") visible = baseLayerId === desc.id;
        else if (desc.kind === "raster-overlay") visible = overlayLayerId === desc.id;
        else if (desc.kind === "vector-kml") visible = Boolean(visibleLayerIds[desc.id]);
        else if (desc.kind === "point-events" || desc.kind === "vector-geojson") {
          visible =
            Boolean(visibleLayerIds[desc.id]) ||
            (profileLayerJobId !== undefined && job.id === profileLayerJobId);
        }
        if (!visible) continue;
        out.push({
          ...desc,
          showLabels: desc.kind === "vector-kml" ? Boolean(labelLayerIds[desc.id]) : undefined,
        });
      }
    }
    return out;
  }, [
    jobs,
    footprints,
    baseLayerId,
    overlayLayerId,
    visibleLayerIds,
    labelLayerIds,
    selectedProfileJob,
  ]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
    queryClient.invalidateQueries({ queryKey: ["project-assets"] });
  };
  const buildMutation = useMutation({
    mutationFn: (payload: { name: string; bounds: Bounds; bufferM: number }) =>
      buildDem(payload.name, payload.bounds, payload.bufferM),
    onSuccess: invalidate,
  });
  const [pendingRunKey, setPendingRunKey] = useState<string | undefined>();
  type ToolDialogContext = {
    spec: JobSpec;
    initialInputs?: Record<string, string | undefined>;
    initialParams?: Record<string, unknown>;
    extraParams?: Record<string, unknown>;
    pendingKey?: string;
  };
  const [toolDialog, setToolDialog] = useState<ToolDialogContext | undefined>();
  const [selectedAssetId, setSelectedAssetId] = useState<string | undefined>();
  const [inspectorTool, setInspectorTool] = useState<InspectorTool | undefined>();
  const [pipelineOpen, setPipelineOpen] = useState(false);
  const runToolMutation = useMutation({
    mutationFn: (payload: {
      spec: JobSpec;
      result: ToolDialogResult;
      extraParams?: Record<string, unknown>;
    }) =>
      runTool(payload.spec.key, {
        inputs: payload.result.inputs,
        params: { ...payload.result.params, ...(payload.extraParams ?? {}) },
      }),
    onMutate: ({ spec, result }) => {
      const demId = result.inputs.dem;
      const pendingKey = demId ? `${demId}:${spec.key}` : `tool:${spec.key}`;
      setPendingRunKey(pendingKey);
    },
    onSettled: () => {
      setPendingRunKey(undefined);
      invalidate();
    },
  });
  const onRunJob = (demJob: Job, spec: JobSpec) => {
    const payload = {
      spec,
      initialInputs: { dem: demJob.id },
      pendingKey: `${demJob.id}:${spec.key}`,
    };
    if (useAssetUi) setInspectorTool(payload);
    else setToolDialog(payload);
  };
  const onRunAssetTool = (
    spec: JobSpec,
    initialInputs: Record<string, string | undefined>,
  ) => {
    setInspectorTool({
      spec,
      initialInputs,
    });
  };
  const runInspectorTool = (tool: InspectorTool, result: ToolDialogResult) => {
    runToolMutation.mutate(
      {
        spec: tool.spec,
        result,
        extraParams: tool.extraParams,
      },
      { onSuccess: () => setInspectorTool(undefined) },
    );
  };
  const uploadKmlMutation = useMutation({
    mutationFn: (file: File) => uploadKmlFile(file, activeDemJob?.id),
    onSuccess: () => {
      invalidate();
    },
  });
  const ropewikiKmlMutation = useMutation({
    mutationFn: (source: string) => {
      if (!source) throw new Error("Enter a Ropewiki URL or title");
      return importRopewikiKml(source, activeDemJob?.id);
    },
    onSuccess: invalidate,
  });
  const trackProfileMutation = useMutation({
    mutationFn: () => {
      if (!selectedKmlTrack) throw new Error("Click a visible KML track first");
      if (!activeDemJob) throw new Error("Select a completed DEM job first");
      return runTool("track-profile", {
        inputs: {
          kml: selectedKmlTrack.kmlJobId,
          dem: activeDemJob.id,
        },
        params: {
          category: selectedKmlTrack.category,
          track_index: selectedKmlTrack.trackIndex,
        },
      });
    },
    onSuccess: invalidate,
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
    onSuccess: invalidate,
  });
  const deleteJob = (job: Job) => deleteJobMutation.mutate(job);
  const cancelJobMutation = useMutation({
    mutationFn: (job: Job) => cancelJob(job.id),
    onSuccess: invalidate,
  });
  const cancelRunningJob = (job: Job) => cancelJobMutation.mutate(job);
  const trimRunsMutation = useMutation({
    mutationFn: ({
      demJobId,
      jobKey,
    }: {
      demJobId: string;
      jobKey: string;
    }) => trimJobRuns(demJobId, jobKey),
    onSuccess: invalidate,
  });
  const trimOldRuns = (demJobId: string, jobKey: string) =>
    trimRunsMutation.mutate({ demJobId, jobKey });

  const snapSpec = useMemo(
    () => specs.find((s) => s.key === "kml-snap"),
    [specs],
  );
  const spineSpec = useMemo(
    () => specs.find((s) => s.key === "ravine-spine"),
    [specs],
  );

  const openSnapDialog = (job: Job, target: KmlSnapTarget, demJob?: Job) => {
    if (!snapSpec) return;
    const initialInputs: Record<string, string | undefined> = { kml: job.id };
    const targetDem = demJob ?? activeDemJob;
    if (targetDem) initialInputs.dem = targetDem.id;
    const payload = {
      spec: snapSpec,
      initialInputs,
      initialParams: { target },
    };
    if (useAssetUi) setInspectorTool(payload);
    else setToolDialog(payload);
  };

  const openSpineDialog = () => {
    if (!spineSpec) return;
    const initialInputs: Record<string, string | undefined> = {};
    if (activeDemJob) initialInputs.dem = activeDemJob.id;
    const extraParams: Record<string, unknown> = {};
    if (selectedKmlTrack?.kmlJobId) {
      extraParams.parent_kml_job_id = selectedKmlTrack.kmlJobId;
    }
    const payload = {
      spec: spineSpec,
      initialInputs,
      extraParams,
    };
    if (useAssetUi) setInspectorTool(payload);
    else setToolDialog(payload);
  };

  const runBuildDem = () => {
    if (!bounds) {
      return;
    }
    const finalName = name.trim() || "aoi";
    setName(finalName);
    buildMutation.mutate(
      { name: finalName, bounds, bufferM },
      { onSuccess: () => demDialogRef.current?.close() },
    );
  };

  const openDemDialog = () => {
    if (!bounds) return;
    const dialog = demDialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
  };

  const runRopewikiImport = () => {
    const source = ropewikiSource.trim();
    if (!source) return;
    ropewikiKmlMutation.mutate(source, {
      onSuccess: () => {
        setRopewikiSource("");
        ropewikiDialogRef.current?.close();
      },
    });
  };

  const openRopewikiDialog = () => {
    const dialog = ropewikiDialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
  };

  const selectedAoiText = bounds ? formatBounds(bounds) : "No AOI selected";

  const error = useMemo(() => {
    return (
      buildMutation.error ??
      runToolMutation.error ??
      uploadKmlMutation.error ??
      ropewikiKmlMutation.error ??
      trackProfileMutation.error ??
      deleteJobMutation.error ??
      cancelJobMutation.error ??
      jobsQuery.error
    );
  }, [
    buildMutation.error,
    runToolMutation.error,
    uploadKmlMutation.error,
    ropewikiKmlMutation.error,
    trackProfileMutation.error,
    deleteJobMutation.error,
    cancelJobMutation.error,
    jobsQuery.error,
  ]);

  return (
    <main className="shell">
      <section className="toolbar">
        <div className="brand">
          <MapIcon size={18} />
          <span>canyongis</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <ProjectPicker queryClient={queryClient} />
          {useAssetUi && (
            <button onClick={() => setPipelineOpen(true)}>
              <GitBranch size={16} />
              Pipeline
            </button>
          )}
          <UserMenu queryClient={queryClient} />
          <button onClick={() => jobsQuery.refetch()}>
            <RefreshCw size={16} />
          </button>
        </div>
      </section>

      <section className={`workspace ${useAssetUi ? "assetWorkspace" : ""}`}>
        {useAssetUi ? (
          <AssetLibrary
            projectId={activeProjectId}
            jobs={jobs}
            specs={specs}
            selectedAssetId={selectedAssetId}
            onSelectAsset={(assetId) => {
              setSelectedAssetId(assetId);
              setInspectorTool(undefined);
            }}
            onRunTool={onRunAssetTool}
          />
        ) : (
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
              <strong>
                {bounds
                  ? `${bounds.west.toFixed(4)}, ${bounds.south.toFixed(4)}`
                  : "none"}
              </strong>
            </div>
            {bounds && (
              <button onClick={() => setBounds(undefined)}>Clear AOI</button>
            )}
            <div>
              <span>DEM job</span>
              <strong>{activeDemJob?.id ?? "none"}</strong>
            </div>
            {activeDemJob && (
              <button onClick={() => setActiveDemJob(undefined)}>
                Clear active DEM/container
              </button>
            )}
            <div>
              <span>Hillshade base</span>
              <strong>{baseLayerId ?? "off"}</strong>
            </div>
            {baseLayerId && (
              <button onClick={() => setBaseLayer(undefined)}>
                Clear hillshade
              </button>
            )}
            <div>
              <span>Overlay</span>
              <strong>{overlayLayerId ?? "none"}</strong>
            </div>
            {overlayLayerId && (
              <button onClick={() => setOverlayLayer(undefined)}>
                Clear overlay
              </button>
            )}
            {footprintCount > 0 && (
              <button onClick={() => clearFootprints()}>
                Hide all footprints ({footprintCount})
              </button>
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
              const unparentedGenerated = unparentedGeneratedLayers(
                generated,
                kmls,
              );
              return (
                <details
                  key={area.demJob.id}
                  open={kmls.length > 0}
                  className="derivedSection"
                >
                  <summary>
                    DEM: {area.name} ({kmls.length} KML)
                  </summary>
                  <div className="derivedList">
                      <AreaCard
                        area={area}
                        specs={containerSpecs}
                      onRunJob={onRunJob}
                      pendingKey={pendingRunKey}
                      onDeleteJob={deleteJob}
                      onCancelJob={cancelRunningJob}
                      onTrimRuns={trimOldRuns}
                    />
                    {kmls.length === 0 && (
                      <div className="empty">
                        No KMLs in this DEM container.
                      </div>
                    )}
                    {kmls.map((job) => (
                      <KmlImportRow
                        key={job.id}
                        job={job}
                        childLayers={childLayersFor(job, generated, kmls)}
                        activeDemJob={area.demJob}
                        onSnap={(sourceJob, target) =>
                          openSnapDialog(sourceJob, target, area.demJob)
                        }
                        onDelete={deleteJob}
                        onCancel={cancelRunningJob}
                      />
                    ))}
                    {unparentedGenerated.length > 0 && (
                      <details open className="derivedSection">
                        <summary>
                          Generated KML ({unparentedGenerated.length})
                        </summary>
                        <div className="derivedList">
                          {unparentedGenerated.map((job) => (
                            <KmlImportRow
                              key={job.id}
                              job={job}
                              activeDemJob={area.demJob}
                                    onDelete={deleteJob}
                              onCancel={cancelRunningJob}
                            />
                          ))}
                        </div>
                      </details>
                    )}
                  </div>
                </details>
              );
            })}
            {(kmlContainers.loose.length > 0 ||
              kmlContainers.looseGenerated.length > 0) && (
              <details open className="derivedSection">
                <summary>
                  KML: unassigned (
                  {kmlContainers.loose.length +
                    kmlContainers.looseGenerated.length}
                  )
                </summary>
                <div className="derivedList">
                  {kmlContainers.loose.map((job) => (
                    <KmlImportRow
                      key={job.id}
                      job={job}
                      childLayers={childLayersFor(
                        job,
                        kmlContainers.looseGenerated,
                        kmlContainers.loose,
                      )}
                      activeDemJob={activeDemJob}
                      onSnap={(sourceJob, target) =>
                        openSnapDialog(sourceJob, target)
                      }
                      onDelete={deleteJob}
                      onCancel={cancelRunningJob}
                    />
                  ))}
                  {unparentedGeneratedLayers(
                    kmlContainers.looseGenerated,
                    kmlContainers.loose,
                  ).map((job) => (
                    <KmlImportRow
                      key={job.id}
                      job={job}
                      activeDemJob={activeDemJob}
                      onDelete={deleteJob}
                      onCancel={cancelRunningJob}
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
                    {(job.status === "queued" || job.status === "running") && (
                      <button
                        className="danger miniButton"
                        onClick={() => cancelRunningJob(job)}
                      >
                        <Square size={12} />
                        Cancel
                      </button>
                    )}
                    {job.status !== "queued" && job.status !== "running" && (
                      <button
                        className="danger miniButton"
                        onClick={() => deleteJob(job)}
                      >
                        <Trash2 size={12} />
                        Delete
                      </button>
                    )}
                  </div>
                  <ProgressTail
                    job={job}
                    onDelete={deleteJob}
                    onCancel={cancelRunningJob}
                  />
                </div>
              ))}
            </div>
          </details>
        </aside>
        )}

        <section className="mapFrame">
          <div className="mapToolDock">
            <div className="mapToolGroup">
              <button
                className={`mapToolIcon ${openMapTool === "aoi" ? "toggled" : ""}`}
                title="AOI tools"
                aria-label="AOI tools"
                onClick={() =>
                  setOpenMapTool(openMapTool === "aoi" ? undefined : "aoi")
                }
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
                  <button
                    className="primary"
                    disabled={!bounds || buildMutation.isPending}
                    onClick={openDemDialog}
                  >
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
                onClick={() =>
                  setOpenMapTool(openMapTool === "kml" ? undefined : "kml")
                }
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
                  <div className="mapToolMeta">
                    {activeDemJob
                      ? `Container ${activeDemJob.id}`
                      : "Import unassigned"}
                  </div>
                  {activeDemJob && (
                    <button onClick={() => setActiveDemJob(undefined)}>
                      Import outside container
                    </button>
                  )}
                  <div className="mapToolButtons">
                    <button
                      disabled={uploadKmlMutation.isPending}
                      onClick={() => fileInputRef.current?.click()}
                    >
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
                onClick={() =>
                  setOpenMapTool(
                    openMapTool === "ravine" ? undefined : "ravine",
                  )
                }
              >
                <Activity size={14} />
              </button>
              {openMapTool === "ravine" && (
                <div className="mapToolPopover">
                  <div className="mapToolHead">
                    <Activity size={14} />
                    <span>Ravine</span>
                  </div>
                  <button
                    disabled={!spineSpec || runToolMutation.isPending}
                    onClick={openSpineDialog}
                  >
                    Ravine Spine
                  </button>
                </div>
              )}
            </div>

            <div className="mapToolGroup">
              <button
                className={`mapToolIcon ${openMapTool === "opacity" ? "toggled" : ""}`}
                title="Layer opacity"
                aria-label="Layer opacity"
                onClick={() =>
                  setOpenMapTool(
                    openMapTool === "opacity" ? undefined : "opacity",
                  )
                }
              >
                <SlidersHorizontal size={14} />
              </button>
              {openMapTool === "opacity" && (
                <div className="mapToolPopover">
                  <div className="mapToolHead">
                    <SlidersHorizontal size={14} />
                    <span>Layer opacity</span>
                  </div>
                  <OpacitySliders />
                </div>
              )}
            </div>
          </div>
          <dialog ref={demDialogRef} className="appDialog">
            <form
              method="dialog"
              onSubmit={(event) => {
                event.preventDefault();
                runBuildDem();
              }}
            >
              <div className="dialogHead">
                <Layers size={16} />
                <strong>Build DEM mosaic</strong>
              </div>
              <div className="mapToolMeta">{selectedAoiText}</div>
              <label>
                Container name
                <input
                  autoFocus
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <label>
                Buffer meters
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={bufferM}
                  onChange={(event) => setBufferM(Number(event.target.value))}
                />
              </label>
              <div className="dialogActions">
                <button
                  type="button"
                  onClick={() => demDialogRef.current?.close()}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={
                    !bounds ||
                    buildMutation.isPending ||
                    !Number.isFinite(bufferM) ||
                    bufferM < 0
                  }
                >
                  Build
                </button>
              </div>
            </form>
          </dialog>
          <dialog ref={ropewikiDialogRef} className="appDialog">
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
              {activeDemJob && (
                <div className="mapToolMeta">Container {activeDemJob.id}</div>
              )}
              <label>
                Ropewiki URL or title
                <input
                  autoFocus
                  value={ropewikiSource}
                  onChange={(event) => setRopewikiSource(event.target.value)}
                />
              </label>
              <div className="dialogActions">
                <button
                  type="button"
                  onClick={() => ropewikiDialogRef.current?.close()}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={
                    !ropewikiSource.trim() || ropewikiKmlMutation.isPending
                  }
                >
                  Import
                </button>
              </div>
            </form>
          </dialog>
          <MapView layers={visibleLayers} />
        </section>
        {useAssetUi && (
          <Inspector
            projectId={activeProjectId}
            selectedAssetId={selectedAssetId}
            tool={inspectorTool}
            jobs={jobs}
            specs={specs}
            isRunningTool={runToolMutation.isPending}
            onSelectAsset={(assetId) => {
              setSelectedAssetId(assetId);
              setInspectorTool(undefined);
            }}
            onClearSelection={() => setSelectedAssetId(undefined)}
            onOpenTool={onRunAssetTool}
            onCloseTool={() => setInspectorTool(undefined)}
            onRunTool={runInspectorTool}
          />
        )}
      </section>
      {!useAssetUi && toolDialog && (
        <ToolDialog
          open={true}
          spec={toolDialog.spec}
          jobs={jobs}
          initialInputs={toolDialog.initialInputs}
          initialParams={toolDialog.initialParams}
          isPending={runToolMutation.isPending}
          onClose={() => setToolDialog(undefined)}
          onRun={(result) =>
            runToolMutation.mutate(
              {
                spec: toolDialog.spec,
                result,
                extraParams: toolDialog.extraParams,
              },
              { onSuccess: () => setToolDialog(undefined) },
            )
          }
        />
      )}
      {useAssetUi && pipelineOpen && (
        <PipelineView
          projectId={activeProjectId}
          onClose={() => setPipelineOpen(false)}
          onSelectAsset={(assetId) => {
            setSelectedAssetId(assetId);
            setInspectorTool(undefined);
            setPipelineOpen(false);
          }}
        />
      )}
    </main>
  );
}
