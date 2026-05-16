import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  GitBranch,
  Layers,
  Map as MapIcon,
  Play,
  RefreshCw,
  SlidersHorizontal,
  Upload,
} from "lucide-react";
import {
  buildDem,
  cancelJob,
  deleteGeneratedKmlLayer,
  deleteJobRecord,
  importRopewikiBulk,
  importRopewikiKml,
  listJobSpecs,
  listJobs,
  listProjectAssets,
  runTool,
  uploadKmlFile,
  type Asset,
  type Bounds,
  type Job,
  type JobSpec,
} from "./api";
import { MapView } from "./MapView";
import { AssetLibrary } from "./AssetLibrary";
import { Inspector, type InspectorTool } from "./Inspector";
import { PipelineView } from "./PipelineView";
import { JobActivityPanel } from "./JobActivityPanel";
import { ProjectPicker } from "./ProjectPicker";
import { UserMenu } from "./UserMenu";
import { OpacitySliders } from "./OpacitySliders";
import { useAppStore } from "./state";
import type { ToolDialogResult } from "./ToolDialog";
import {
  assetSynthesizedLayerId,
  descriptorsFromJob,
  isPreviewAsset,
  synthesizedLayerFromAsset,
  type LayerDescriptor,
} from "./layers";

function formatBounds(b: Bounds): string {
  return `${b.west.toFixed(4)}, ${b.south.toFixed(4)} → ${b.east.toFixed(4)}, ${b.north.toFixed(4)}`;
}

export function App() {
  const queryClient = useQueryClient();
  const bounds = useAppStore((state) => state.bounds);
  const activeProjectId = useAppStore((state) => state.activeProjectId);
  const activeDemJob = useAppStore((state) => state.activeDemJob);
  const selectedKmlTrack = useAppStore((state) => state.selectedKmlTrack);
  const baseLayerId = useAppStore((state) => state.baseLayerId);
  const overlayLayerId = useAppStore((state) => state.overlayLayerId);
  const visibleLayerIds = useAppStore((state) => state.visibleLayerIds);
  const labelLayerIds = useAppStore((state) => state.labelLayerIds);
  const footprints = useAppStore((state) => state.footprints);
  const setActiveDemJob = useAppStore((state) => state.setActiveDemJob);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const ropewikiDialogRef = useRef<HTMLDialogElement>(null);
  const demDialogRef = useRef<HTMLDialogElement>(null);

  const [name, setName] = useState("aoi");
  const [bufferM, setBufferM] = useState(200);
  const [openMapTool, setOpenMapTool] = useState<
    "aoi" | "kml" | "ravine" | "opacity" | undefined
  >();
  const [ropewikiSource, setRopewikiSource] = useState("");
  const [bulkMaxResults, setBulkMaxResults] = useState(50);
  const [selectedAssetId, setSelectedAssetId] = useState<string | undefined>();
  const [inspectorTool, setInspectorTool] = useState<InspectorTool | undefined>();
  const [pipelineOpen, setPipelineOpen] = useState(false);
  const [jobsOpen, setJobsOpen] = useState(false);

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
  const assetsQuery = useQuery({
    queryKey: ["project-assets", activeProjectId],
    queryFn: () => listProjectAssets(activeProjectId as string),
    enabled: Boolean(activeProjectId),
    refetchInterval: 2000,
  });

  const jobs = jobsQuery.data ?? [];
  const specs = useMemo(() => specsQuery.data ?? [], [specsQuery.data]);
  const projectAssets: Asset[] = useMemo(
    () => assetsQuery.data ?? [],
    [assetsQuery.data],
  );
  const runningJobCount = useMemo(
    () => jobs.filter((job) => job.status === "queued" || job.status === "running").length,
    [jobs],
  );
  const spineSpec = useMemo(
    () => specs.find((s) => s.key === "ravine-spine"),
    [specs],
  );

  // Auto-show the track-profile job's geojson when its parent KML track is selected.
  const selectedProfileJob = useMemo(() => {
    if (!selectedKmlTrack || !activeDemJob) return undefined;
    return jobs.find((job) => {
      if (job.kind !== "track-profile") return false;
      const p = job.params as {
        kml_job_id?: string;
        category?: string;
        track_index?: number;
        dem_job_id?: string;
      };
      return (
        p.kml_job_id === selectedKmlTrack.kmlJobId &&
        p.category === selectedKmlTrack.category &&
        p.track_index === selectedKmlTrack.trackIndex &&
        p.dem_job_id === activeDemJob.id
      );
    });
  }, [jobs, selectedKmlTrack, activeDemJob]);

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
    const profileLayerJobId =
      selectedProfileJob?.status === "succeeded" ? selectedProfileJob.id : undefined;
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
    for (const asset of projectAssets) {
      if (!isPreviewAsset(asset)) continue;
      const layerId = assetSynthesizedLayerId(asset.id);
      if (baseLayerId !== layerId && overlayLayerId !== layerId) continue;
      const label =
        typeof asset.metadata.label === "string" && asset.metadata.label
          ? asset.metadata.label
          : asset.path?.split("/").pop() || asset.id;
      const synthesized = synthesizedLayerFromAsset(asset, label);
      if (synthesized) out.push(synthesized);
    }
    return out;
  }, [
    jobs,
    projectAssets,
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
    queryClient.invalidateQueries({ queryKey: ["project-pipeline"] });
  };

  const buildMutation = useMutation({
    mutationFn: (payload: { name: string; bounds: Bounds; bufferM: number }) =>
      buildDem(payload.name, payload.bounds, payload.bufferM),
    onSuccess: invalidate,
  });
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
    onSettled: invalidate,
  });
  const uploadKmlMutation = useMutation({
    mutationFn: (file: File) => uploadKmlFile(file, activeDemJob?.id),
    onSuccess: invalidate,
  });
  const ropewikiKmlMutation = useMutation({
    mutationFn: (source: string) => {
      if (!source) throw new Error("Enter a Ropewiki URL or title");
      return importRopewikiKml(source, activeDemJob?.id);
    },
    onSuccess: invalidate,
  });
  const ropewikiBulkMutation = useMutation({
    mutationFn: () => {
      if (!bounds) throw new Error("Draw or select an AOI first");
      return importRopewikiBulk({
        bounds,
        containerDemJobId: activeDemJob?.id,
        maxResults: bulkMaxResults,
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
  const cancelJobMutation = useMutation({
    mutationFn: (job: Job) => cancelJob(job.id),
    onSuccess: invalidate,
  });

  const deleteJob = (job: Job) => deleteJobMutation.mutate(job);
  const cancelRunningJob = (job: Job) => cancelJobMutation.mutate(job);

  const onRunAssetTool = (
    spec: JobSpec,
    initialInputs: Record<string, string | undefined>,
  ) => setInspectorTool({ spec, initialInputs });

  const runInspectorTool = (tool: InspectorTool, result: ToolDialogResult) => {
    runToolMutation.mutate(
      { spec: tool.spec, result, extraParams: tool.extraParams },
      { onSuccess: () => setInspectorTool(undefined) },
    );
  };

  const openSpineDialog = () => {
    if (!spineSpec) return;
    const initialInputs: Record<string, string | undefined> = {};
    if (activeDemJob) initialInputs.dem = activeDemJob.id;
    const extraParams: Record<string, unknown> = {};
    if (selectedKmlTrack?.kmlJobId) {
      extraParams.parent_kml_job_id = selectedKmlTrack.kmlJobId;
    }
    setInspectorTool({ spec: spineSpec, initialInputs, extraParams });
  };

  const runBuildDem = () => {
    if (!bounds) return;
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
    if (dialog && !dialog.open) dialog.showModal();
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
    if (dialog && !dialog.open) dialog.showModal();
  };

  const selectedAoiText = bounds ? formatBounds(bounds) : "No AOI selected";

  return (
    <main className="shell">
      <section className="toolbar">
        <div className="brand">
          <MapIcon size={18} />
          <span>canyongis</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <ProjectPicker queryClient={queryClient} />
          <button
            className={pipelineOpen ? "toggled" : ""}
            onClick={() => setPipelineOpen((open) => !open)}
          >
            <GitBranch size={16} />
            Pipeline
          </button>
          <button
            className={jobsOpen ? "toggled" : ""}
            onClick={() => setJobsOpen((open) => !open)}
          >
            <Activity size={16} />
            Jobs
            {runningJobCount > 0 && <span className="toolbarBadge">{runningJobCount}</span>}
          </button>
          <UserMenu queryClient={queryClient} />
          <button onClick={() => jobsQuery.refetch()}>
            <RefreshCw size={16} />
          </button>
        </div>
      </section>

      <section className="workspace assetWorkspace">
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
          onDeleteJob={deleteJob}
        />
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
                  <div className="mapToolBulk">
                    <label>
                      <span>Bulk Ropewiki import</span>
                      <input
                        type="number"
                        min={1}
                        max={500}
                        value={bulkMaxResults}
                        onChange={(event) => {
                          const value = Number(event.target.value);
                          if (Number.isFinite(value) && value >= 1) {
                            setBulkMaxResults(Math.min(500, Math.floor(value)));
                          }
                        }}
                      />
                    </label>
                    <button
                      disabled={ropewikiBulkMutation.isPending || !bounds}
                      onClick={() => ropewikiBulkMutation.mutate()}
                      title="Import all Ropewiki canyons inside the AOI"
                    >
                      {ropewikiBulkMutation.isPending
                        ? "Discovering..."
                        : "Import all in AOI"}
                    </button>
                    {ropewikiBulkMutation.error && (
                      <div className="error">
                        {String((ropewikiBulkMutation.error as Error).message)}
                      </div>
                    )}
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
                  setOpenMapTool(openMapTool === "ravine" ? undefined : "ravine")
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
                  setOpenMapTool(openMapTool === "opacity" ? undefined : "opacity")
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
                <button type="button" onClick={() => demDialogRef.current?.close()}>
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

          <MapView layers={visibleLayers} />
        </section>

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
      </section>

      {jobsOpen && (
        <JobActivityPanel
          jobs={jobs}
          onClose={() => setJobsOpen(false)}
          onDeleteJob={deleteJob}
          onCancelJob={cancelRunningJob}
        />
      )}
      {pipelineOpen && (
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
