import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  GitBranch,
  Layers,
  Map as MapIcon,
  Play,
  RefreshCw,
  Search,
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
  resolveMapUrl,
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

type SearchAction =
  | { kind: "jump"; lat: number; lng: number; label: string }
  | { kind: "ropewiki"; source: string };

type NominatimResult = {
  lat: string;
  lon: string;
  display_name?: string;
};

function pointBounds(lat: number, lng: number, span = 0.025): Bounds {
  return {
    west: lng - span,
    south: lat - span,
    east: lng + span,
    north: lat + span,
  };
}

function isValidLatLng(lat: number, lng: number): boolean {
  return (
    Number.isFinite(lat) &&
    Number.isFinite(lng) &&
    Math.abs(lat) <= 90 &&
    Math.abs(lng) <= 180
  );
}

function coordinateAction(value: string): SearchAction | undefined {
  const coord = "(-?\\d+(?:\\.\\d+)?)";
  const match = value.trim().match(new RegExp(`^\\s*${coord}[,\\s]+${coord}\\s*$`));
  if (!match) return undefined;
  const lat = Number(match[1]);
  const lng = Number(match[2]);
  if (!isValidLatLng(lat, lng)) return undefined;
  return { kind: "jump", lat, lng, label: `${lat.toFixed(5)}, ${lng.toFixed(5)}` };
}

function coordsFromText(value: string): SearchAction | undefined {
  let decoded = value;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    decoded = value;
  }
  const patterns = [
    /@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:[,/]|$)/,
    /[?&](?:q|query|ll)=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:[&]|$)/,
    /!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)/,
  ];
  for (const pattern of patterns) {
    const match = decoded.match(pattern);
    if (!match) continue;
    const lat = Number(match[1]);
    const lng = Number(match[2]);
    if (isValidLatLng(lat, lng)) {
      return { kind: "jump", lat, lng, label: `${lat.toFixed(5)}, ${lng.toFixed(5)}` };
    }
  }
  return coordinateAction(decoded);
}

function searchUrl(value: string): URL | undefined {
  try {
    return new URL(value);
  } catch {
    return undefined;
  }
}

function isRopewikiUrl(url: URL): boolean {
  return url.hostname.toLowerCase().replace(/^www\./, "") === "ropewiki.com";
}

function isGoogleMapsUrl(url: URL): boolean {
  const host = url.hostname.toLowerCase().replace(/^www\./, "");
  return (
    host === "maps.app.goo.gl" ||
    host === "goo.gl" ||
    host === "google.com" ||
    host.endsWith(".google.com")
  );
}

async function geocodeAddress(query: string): Promise<SearchAction> {
  const params = new URLSearchParams({ format: "jsonv2", limit: "1", q: query });
  const res = await fetch(`https://nominatim.openstreetmap.org/search?${params.toString()}`, {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`Geocoder failed: ${res.status} ${res.statusText}`);
  const data = (await res.json()) as NominatimResult[];
  const first = data[0];
  if (!first) throw new Error("No matching location found");
  const lat = Number(first.lat);
  const lng = Number(first.lon);
  if (!isValidLatLng(lat, lng)) throw new Error("Geocoder returned an invalid location");
  return { kind: "jump", lat, lng, label: first.display_name ?? query };
}

async function classifySearch(value: string): Promise<SearchAction> {
  const trimmed = value.trim();
  const directCoords = coordinateAction(trimmed);
  if (directCoords) return directCoords;
  const url = searchUrl(trimmed);
  if (url && isRopewikiUrl(url)) return { kind: "ropewiki", source: trimmed };
  if (url && isGoogleMapsUrl(url)) {
    const coords = coordsFromText(trimmed);
    if (coords) return coords;
    const resolved = await resolveMapUrl(trimmed);
    const resolvedCoords = coordsFromText(resolved);
    if (resolvedCoords) return resolvedCoords;
    throw new Error("Google Maps link did not include coordinates");
  }
  const linkCoords = coordsFromText(trimmed);
  if (linkCoords) return linkCoords;
  return geocodeAddress(trimmed);
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
  const requestPan = useAppStore((state) => state.requestPan);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const ropewikiDialogRef = useRef<HTMLDialogElement>(null);
  const demDialogRef = useRef<HTMLDialogElement>(null);

  const [name, setName] = useState("aoi");
  const [bufferM, setBufferM] = useState(200);
  const [openMapTool, setOpenMapTool] = useState<
    "aoi" | "kml" | "ravine" | "opacity" | undefined
  >();
  const [ropewikiSource, setRopewikiSource] = useState("");
  const [searchText, setSearchText] = useState("");
  const [searchStatus, setSearchStatus] = useState<string | undefined>();
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
  const searchMutation = useMutation({
    mutationFn: async (query: string) => {
      if (!query.trim()) {
        throw new Error("Enter a location, address, Ropewiki URL, or Google Maps link");
      }
      const action = await classifySearch(query);
      if (action.kind === "ropewiki") {
        const job = await importRopewikiKml(action.source, activeDemJob?.id);
        return { kind: "ropewiki" as const, job };
      }
      requestPan(pointBounds(action.lat, action.lng));
      return { kind: "jump" as const, label: action.label };
    },
    onSuccess: (result) => {
      if (result.kind === "ropewiki") {
        invalidate();
        setSearchStatus("Ropewiki KML import queued");
      } else {
        setSearchStatus(`Jumped to ${result.label}`);
      }
    },
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

  const runSearch = () => {
    const query = searchText.trim();
    if (!query) return;
    setSearchStatus(undefined);
    searchMutation.mutate(query);
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
        <form
          className="toolbarSearch"
          onSubmit={(event) => {
            event.preventDefault();
            runSearch();
          }}
        >
          <Search size={15} />
          <input
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            placeholder="Search GPS, address, Ropewiki, or Google Maps"
            aria-label="Search location or import link"
          />
          <button
            type="submit"
            disabled={!searchText.trim() || searchMutation.isPending}
          >
            {searchMutation.isPending ? "Searching" : "Go"}
          </button>
          {(searchMutation.error || searchStatus) && (
            <div
              className={
                searchMutation.error ? "toolbarSearchMessage error" : "toolbarSearchMessage"
              }
            >
              {searchMutation.error
                ? String((searchMutation.error as Error).message)
                : searchStatus}
            </div>
          )}
        </form>
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
