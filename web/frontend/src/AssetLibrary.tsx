import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Eye, LocateFixed, Play, Search, Tags, Trash2 } from "lucide-react";
import {
  getProjectPipeline,
  listProjectAssets,
  type Asset,
  type AssetLineageEdge,
  type Job,
  type JobSpec,
} from "./api";
import { useAppStore } from "./state";
import { descriptorsFromJob, type LayerDescriptor } from "./layers";
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

function isPreviewAssetKind(kind: string): boolean {
  return (
    kind.startsWith("preview.") ||
    kind === "pmtiles" ||
    kind === "preview_png" ||
    kind === "preview_webp" ||
    kind === "preview_jpg"
  );
}

function isPreviewAsset(asset: Asset): boolean {
  return isPreviewAssetKind(asset.kind) || typeof asset.metadata.layer_key === "string";
}

function canonicalAssetForJob(assets: Asset[], jobId: string): Asset | undefined {
  return assets
    .filter((asset) => asset.job_id === jobId && !isPreviewAsset(asset))
    .sort(assetSort)[0];
}

function layerKey(asset: Asset): string | undefined {
  const value = asset.metadata.layer_key;
  return typeof value === "string" && value ? value : undefined;
}

function assetLayerSourceType(asset: Asset): "image" | "pmtiles" | undefined {
  if (asset.kind === "pmtiles" || asset.media_type === "application/vnd.pmtiles") return "pmtiles";
  if (asset.kind.startsWith("preview.") && asset.format === "pmtiles") return "pmtiles";
  if (asset.media_type?.startsWith("image/")) return "image";
  if (asset.kind === "preview_png" || asset.kind === "preview_webp" || asset.kind === "preview_jpg") {
    return "image";
  }
  return undefined;
}

function synthesizedLayer(asset: Asset, name: string): LayerDescriptor | undefined {
  const sourceType = assetLayerSourceType(asset);
  if (!sourceType || !asset.url) return undefined;
  const bounds =
    asset.bounds_wgs84 && asset.bounds_wgs84.length === 4
      ? ([
          asset.bounds_wgs84[0],
          asset.bounds_wgs84[1],
          asset.bounds_wgs84[2],
          asset.bounds_wgs84[3],
        ] as [number, number, number, number])
      : undefined;
  const role = typeof asset.metadata.role === "string" ? asset.metadata.role : undefined;
  const source: LayerDescriptor["source"] = { type: sourceType, url: asset.url };
  if (asset.path) source.path = asset.path;
  return {
    id: `asset:${asset.id}`,
    label: name,
    kind: role === "base" ? "base-raster" : "raster-overlay",
    bounds_wgs84: bounds,
    role,
    source,
  };
}

function descriptorsForAsset(asset: Asset, job: Job | undefined, name: string): LayerDescriptor[] {
  const descriptors = job ? descriptorsFromJob(job) : [];
  if (isPreviewAsset(asset)) {
    const key = layerKey(asset);
    const matching = descriptors.filter((descriptor) => {
      if (key && descriptor.id.endsWith(`:${key}`)) return true;
      return Boolean(asset.path && descriptor.source?.path === asset.path);
    });
    if (matching.length > 0) return matching;
    const synthesized = synthesizedLayer(asset, name);
    return synthesized ? [synthesized] : [];
  }
  if (asset.kind.startsWith("raster.")) return [];
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
  const canonicalByJob = new Map<string, Asset>();
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
    const parent = isPreviewAsset(consumer) && canonical ? canonical : producer;
    const children = childrenByParent.get(parent.id) ?? [];
    if (!children.some((asset) => asset.id === consumer.id)) children.push(consumer);
    childrenByParent.set(parent.id, children);
    hasParent.add(consumer.id);
    if (canonical && canonical.id !== consumer.id && !hasParent.has(canonical.id)) {
      const parentChildren = childrenByParent.get(producer.id) ?? [];
      if (!parentChildren.some((asset) => asset.id === canonical.id)) parentChildren.push(canonical);
      childrenByParent.set(producer.id, parentChildren);
      hasParent.add(canonical.id);
    }
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

function AssetRow({
  asset,
  job,
  specs,
  selected,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
}: {
  asset: Asset;
  job?: Job;
  specs: JobSpec[];
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
  selectedAssetId,
  onSelectAsset,
  onRunTool,
  onDeleteJob,
}: {
  parentId: string;
  childrenByParent: Map<string, Asset[]>;
  jobsById: Map<string, Job>;
  specs: JobSpec[];
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
