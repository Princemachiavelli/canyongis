import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { GitBranch, X } from "lucide-react";
import { getProjectPipeline, type AssetLineageEdge, type ProjectPipelineAsset } from "./api";

type PipelineViewProps = {
  projectId?: string;
  onClose: () => void;
  onSelectAsset: (assetId: string) => void;
};

function assetLabel(asset: ProjectPipelineAsset): string {
  const label = asset.metadata.label;
  if (typeof label === "string" && label) return label;
  if (asset.path) return asset.path.split("/").pop() ?? asset.id;
  return asset.id;
}

type PipelineTreeData = {
  roots: ProjectPipelineAsset[];
  childrenByParent: Map<string, ProjectPipelineAsset[]>;
};

type PipelineGroup = {
  key: string;
  label: string;
  assets: ProjectPipelineAsset[];
};

function assetGroup(kind: string): { key: string; label: string } {
  if (kind === "raster.dem") return { key: "dem", label: "DEMs" };
  if (kind === "raster.hillshade") return { key: "hillshade", label: "Hillshades" };
  if (kind.startsWith("raster.score")) return { key: "score", label: "Score Rasters" };
  if (kind.startsWith("vector.kml.route")) return { key: "route", label: "Routes" };
  if (kind.startsWith("vector.kml.spine")) return { key: "spine", label: "Spines" };
  if (isPreviewAssetKind(kind)) return { key: "preview", label: "Previews" };
  if (kind.includes("rappel") || kind.startsWith("vector.geojson.points")) {
    return { key: "event", label: "Events" };
  }
  if (kind.startsWith("raster.")) return { key: "raster", label: "Rasters" };
  if (kind.startsWith("vector.")) return { key: "vector", label: "Vectors" };
  return { key: "other", label: "Other" };
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

function isPreviewAsset(asset: ProjectPipelineAsset): boolean {
  return isPreviewAssetKind(asset.kind) || typeof asset.metadata.layer_key === "string";
}

function canonicalAssetForJob(
  assets: ProjectPipelineAsset[],
  jobId: string,
): ProjectPipelineAsset | undefined {
  return assets
    .filter((asset) => asset.job_id === jobId && !isPreviewAsset(asset))
    .sort(assetSort)[0];
}

function assetSort(a: ProjectPipelineAsset, b: ProjectPipelineAsset): number {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
  return a.id.localeCompare(b.id);
}

function buildTree(
  assets: ProjectPipelineAsset[],
  edges: AssetLineageEdge[],
): PipelineTreeData {
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  const childrenByParent = new Map<string, ProjectPipelineAsset[]>();
  const hasParent = new Set<string>();
  const canonicalByJob = new Map<string, ProjectPipelineAsset>();
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

function groupAssets(assets: ProjectPipelineAsset[]): PipelineGroup[] {
  const byKey = new Map<string, PipelineGroup>();
  for (const asset of assets) {
    const groupInfo = assetGroup(asset.kind);
    const group = byKey.get(groupInfo.key) ?? { ...groupInfo, assets: [] };
    group.assets.push(asset);
    byKey.set(group.key, group);
  }
  for (const group of byKey.values()) group.assets.sort(assetSort);
  return [...byKey.values()].sort((a, b) => a.label.localeCompare(b.label));
}

function PipelineNode({
  asset,
  childrenByParent,
  onSelectAsset,
}: {
  asset: ProjectPipelineAsset;
  childrenByParent: Map<string, ProjectPipelineAsset[]>;
  onSelectAsset: (assetId: string) => void;
}) {
  const groups = groupAssets(childrenByParent.get(asset.id) ?? []);
  return (
    <div className="pipelineTreeNode">
      <button className="pipelineNode" onClick={() => onSelectAsset(asset.id)}>
        <strong>{assetLabel(asset)}</strong>
        <span>{asset.kind}</span>
        <code>{asset.id}</code>
      </button>
      {groups.length > 0 && (
        <div className="pipelineChildren">
          {groups.map((group) => (
            <details key={`${asset.id}:${group.key}`} className="pipelineTypeGroup">
              <summary>
                <span>{group.label}</span>
                <span className="assetCount">{group.assets.length}</span>
              </summary>
              <div className="pipelineGroupRows">
                {group.assets.map((child) => (
                  <PipelineNode
                    key={child.id}
                    asset={child}
                    childrenByParent={childrenByParent}
                    onSelectAsset={onSelectAsset}
                  />
                ))}
              </div>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

export function PipelineView({ projectId, onClose, onSelectAsset }: PipelineViewProps) {
  const pipelineQuery = useQuery({
    queryKey: ["project-pipeline", projectId],
    queryFn: () => getProjectPipeline(projectId as string),
    enabled: Boolean(projectId),
  });
  const pipeline = pipelineQuery.data;
  const tree = useMemo(
    () => buildTree(pipeline?.assets ?? [], pipeline?.edges ?? []),
    [pipeline],
  );
  const demRoots = tree.roots.filter((asset) => asset.kind === "raster.dem");
  const otherRootGroups = groupAssets(tree.roots.filter((asset) => asset.kind !== "raster.dem"));

  return (
    <div className="pipelineOverlay">
      <div className="pipelineHead">
        <div>
          <GitBranch size={18} />
          <strong>Project Pipeline</strong>
          {pipeline && <span>{pipeline.assets.length} assets</span>}
        </div>
        <button className="iconButton" onClick={onClose} aria-label="Close pipeline">
          <X size={16} />
        </button>
      </div>
      {pipelineQuery.isLoading && <div className="empty">Loading pipeline...</div>}
      {pipelineQuery.error && <pre className="error">{String(pipelineQuery.error)}</pre>}
      {pipeline && pipeline.assets.length === 0 && <div className="empty">No assets in this project.</div>}
      {pipeline && pipeline.assets.length > 0 && (
        <div className="pipelineCanvas">
          {demRoots.map((asset) => (
            <details key={`aoi:${asset.id}`} className="pipelineRoot" open>
              <summary>
                <span>AOI Bounds</span>
                <span className="assetCount">1</span>
              </summary>
              <PipelineNode
                asset={asset}
                childrenByParent={tree.childrenByParent}
                onSelectAsset={onSelectAsset}
              />
            </details>
          ))}
          {otherRootGroups.map((group) => (
            <details key={`root:${group.key}`} className="pipelineRoot" open>
              <summary>
                <span>{group.label}</span>
                <span className="assetCount">{group.assets.length}</span>
              </summary>
              <div className="pipelineGroupRows">
                {group.assets.map((asset) => (
                  <PipelineNode
                    key={asset.id}
                    asset={asset}
                    childrenByParent={tree.childrenByParent}
                    onSelectAsset={onSelectAsset}
                  />
                ))}
              </div>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
