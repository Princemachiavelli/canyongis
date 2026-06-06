import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box,
  Eye,
  LocateFixed,
  Play,
  Plus,
  RefreshCw,
  Search,
  Tags,
  Trash2,
  X,
} from "lucide-react";
import {
  getProjectPipeline,
  hideProjectAsset,
  linkProjectAsset,
  listAssetLibrary,
  listHiddenProjectAssets,
  listProjectAssets,
  setKmlContainer,
  syncKmlImport,
  unhideProjectAsset,
  unlinkProjectAsset,
  type Asset,
  type AssetLineageEdge,
  type Job,
  type JobSpec,
} from "./api";
import { useAppStore } from "./state";
import {
  assetLayerSourceType,
  assetSynthesizedLayerId,
  descriptorsFromJob,
  isPreviewAsset,
  isPreviewAssetKind,
  synthesizedLayerFromAsset,
  type LayerDescriptor,
} from "./layers";
import {
  assetBounds,
  assetGroup,
  assetName,
  compatibleSpecs,
} from "./assetUtils";

type AssetLibraryProps = {
  projectId?: string;
  jobs: Job[];
  specs: JobSpec[];
  variant?: "project" | "library";
  currentProjectAssetIds?: Set<string>;
  selectedAssetId?: string;
  onSelectAsset: (assetId: string) => void;
  onRunTool: (
    spec: JobSpec,
    initialInputs: Record<string, string | undefined>,
  ) => void;
  onDeleteJob: (job: Job) => void;
  onClose?: () => void;
};

type AssetRowExtras = {
  hiddenInActiveProject: boolean;
  onHide?: (asset: Asset) => void;
  onUnhide?: (asset: Asset) => void;
};

type AssetGroup = {
  key: string;
  label: string;
  assets: Asset[];
};

type AssetTreeData = {
  roots: Asset[];
  childrenByParent: Map<string, Asset[]>;
  previewByParentId: Map<string, Asset>;
};

type JobOutputRun = {
  job: Job;
  assets: Asset[];
};

type JobOutputBundle = {
  key: string;
  label: string;
  runs: JobOutputRun[];
};

function isRouteAsset(asset: Asset): boolean {
  return asset.kind === "vector.kml.route" || asset.kind.startsWith("vector.kml.route.");
}

function syncableJob(job: Job | undefined): boolean {
  if (!job || job.kind !== "kml-import" || job.status !== "succeeded") return false;
  const source = (job.result as { source?: { type?: string } } | undefined)?.source;
  return source?.type === "ropewiki" || source?.type === "caltopo" || source?.type === "url";
}

function kmlJobContainerId(job: Job): string | undefined {
  const resultId = job.result?.container_dem_job_id;
  if (typeof resultId === "string" && resultId) return resultId;
  const paramId = job.params.container_dem_job_id;
  return typeof paramId === "string" && paramId ? paramId : undefined;
}

function demJobLabel(job: Job): string {
  const name =
    (job.result?.name as string | undefined) ?? (job.params.name as string | undefined);
  return name ? `${name} · ${job.id.slice(0, 8)}` : job.id;
}

function jobRunLabel(job: Job): string {
  const name =
    (job.result?.name as string | undefined) ?? (job.params.name as string | undefined);
  return name ? `${name} · ${job.id.slice(0, 8)}` : job.id.slice(0, 8);
}

function formatBytes(value: number | null): string | undefined {
  if (value == null) return undefined;
  if (value < 1024) return `${value} B`;
  const mb = value / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function formatBounds(bounds: ReturnType<typeof assetBounds>): string | undefined {
  if (!bounds) return undefined;
  return `${bounds.west.toFixed(4)}, ${bounds.south.toFixed(4)} to ${bounds.east.toFixed(4)}, ${bounds.north.toFixed(4)}`;
}

function lineageGroup(kind: string): { key: string; label: string } {
  if (kind === "raster.dem") return { key: "dem", label: "DEMs" };
  if (kind === "raster.hillshade") return { key: "hillshade", label: "Hillshades" };
  if (kind.startsWith("raster.score")) return { key: "score", label: "Score Rasters" };
  if (kind.startsWith("vector.kml.route")) return { key: "route", label: "Routes" };
  if (kind.startsWith("vector.kml.spine")) return { key: "spine", label: "Spines" };
  if (isPreviewAssetKind(kind)) return { key: "preview", label: "Previews" };
  return assetGroup(kind);
}

function canonicalAssetForJob(assets: Asset[], jobId: string): Asset | undefined {
  return assets
    .filter((asset) => asset.job_id === jobId && !isPreviewAsset(asset))
    .sort(assetSort)[0];
}

function assetOutputKey(asset: Asset): string | undefined {
  const key = asset.metadata.output_key;
  return typeof key === "string" && key ? key : undefined;
}

function assetRole(asset: Asset): string | undefined {
  const role = asset.metadata.role;
  return typeof role === "string" && role ? role : undefined;
}

function assetProjectsText(asset: Asset): string | undefined {
  if (!asset.projects || asset.projects.length === 0) return undefined;
  const names = [...new Set(asset.projects.map((project) => project.name))].sort((a, b) =>
    a.localeCompare(b),
  );
  return names.length > 0 ? names.join(", ") : undefined;
}

function previewLayerKey(asset: Asset): string | undefined {
  const key = asset.metadata.layer_key;
  return typeof key === "string" && key ? key : undefined;
}

function previewParentForJob(assets: Asset[], preview: Asset): Asset | undefined {
  if (!preview.job_id) return undefined;
  const siblings = assets.filter(
    (asset) => asset.job_id === preview.job_id && !isPreviewAsset(asset),
  );
  if (siblings.length === 0) return undefined;
  if (siblings.length === 1) return siblings[0];

  const layerKey = previewLayerKey(preview);
  if (layerKey) {
    const keyMatches = siblings.filter((asset) => {
      const outputKey = assetOutputKey(asset);
      return Boolean(
        outputKey &&
          (layerKey === outputKey ||
            layerKey === `panel_${outputKey}` ||
            layerKey.includes(outputKey)),
      );
    });
    if (keyMatches.length === 1) return keyMatches[0];
  }

  const role = assetRole(preview);
  if (role) {
    const roleMatches = siblings.filter((asset) => assetRole(asset) === role);
    if (roleMatches.length === 1) return roleMatches[0];
  }

  return canonicalAssetForJob(assets, preview.job_id);
}

function descriptorsForAsset(asset: Asset, job: Job | undefined, name: string): LayerDescriptor[] {
  if (isPreviewAsset(asset)) {
    const synthesized = synthesizedLayerFromAsset(asset, name);
    return synthesized ? [synthesized] : [];
  }
  if (asset.kind.startsWith("raster.")) return [];
  const descriptors = job ? descriptorsFromJob(job) : [];
  return descriptors.filter(
    (descriptor) =>
      descriptor.kind === "vector-kml" ||
      descriptor.kind === "vector-geojson" ||
      descriptor.kind === "point-events",
  );
}

function assetSort(a: Asset, b: Asset): number {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
  return a.id.localeCompare(b.id);
}

function buildTree(assets: Asset[], edges: AssetLineageEdge[]): AssetTreeData {
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  const childrenByParent = new Map<string, Asset[]>();
  const hasParent = new Set<string>();
  const parentByChild = new Map<string, string>();
  const canonicalByJob = new Map<string, Asset>();
  const previewByParentId = new Map<string, Asset>();
  const previewHosts = new Set<string>();
  const attachChild = (parent: Asset, child: Asset) => {
    if (parent.id === child.id) return;
    const existingParentId = parentByChild.get(child.id);
    if (existingParentId && existingParentId !== parent.id) {
      const existingChildren = childrenByParent.get(existingParentId) ?? [];
      childrenByParent.set(
        existingParentId,
        existingChildren.filter((asset) => asset.id !== child.id),
      );
    }
    const children = childrenByParent.get(parent.id) ?? [];
    if (!children.some((asset) => asset.id === child.id)) children.push(child);
    childrenByParent.set(parent.id, children);
    hasParent.add(child.id);
    parentByChild.set(child.id, parent.id);
  };
  const attachPreview = (parent: Asset, preview: Asset) => {
    if (parent.id === preview.id) return;
    if (!previewByParentId.has(parent.id)) previewByParentId.set(parent.id, preview);
    previewHosts.add(preview.id);
    hasParent.add(preview.id);
  };
  for (const asset of assets) {
    if (!asset.job_id || isPreviewAsset(asset)) continue;
    if (!canonicalByJob.has(asset.job_id)) {
      canonicalByJob.set(asset.job_id, canonicalAssetForJob(assets, asset.job_id) ?? asset);
    }
  }
  for (const edge of edges) {
    const producer = assetsById.get(edge.producer);
    const consumer = assetsById.get(edge.consumer);
    if (!producer || !consumer) continue;
    const canonical = consumer.job_id ? canonicalByJob.get(consumer.job_id) : undefined;
    if (isPreviewAsset(consumer)) {
      const previewParent =
        previewParentForJob(assets, consumer) ?? canonical ?? producer;
      attachPreview(previewParent, consumer);
      if (canonical && canonical.id !== consumer.id && !hasParent.has(canonical.id)) {
        attachChild(producer, canonical);
      }
      continue;
    }
    attachChild(producer, consumer);
    if (canonical && canonical.id !== consumer.id && !hasParent.has(canonical.id)) {
      attachChild(producer, canonical);
    }
  }
  for (const asset of assets) {
    if (!isPreviewAsset(asset)) continue;
    if (previewHosts.has(asset.id)) continue;
    const parent = previewParentForJob(assets, asset);
    if (parent) attachPreview(parent, asset);
  }
  for (const children of childrenByParent.values()) children.sort(assetSort);
  return {
    roots: assets.filter((asset) => !hasParent.has(asset.id)).sort(assetSort),
    childrenByParent,
    previewByParentId,
  };
}

function matchingAssetIds(assets: Asset[], jobsById: Map<string, Job>, search: string): Set<string> {
  const q = search.trim().toLowerCase();
  if (!q) return new Set(assets.map((asset) => asset.id));
  const ids = new Set<string>();
  for (const asset of assets) {
    const job = asset.job_id ? jobsById.get(asset.job_id) : undefined;
    const name = assetName(asset, job);
    const haystack = `${name} ${asset.kind} ${asset.id} ${asset.path ?? ""}`.toLowerCase();
    if (haystack.includes(q)) ids.add(asset.id);
  }
  return ids;
}

function expandMatches(matches: Set<string>, edges: AssetLineageEdge[]): Set<string> {
  if (matches.size === 0) return matches;
  const expanded = new Set(matches);
  let changed = true;
  while (changed) {
    changed = false;
    for (const edge of edges) {
      if (expanded.has(edge.consumer) && !expanded.has(edge.producer)) {
        expanded.add(edge.producer);
        changed = true;
      }
      if (expanded.has(edge.producer) && !expanded.has(edge.consumer)) {
        expanded.add(edge.consumer);
        changed = true;
      }
    }
  }
  return expanded;
}

function groupAssets(assets: Asset[]): AssetGroup[] {
  const byKey = new Map<string, AssetGroup>();
  for (const asset of assets) {
    const groupInfo = lineageGroup(asset.kind);
    const group = byKey.get(groupInfo.key) ?? { ...groupInfo, assets: [] };
    group.assets.push(asset);
    byKey.set(group.key, group);
  }
  for (const group of byKey.values()) group.assets.sort(assetSort);
  return [...byKey.values()].sort((a, b) => a.label.localeCompare(b.label));
}

function buildJobOutputBundles(
  assets: Asset[],
  jobsById: Map<string, Job>,
  specs: JobSpec[],
): { bundles: JobOutputBundle[]; looseAssets: Asset[] } {
  const specByKey = new Map(specs.map((spec) => [spec.key, spec]));
  const runsByKind = new Map<string, Map<string, JobOutputRun>>();
  const bundledAssetIds = new Set<string>();

  for (const asset of assets) {
    if (!asset.job_id || isPreviewAsset(asset)) continue;
    const job = jobsById.get(asset.job_id);
    if (!job) continue;
    const runs = runsByKind.get(job.kind) ?? new Map<string, JobOutputRun>();
    const run = runs.get(job.id) ?? { job, assets: [] };
    run.assets.push(asset);
    runs.set(job.id, run);
    runsByKind.set(job.kind, runs);
  }

  const bundles: JobOutputBundle[] = [];
  for (const [kind, runsMap] of runsByKind) {
    const runs = [...runsMap.values()]
      .map((run) => ({ ...run, assets: run.assets.sort(assetSort) }))
      .sort((a, b) => (a.job.updated_at < b.job.updated_at ? 1 : -1));
    const shouldBundle = runs.length > 1 || runs.some((run) => run.assets.length > 1);
    if (!shouldBundle) continue;
    for (const run of runs) for (const asset of run.assets) bundledAssetIds.add(asset.id);
    bundles.push({
      key: kind,
      label: specByKey.get(kind)?.label ?? kind,
      runs,
    });
  }

  bundles.sort((a, b) => a.label.localeCompare(b.label));
  return {
    bundles,
    looseAssets: assets.filter((asset) => !bundledAssetIds.has(asset.id)),
  };
}

function ContainerPicker({
  job,
  demJobs,
  expanded,
}: {
  job: Job;
  demJobs: Job[];
  expanded: boolean;
}) {
  const queryClient = useQueryClient();
  const currentId = kmlJobContainerId(job);
  const currentLabel = useMemo(() => {
    if (!currentId) return "(detached)";
    const match = demJobs.find((d) => d.id === currentId);
    return match ? demJobLabel(match) : `${currentId.slice(0, 8)} (missing)`;
  }, [currentId, demJobs]);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (expanded) setOpen(true);
  }, [expanded]);
  const mutation = useMutation({
    mutationFn: (newId: string | null) => setKmlContainer(job.id, newId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["project-assets"] });
      queryClient.invalidateQueries({ queryKey: ["project-pipeline"] });
    },
  });
  const showOpen = open || expanded;
  return (
    <div className="containerPicker">
      <button
        type="button"
        className={showOpen ? "toggled miniButton" : "miniButton"}
        onClick={() => setOpen((o) => !o)}
        title="Change AOI container"
      >
        <Box size={12} />
        AOI: {currentLabel}
      </button>
      {showOpen && (
        <div className="containerPickerBody">
          <select
            value={currentId ?? ""}
            disabled={mutation.isPending}
            onChange={(event) => {
              const value = event.target.value;
              mutation.mutate(value ? value : null);
            }}
          >
            <option value="">— Detached —</option>
            {demJobs.map((dem) => (
              <option key={dem.id} value={dem.id}>
                {demJobLabel(dem)}
              </option>
            ))}
          </select>
          {currentId && (
            <button
              type="button"
              className="miniButton"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate(null)}
            >
              Detach
            </button>
          )}
          {mutation.error && (
            <span className="error">{String((mutation.error as Error).message)}</span>
          )}
        </div>
      )}
    </div>
  );
}

function AssetRow({
  asset,
  job,
  specs,
  demJobs,
  variant,
  inCurrentProject,
  selected,
  preview,
  hiddenInActiveProject,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
  onAddAsset,
  onRemoveAsset,
  onHide,
  onUnhide,
}: {
  asset: Asset;
  job?: Job;
  specs: JobSpec[];
  demJobs: Job[];
  variant: "project" | "library";
  inCurrentProject: boolean;
  selected: boolean;
  preview?: Asset;
  hiddenInActiveProject: boolean;
  onSelectAsset: (assetId: string) => void;
  onRunTool: AssetLibraryProps["onRunTool"];
  onDeleteJob: AssetLibraryProps["onDeleteJob"];
  onAddAsset?: (asset: Asset) => void;
  onRemoveAsset?: (asset: Asset) => void;
  onHide?: (asset: Asset) => void;
  onUnhide?: (asset: Asset) => void;
}) {
  const setActiveDemJob = useAppStore((s) => s.setActiveDemJob);
  const toggleFootprint = useAppStore((s) => s.toggleFootprint);
  const requestPan = useAppStore((s) => s.requestPan);
  const setBounds = useAppStore((s) => s.setBounds);
  const setBaseLayer = useAppStore((s) => s.setBaseLayer);
  const setOverlayLayer = useAppStore((s) => s.setOverlayLayer);
  const toggleLayer = useAppStore((s) => s.toggleLayer);
  const toggleLayerLabels = useAppStore((s) => s.toggleLayerLabels);
  const baseLayerId = useAppStore((s) => s.baseLayerId);
  const overlayLayerId = useAppStore((s) => s.overlayLayerId);
  const visibleLayerIds = useAppStore((s) => s.visibleLayerIds);
  const labelLayerIds = useAppStore((s) => s.labelLayerIds);
  const footprintVisible = useAppStore((s) => Boolean(s.footprints[asset.id]));

  const bounds = assetBounds(asset);
  const name = assetName(asset, job);
  const descriptors = useMemo(() => descriptorsForAsset(asset, job, name), [asset, job, name]);
  const tools = useMemo(() => compatibleSpecs(asset, specs), [asset, specs]);
  const size = formatBytes(asset.size_bytes);
  const boundsText = formatBounds(bounds);
  const projectsText = assetProjectsText(asset);
  const canDeleteJob = Boolean(
    job && !asset.linked && job.status !== "queued" && job.status !== "running",
  );
  const queryClient = useQueryClient();
  const canSync = isRouteAsset(asset) && syncableJob(job);
  const syncMutation = useMutation({
    mutationFn: () => {
      if (!job) throw new Error("no job");
      return syncKmlImport(job.id);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["project-assets"] });
      queryClient.invalidateQueries({ queryKey: ["asset-library"] });
      queryClient.invalidateQueries({ queryKey: ["project-pipeline"] });
    },
  });

  const previewLayerId = preview ? assetSynthesizedLayerId(preview.id) : undefined;
  const previewActive = Boolean(
    previewLayerId && (baseLayerId === previewLayerId || overlayLayerId === previewLayerId),
  );
  const togglePreviewOverlay = () => {
    if (!preview || !previewLayerId) return;
    const role = typeof preview.metadata.role === "string" ? preview.metadata.role : undefined;
    if (role === "base") {
      setBaseLayer(baseLayerId === previewLayerId ? undefined : previewLayerId);
    } else {
      setOverlayLayer(overlayLayerId === previewLayerId ? undefined : previewLayerId);
    }
  };

  return (
    <div className={`assetRow ${selected ? "selected" : ""}`}>
      <div className="assetRowHead">
        {preview && preview.url && (
          <button
            type="button"
            className={`assetPreviewIcon${previewActive ? " active" : ""}`}
            title={previewActive ? "Hide preview on map" : "Show preview on map"}
            onClick={togglePreviewOverlay}
          >
            {assetLayerSourceType(preview) === "image" ? (
              <img src={preview.url} alt="" loading="lazy" />
            ) : (
              <Eye size={16} />
            )}
          </button>
        )}
        <button
          type="button"
          className="assetNameButton"
          title={asset.path ?? asset.id}
          onClick={() => {
            onSelectAsset(asset.id);
            if (asset.kind === "raster.dem" && job) setActiveDemJob(job);
            if (bounds) requestPan(bounds);
          }}
        >
          <strong>{name}</strong>
          <code>{asset.id}</code>
        </button>
        {asset.linked && (
          <span className="assetLinked" title={`Linked from ${asset.project_name}`}>
            link
          </span>
        )}
      </div>
      <div className="assetMeta">
        <span>{asset.kind}</span>
        {job && <span>job {job.id.slice(0, 8)}</span>}
        {asset.path && <span>{asset.path.split("/").pop()}</span>}
        {size && <span>{size}</span>}
        {projectsText && <span>projects {projectsText}</span>}
        {boundsText && <span>bounds {boundsText}</span>}
      </div>
      {isRouteAsset(asset) && job && (
        <ContainerPicker job={job} demJobs={demJobs} expanded={selected} />
      )}
      <div className="assetActions">
        {bounds && (
          <>
            <button
              className={footprintVisible ? "toggled miniButton" : "miniButton"}
              onClick={() =>
                toggleFootprint({
                  id: asset.id,
                  name,
                  bounds,
                })
              }
            >
              <Eye size={12} />
              Bounds
            </button>
            <button className="miniButton" onClick={() => requestPan(bounds)}>
              <LocateFixed size={12} />
              Pan
            </button>
            <button className="miniButton" onClick={() => setBounds(bounds)}>
              AOI
            </button>
          </>
        )}
        {descriptors.map((descriptor) => {
          if (descriptor.kind === "base-raster") {
            const active = baseLayerId === descriptor.id;
            return (
              <button
                key={descriptor.id}
                className={active ? "toggled miniButton" : "miniButton"}
                onClick={() => setBaseLayer(active ? undefined : descriptor.id)}
              >
                <Eye size={12} />
                Base
              </button>
            );
          }
          if (descriptor.kind === "raster-overlay") {
            const active = overlayLayerId === descriptor.id;
            return (
              <button
                key={descriptor.id}
                className={active ? "toggled miniButton" : "miniButton"}
                onClick={() => setOverlayLayer(active ? undefined : descriptor.id)}
              >
                <Eye size={12} />
                Display
              </button>
            );
          }
          if (
            descriptor.kind === "vector-kml" ||
            descriptor.kind === "vector-geojson" ||
            descriptor.kind === "point-events"
          ) {
            const visible = Boolean(visibleLayerIds[descriptor.id]);
            const labelsVisible = Boolean(labelLayerIds[descriptor.id]);
            return (
              <span key={descriptor.id} className="assetInlineActions">
                <button
                  className={visible ? "toggled miniButton" : "miniButton"}
                  onClick={() => toggleLayer(descriptor.id)}
                >
                  <Eye size={12} />
                  {visible ? "Hide" : "Show"}
                </button>
                {descriptor.kind === "vector-kml" && (
                  <button
                    className={labelsVisible ? "toggled miniButton" : "miniButton"}
                    disabled={!visible}
                    onClick={() => toggleLayerLabels(descriptor.id)}
                  >
                    <Tags size={12} />
                    Labels
                  </button>
                )}
              </span>
            );
          }
          return null;
        })}
        {canSync && (
          <button
            className="miniButton"
            disabled={syncMutation.isPending}
            onClick={() => syncMutation.mutate()}
            title={
              syncMutation.error
                ? `Sync failed: ${(syncMutation.error as Error).message}`
                : "Re-fetch the KML from its source URL"
            }
          >
            <RefreshCw size={12} />
            {syncMutation.isPending ? "Syncing..." : "Sync"}
          </button>
        )}
        {variant === "library" && (
          <>
            {hiddenInActiveProject ? (
              <button
                className="miniButton"
                onClick={() => onUnhide?.(asset)}
                title="Un-hide this asset in the active project"
              >
                <Plus size={12} />
                Add to project
              </button>
            ) : asset.kind === "raster.dem" || !inCurrentProject ? (
              <button
                className="miniButton"
                disabled={inCurrentProject}
                onClick={() => onAddAsset?.(asset)}
                title={
                  asset.kind === "raster.dem"
                    ? "Add this container to the current project"
                    : "Link this asset into the current project"
                }
              >
                <Plus size={12} />
                {inCurrentProject ? "In project" : "Add to project"}
              </button>
            ) : null}
            {job && canDeleteJob && (
              <button
                className="danger miniButton"
                title="Delete this job and its output files (asset library only)"
                onClick={() => onDeleteJob(job)}
              >
                <Trash2 size={12} />
                Delete job
              </button>
            )}
          </>
        )}
        {variant === "project" && (
          <button
            className="miniButton"
            onClick={() =>
              asset.linked ? onRemoveAsset?.(asset) : onHide?.(asset)
            }
            title={
              asset.linked
                ? "Unlink this asset from the current project"
                : "Hide this asset in the current project (still available in the asset library)"
            }
          >
            <X size={12} />
            Remove from project
          </button>
        )}
      </div>
      {tools.length > 0 && asset.job_id && (
        <div className="assetTools">
          {tools.map(({ spec, inputKey }) => (
            <button
              key={`${spec.key}:${inputKey}`}
              className="miniButton"
              disabled={!spec.available}
              title={spec.unavailable_reason ?? spec.description}
              onClick={() => onRunTool(spec, { [inputKey]: asset.job_id ?? undefined })}
            >
              <Play size={12} />
              {spec.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function JobOutputGroup({
  bundle,
  childrenByParent,
  previewByParentId,
  jobsById,
  specs,
  demJobs,
  variant,
  currentProjectAssetIds,
  hiddenInActiveProjectIds,
  selectedAssetId,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
  onAddAsset,
  onRemoveAsset,
  onHide,
  onUnhide,
}: {
  bundle: JobOutputBundle;
  childrenByParent: Map<string, Asset[]>;
  previewByParentId: Map<string, Asset>;
  jobsById: Map<string, Job>;
  specs: JobSpec[];
  demJobs: Job[];
  variant: "project" | "library";
  currentProjectAssetIds: Set<string>;
  hiddenInActiveProjectIds: Set<string>;
  selectedAssetId?: string;
  onSelectAsset: (assetId: string) => void;
  onRunTool: AssetLibraryProps["onRunTool"];
  onDeleteJob: AssetLibraryProps["onDeleteJob"];
  onAddAsset?: (asset: Asset) => void;
  onRemoveAsset?: (asset: Asset) => void;
  onHide?: AssetRowExtras["onHide"];
  onUnhide?: AssetRowExtras["onUnhide"];
}) {
  const [selectedJobId, setSelectedJobId] = useState(bundle.runs[0]?.job.id ?? "");
  const selectedRun = bundle.runs.find((run) => run.job.id === selectedJobId) ?? bundle.runs[0];
  const [selectedOutputId, setSelectedOutputId] = useState(selectedRun?.assets[0]?.id ?? "");
  const selectedAsset =
    selectedRun?.assets.find((asset) => asset.id === selectedOutputId) ?? selectedRun?.assets[0];
  const outputCount = bundle.runs.reduce((sum, run) => sum + run.assets.length, 0);

  useEffect(() => {
    if (!bundle.runs.some((run) => run.job.id === selectedJobId)) {
      setSelectedJobId(bundle.runs[0]?.job.id ?? "");
    }
  }, [bundle.runs, selectedJobId]);

  useEffect(() => {
    if (!selectedRun?.assets.some((asset) => asset.id === selectedOutputId)) {
      setSelectedOutputId(selectedRun?.assets[0]?.id ?? "");
    }
  }, [selectedRun, selectedOutputId]);

  if (!selectedRun || !selectedAsset) return null;

  return (
    <details className="assetTypeGroup jobOutputGroup" open>
      <summary>
        <span>{bundle.label}</span>
        <span className="assetCount">{outputCount}</span>
      </summary>
      <div className="jobOutputControls">
        {bundle.runs.length > 1 && (
          <label>
            <span>Run</span>
            <select
              value={selectedRun.job.id}
              onChange={(event) => setSelectedJobId(event.target.value)}
            >
              {bundle.runs.map((run) => (
                <option key={run.job.id} value={run.job.id}>
                  {jobRunLabel(run.job)}
                </option>
              ))}
            </select>
          </label>
        )}
        {selectedRun.assets.length > 1 && (
          <label>
            <span>Output</span>
            <select
              value={selectedAsset.id}
              onChange={(event) => setSelectedOutputId(event.target.value)}
            >
              {selectedRun.assets.map((asset) => (
                <option key={asset.id} value={asset.id}>
                  {assetName(asset, selectedRun.job)}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div className="assetTreeNode">
        <AssetRow
          asset={selectedAsset}
          job={selectedRun.job}
          specs={specs}
          demJobs={demJobs}
          variant={variant}
          inCurrentProject={currentProjectAssetIds.has(selectedAsset.id)}
          selected={selectedAsset.id === selectedAssetId}
          preview={previewByParentId.get(selectedAsset.id)}
          hiddenInActiveProject={hiddenInActiveProjectIds.has(selectedAsset.id)}
          onSelectAsset={onSelectAsset}
          onRunTool={onRunTool}
          onDeleteJob={onDeleteJob}
          onAddAsset={onAddAsset}
          onRemoveAsset={onRemoveAsset}
          onHide={onHide}
          onUnhide={onUnhide}
        />
        <AssetTreeChildren
          parentId={selectedAsset.id}
          childrenByParent={childrenByParent}
          previewByParentId={previewByParentId}
          jobsById={jobsById}
          specs={specs}
          demJobs={demJobs}
          variant={variant}
          currentProjectAssetIds={currentProjectAssetIds}
          hiddenInActiveProjectIds={hiddenInActiveProjectIds}
          selectedAssetId={selectedAssetId}
          onSelectAsset={onSelectAsset}
          onRunTool={onRunTool}
          onDeleteJob={onDeleteJob}
          onAddAsset={onAddAsset}
          onRemoveAsset={onRemoveAsset}
          onHide={onHide}
          onUnhide={onUnhide}
        />
      </div>
    </details>
  );
}

function AssetTreeChildren({
  parentId,
  childrenByParent,
  previewByParentId,
  jobsById,
  specs,
  demJobs,
  variant,
  currentProjectAssetIds,
  hiddenInActiveProjectIds,
  selectedAssetId,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
  onAddAsset,
  onRemoveAsset,
  onHide,
  onUnhide,
}: {
  parentId: string;
  childrenByParent: Map<string, Asset[]>;
  previewByParentId: Map<string, Asset>;
  jobsById: Map<string, Job>;
  specs: JobSpec[];
  demJobs: Job[];
  variant: "project" | "library";
  currentProjectAssetIds: Set<string>;
  hiddenInActiveProjectIds: Set<string>;
  selectedAssetId?: string;
  onSelectAsset: (assetId: string) => void;
  onRunTool: AssetLibraryProps["onRunTool"];
  onDeleteJob: AssetLibraryProps["onDeleteJob"];
  onAddAsset?: (asset: Asset) => void;
  onRemoveAsset?: (asset: Asset) => void;
  onHide?: AssetRowExtras["onHide"];
  onUnhide?: AssetRowExtras["onUnhide"];
}) {
  const { bundles, looseAssets } = buildJobOutputBundles(
    childrenByParent.get(parentId) ?? [],
    jobsById,
    specs,
  );
  const groups = groupAssets(looseAssets);
  if (groups.length === 0 && bundles.length === 0) return null;
  return (
    <div className="assetTreeChildren">
      {bundles.map((bundle) => (
        <JobOutputGroup
          key={`${parentId}:job:${bundle.key}`}
          bundle={bundle}
          childrenByParent={childrenByParent}
          previewByParentId={previewByParentId}
          jobsById={jobsById}
          specs={specs}
          demJobs={demJobs}
          variant={variant}
          currentProjectAssetIds={currentProjectAssetIds}
          hiddenInActiveProjectIds={hiddenInActiveProjectIds}
          selectedAssetId={selectedAssetId}
          onSelectAsset={onSelectAsset}
          onRunTool={onRunTool}
          onDeleteJob={onDeleteJob}
          onAddAsset={onAddAsset}
          onRemoveAsset={onRemoveAsset}
          onHide={onHide}
          onUnhide={onUnhide}
        />
      ))}
      {groups.map((group) => (
        <details key={`${parentId}:${group.key}`} className="assetTypeGroup">
          <summary>
            <span>{group.label}</span>
            <span className="assetCount">{group.assets.length}</span>
          </summary>
          <div className="assetRows">
            {group.assets.map((asset) => (
              <div key={asset.id} className="assetTreeNode">
                <AssetRow
                  asset={asset}
                  job={asset.job_id ? jobsById.get(asset.job_id) : undefined}
                  specs={specs}
                  demJobs={demJobs}
                  variant={variant}
                  inCurrentProject={currentProjectAssetIds.has(asset.id)}
                  selected={asset.id === selectedAssetId}
                  preview={previewByParentId.get(asset.id)}
                  hiddenInActiveProject={hiddenInActiveProjectIds.has(asset.id)}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
                  onAddAsset={onAddAsset}
                  onRemoveAsset={onRemoveAsset}
                  onHide={onHide}
                  onUnhide={onUnhide}
                />
                <AssetTreeChildren
                  parentId={asset.id}
                  childrenByParent={childrenByParent}
                  previewByParentId={previewByParentId}
                  jobsById={jobsById}
                  specs={specs}
                  demJobs={demJobs}
                  variant={variant}
                  currentProjectAssetIds={currentProjectAssetIds}
                  hiddenInActiveProjectIds={hiddenInActiveProjectIds}
                  selectedAssetId={selectedAssetId}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
                  onAddAsset={onAddAsset}
                  onRemoveAsset={onRemoveAsset}
                  onHide={onHide}
                  onUnhide={onUnhide}
                />
              </div>
            ))}
          </div>
        </details>
      ))}
    </div>
  );
}

export function AssetLibrary({
  projectId,
  jobs,
  specs,
  variant = "project",
  currentProjectAssetIds,
  selectedAssetId,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
  onClose,
}: AssetLibraryProps) {
  const [search, setSearch] = useState("");
  const queryClient = useQueryClient();
  const assetsQuery = useQuery({
    queryKey: variant === "library" ? ["asset-library"] : ["project-assets", projectId],
    queryFn: () =>
      variant === "library"
        ? listAssetLibrary()
        : listProjectAssets(projectId as string),
    enabled: variant === "library" || Boolean(projectId),
    refetchInterval: 2000,
  });
  const pipelineQuery = useQuery({
    queryKey: ["project-pipeline", projectId],
    queryFn: () => getProjectPipeline(projectId as string),
    enabled: variant === "project" && Boolean(projectId),
    refetchInterval: 2000,
  });

  const hiddenQuery = useQuery({
    queryKey: ["project-hidden-assets", projectId],
    queryFn: () => listHiddenProjectAssets(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: 5000,
  });
  const hiddenInActiveProjectIds = useMemo(
    () => new Set(hiddenQuery.data ?? []),
    [hiddenQuery.data],
  );
  const invalidateProjectAndLibrary = () => {
    queryClient.invalidateQueries({ queryKey: ["project-assets"] });
    queryClient.invalidateQueries({ queryKey: ["project-pipeline"] });
    queryClient.invalidateQueries({ queryKey: ["project-hidden-assets"] });
    queryClient.invalidateQueries({ queryKey: ["asset-library"] });
  };
  const linkMutation = useMutation({
    mutationFn: (asset: Asset) => {
      if (!projectId) throw new Error("Choose a project before adding assets");
      return linkProjectAsset(projectId, asset.id);
    },
    onSuccess: invalidateProjectAndLibrary,
  });
  const unlinkMutation = useMutation({
    mutationFn: (asset: Asset) => {
      if (!projectId) throw new Error("Choose a project before removing assets");
      return unlinkProjectAsset(projectId, asset.id);
    },
    onSuccess: invalidateProjectAndLibrary,
  });
  const hideMutation = useMutation({
    mutationFn: (asset: Asset) => {
      if (!projectId) throw new Error("Choose a project before hiding assets");
      return hideProjectAsset(projectId, asset.id);
    },
    onSuccess: invalidateProjectAndLibrary,
  });
  const unhideMutation = useMutation({
    mutationFn: (asset: Asset) => {
      if (!projectId) throw new Error("Choose a project before un-hiding assets");
      return unhideProjectAsset(projectId, asset.id);
    },
    onSuccess: invalidateProjectAndLibrary,
  });

  const jobsById = useMemo(() => new Map(jobs.map((job) => [job.id, job])), [jobs]);
  const demJobs = useMemo(
    () =>
      jobs
        .filter((job) => job.kind === "build-dem" && job.status === "succeeded")
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1)),
    [jobs],
  );
  const tree = useMemo<AssetTreeData>(() => {
    const assets = assetsQuery.data ?? [];
    const edges = variant === "project" ? (pipelineQuery.data?.edges ?? []) : [];
    const visibleIds = expandMatches(matchingAssetIds(assets, jobsById, search), edges);
    return buildTree(
      assets.filter((asset) => visibleIds.has(asset.id)),
      edges,
    );
  }, [assetsQuery.data, jobsById, pipelineQuery.data, search, variant]);
  const demRoots = tree.roots.filter((asset) => asset.kind === "raster.dem");
  const otherRoots = tree.roots.filter((asset) => asset.kind !== "raster.dem");
  const rootJobOutputs = useMemo(
    () => buildJobOutputBundles(otherRoots, jobsById, specs),
    [jobsById, otherRoots, specs],
  );
  const activeAssetIds = currentProjectAssetIds ?? new Set((assetsQuery.data ?? []).map((a) => a.id));
  const title = variant === "library" ? "Asset Library" : "Current Project";

  return (
    <aside className="panel assetLibrary">
      <div className="panelHeader">
        <Tags size={16} />
        <h1>{title}</h1>
        {onClose && (
          <button className="miniButton" onClick={onClose} title="Close asset library">
            <X size={12} />
          </button>
        )}
      </div>
      <label className="assetSearch">
        <Search size={14} />
        <input
          placeholder="Search assets"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </label>
      {variant === "project" && !projectId && (
        <div className="empty">Choose a project to show assets.</div>
      )}
      {assetsQuery.error && <pre className="error">{String(assetsQuery.error)}</pre>}
      {(variant === "library" || projectId) && assetsQuery.isLoading && (
        <div className="empty">Loading assets...</div>
      )}
      {pipelineQuery.error && <pre className="error">{String(pipelineQuery.error)}</pre>}
      {(variant === "library" || projectId) && !assetsQuery.isLoading && tree.roots.length === 0 && (
        <div className="empty">
          {variant === "library" ? "No matching assets in the library." : "No matching assets in this project."}
        </div>
      )}
      {(linkMutation.error || unlinkMutation.error) && (
        <pre className="error">
          {String((linkMutation.error ?? unlinkMutation.error) as Error)}
        </pre>
      )}
      <div className="assetGroups">
        {demRoots.map((asset) => (
          <details key={`aoi:${asset.id}`} className="assetGroup" open>
            <summary>
              <span>{assetName(asset, asset.job_id ? jobsById.get(asset.job_id) : undefined)}</span>
              <span className="assetCount">1</span>
            </summary>
            {formatBounds(assetBounds(asset)) && (
              <div className="assetGroupMeta">bounds {formatBounds(assetBounds(asset))}</div>
            )}
            <div className="assetRows">
              <div className="assetTreeNode">
                <AssetRow
                  asset={asset}
                  job={asset.job_id ? jobsById.get(asset.job_id) : undefined}
                  specs={specs}
                  demJobs={demJobs}
                  variant={variant}
                  inCurrentProject={activeAssetIds.has(asset.id)}
                  selected={asset.id === selectedAssetId}
                  preview={tree.previewByParentId.get(asset.id)}
                  hiddenInActiveProject={hiddenInActiveProjectIds.has(asset.id)}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
                  onAddAsset={(a) => linkMutation.mutate(a)}
                  onRemoveAsset={(a) => unlinkMutation.mutate(a)}
                  onHide={(a) => hideMutation.mutate(a)}
                  onUnhide={(a) => unhideMutation.mutate(a)}
                />
                <AssetTreeChildren
                  parentId={asset.id}
                  childrenByParent={tree.childrenByParent}
                  previewByParentId={tree.previewByParentId}
                  jobsById={jobsById}
                  specs={specs}
                  demJobs={demJobs}
                  variant={variant}
                  currentProjectAssetIds={activeAssetIds}
                  hiddenInActiveProjectIds={hiddenInActiveProjectIds}
                  selectedAssetId={selectedAssetId}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
                  onAddAsset={(a) => linkMutation.mutate(a)}
                  onRemoveAsset={(a) => unlinkMutation.mutate(a)}
                  onHide={(a) => hideMutation.mutate(a)}
                  onUnhide={(a) => unhideMutation.mutate(a)}
                />
              </div>
            </div>
          </details>
        ))}
        {rootJobOutputs.bundles.map((bundle) => (
          <JobOutputGroup
            key={`root-job:${bundle.key}`}
            bundle={bundle}
            childrenByParent={tree.childrenByParent}
            previewByParentId={tree.previewByParentId}
            jobsById={jobsById}
            specs={specs}
            demJobs={demJobs}
            variant={variant}
            currentProjectAssetIds={activeAssetIds}
            hiddenInActiveProjectIds={hiddenInActiveProjectIds}
            selectedAssetId={selectedAssetId}
            onSelectAsset={onSelectAsset}
            onRunTool={onRunTool}
            onDeleteJob={onDeleteJob}
            onAddAsset={(a) => linkMutation.mutate(a)}
            onRemoveAsset={(a) => unlinkMutation.mutate(a)}
            onHide={(a) => hideMutation.mutate(a)}
            onUnhide={(a) => unhideMutation.mutate(a)}
          />
        ))}
        {groupAssets(rootJobOutputs.looseAssets).map((group) => (
          <details key={`root:${group.key}`} className="assetGroup" open>
            <summary>
              <span>{group.label}</span>
              <span className="assetCount">{group.assets.length}</span>
            </summary>
            <div className="assetRows">
              {group.assets.map((asset) => (
                <div key={asset.id} className="assetTreeNode">
                  <AssetRow
                    asset={asset}
                    job={asset.job_id ? jobsById.get(asset.job_id) : undefined}
                    specs={specs}
                    demJobs={demJobs}
                    variant={variant}
                    inCurrentProject={activeAssetIds.has(asset.id)}
                    selected={asset.id === selectedAssetId}
                    preview={tree.previewByParentId.get(asset.id)}
                    hiddenInActiveProject={hiddenInActiveProjectIds.has(asset.id)}
                    onSelectAsset={onSelectAsset}
                    onRunTool={onRunTool}
                    onDeleteJob={onDeleteJob}
                    onAddAsset={(a) => linkMutation.mutate(a)}
                    onRemoveAsset={(a) => unlinkMutation.mutate(a)}
                    onHide={(a) => hideMutation.mutate(a)}
                    onUnhide={(a) => unhideMutation.mutate(a)}
                  />
                  <AssetTreeChildren
                    parentId={asset.id}
                    childrenByParent={tree.childrenByParent}
                    previewByParentId={tree.previewByParentId}
                    jobsById={jobsById}
                    specs={specs}
                    demJobs={demJobs}
                    variant={variant}
                    currentProjectAssetIds={activeAssetIds}
                    hiddenInActiveProjectIds={hiddenInActiveProjectIds}
                    selectedAssetId={selectedAssetId}
                    onSelectAsset={onSelectAsset}
                    onRunTool={onRunTool}
                    onDeleteJob={onDeleteJob}
                    onAddAsset={(a) => linkMutation.mutate(a)}
                    onRemoveAsset={(a) => unlinkMutation.mutate(a)}
                    onHide={(a) => hideMutation.mutate(a)}
                    onUnhide={(a) => unhideMutation.mutate(a)}
                  />
                </div>
              ))}
            </div>
          </details>
        ))}
      </div>
    </aside>
  );
}
