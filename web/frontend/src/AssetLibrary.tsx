import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Eye, LocateFixed, Play, Search, Tags, Trash2 } from "lucide-react";
import {
  getProjectPipeline,
  listProjectAssets,
  setKmlContainer,
  type Asset,
  type AssetLineageEdge,
  type Job,
  type JobSpec,
} from "./api";
import { useAppStore } from "./state";
import {
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
  selectedAssetId?: string;
  onSelectAsset: (assetId: string) => void;
  onRunTool: (
    spec: JobSpec,
    initialInputs: Record<string, string | undefined>,
  ) => void;
  onDeleteJob: (job: Job) => void;
};

type AssetGroup = {
  key: string;
  label: string;
  assets: Asset[];
};

type AssetTreeData = {
  roots: Asset[];
  childrenByParent: Map<string, Asset[]>;
};

function isRouteAsset(asset: Asset): boolean {
  return asset.kind === "vector.kml.route" || asset.kind.startsWith("vector.kml.route.");
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

function formatBytes(value: number | null): string | undefined {
  if (value == null) return undefined;
  if (value < 1024) return `${value} B`;
  const mb = value / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  return `${(value / 1024).toFixed(1)} KB`;
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
    const previewParent = isPreviewAsset(consumer)
      ? previewParentForJob(assets, consumer)
      : undefined;
    const parent = previewParent ?? (isPreviewAsset(consumer) && canonical ? canonical : producer);
    attachChild(parent, consumer);
    if (canonical && canonical.id !== consumer.id && !hasParent.has(canonical.id)) {
      attachChild(producer, canonical);
    }
  }
  for (const asset of assets) {
    if (!isPreviewAsset(asset) || parentByChild.has(asset.id)) continue;
    const parent = previewParentForJob(assets, asset);
    if (parent) attachChild(parent, asset);
  }
  for (const children of childrenByParent.values()) children.sort(assetSort);
  return {
    roots: assets.filter((asset) => !hasParent.has(asset.id)).sort(assetSort),
    childrenByParent,
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
  selected,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
}: {
  asset: Asset;
  job?: Job;
  specs: JobSpec[];
  demJobs: Job[];
  selected: boolean;
  onSelectAsset: (assetId: string) => void;
  onRunTool: AssetLibraryProps["onRunTool"];
  onDeleteJob: AssetLibraryProps["onDeleteJob"];
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
  const canDeleteJob = Boolean(
    job && !asset.linked && job.status !== "queued" && job.status !== "running",
  );

  return (
    <div className={`assetRow ${selected ? "selected" : ""}`}>
      <div className="assetRowHead">
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
        {size && <span>{size}</span>}
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
        {job && canDeleteJob && (
          <button
            className="danger miniButton"
            title="Delete this job and its output files"
            onClick={() => onDeleteJob(job)}
          >
            <Trash2 size={12} />
            Delete job
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

function AssetTreeChildren({
  parentId,
  childrenByParent,
  jobsById,
  specs,
  demJobs,
  selectedAssetId,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
}: {
  parentId: string;
  childrenByParent: Map<string, Asset[]>;
  jobsById: Map<string, Job>;
  specs: JobSpec[];
  demJobs: Job[];
  selectedAssetId?: string;
  onSelectAsset: (assetId: string) => void;
  onRunTool: AssetLibraryProps["onRunTool"];
  onDeleteJob: AssetLibraryProps["onDeleteJob"];
}) {
  const groups = groupAssets(childrenByParent.get(parentId) ?? []);
  if (groups.length === 0) return null;
  return (
    <div className="assetTreeChildren">
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
                  selected={asset.id === selectedAssetId}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
                />
                <AssetTreeChildren
                  parentId={asset.id}
                  childrenByParent={childrenByParent}
                  jobsById={jobsById}
                  specs={specs}
                  demJobs={demJobs}
                  selectedAssetId={selectedAssetId}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
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
  selectedAssetId,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
}: AssetLibraryProps) {
  const [search, setSearch] = useState("");
  const assetsQuery = useQuery({
    queryKey: ["project-assets", projectId],
    queryFn: () => listProjectAssets(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: 2000,
  });
  const pipelineQuery = useQuery({
    queryKey: ["project-pipeline", projectId],
    queryFn: () => getProjectPipeline(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: 2000,
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
    const edges = pipelineQuery.data?.edges ?? [];
    const visibleIds = expandMatches(matchingAssetIds(assets, jobsById, search), edges);
    return buildTree(
      assets.filter((asset) => visibleIds.has(asset.id)),
      edges,
    );
  }, [assetsQuery.data, jobsById, pipelineQuery.data, search]);
  const demRoots = tree.roots.filter((asset) => asset.kind === "raster.dem");
  const otherRoots = tree.roots.filter((asset) => asset.kind !== "raster.dem");

  return (
    <aside className="panel assetLibrary">
      <div className="panelHeader">
        <Tags size={16} />
        <h1>Asset Library</h1>
      </div>
      <label className="assetSearch">
        <Search size={14} />
        <input
          placeholder="Search assets"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </label>
      {!projectId && <div className="empty">Choose a project to show assets.</div>}
      {assetsQuery.error && <pre className="error">{String(assetsQuery.error)}</pre>}
      {projectId && assetsQuery.isLoading && <div className="empty">Loading assets...</div>}
      {pipelineQuery.error && <pre className="error">{String(pipelineQuery.error)}</pre>}
      {projectId && !assetsQuery.isLoading && tree.roots.length === 0 && (
        <div className="empty">No matching assets in this project.</div>
      )}
      <div className="assetGroups">
        {demRoots.map((asset) => (
          <details key={`aoi:${asset.id}`} className="assetGroup" open>
            <summary>
              <span>AOI Bounds</span>
              <span className="assetCount">1</span>
            </summary>
            <div className="assetRows">
              <div className="assetTreeNode">
                <AssetRow
                  asset={asset}
                  job={asset.job_id ? jobsById.get(asset.job_id) : undefined}
                  specs={specs}
                  demJobs={demJobs}
                  selected={asset.id === selectedAssetId}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
                />
                <AssetTreeChildren
                  parentId={asset.id}
                  childrenByParent={tree.childrenByParent}
                  jobsById={jobsById}
                  specs={specs}
                  demJobs={demJobs}
                  selectedAssetId={selectedAssetId}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                  onDeleteJob={onDeleteJob}
                />
              </div>
            </div>
          </details>
        ))}
        {groupAssets(otherRoots).map((group) => (
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
                    selected={asset.id === selectedAssetId}
                    onSelectAsset={onSelectAsset}
                    onRunTool={onRunTool}
                    onDeleteJob={onDeleteJob}
                  />
                  <AssetTreeChildren
                    parentId={asset.id}
                    childrenByParent={tree.childrenByParent}
                    jobsById={jobsById}
                    specs={specs}
                    demJobs={demJobs}
                    selectedAssetId={selectedAssetId}
                    onSelectAsset={onSelectAsset}
                    onRunTool={onRunTool}
                    onDeleteJob={onDeleteJob}
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
