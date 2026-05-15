import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Eye, LocateFixed, Play, Search, Tags } from "lucide-react";
import {
  listProjectAssets,
  type Asset,
  type Job,
  type JobSpec,
} from "./api";
import { useAppStore } from "./state";
import { descriptorsFromJob } from "./layers";
import {
  ASSET_GROUP_ORDER,
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
};

type AssetGroup = {
  key: string;
  label: string;
  assets: Asset[];
};

function formatBytes(value: number | null): string | undefined {
  if (value == null) return undefined;
  if (value < 1024) return `${value} B`;
  const mb = value / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function AssetRow({
  asset,
  job,
  specs,
  selected,
  onSelectAsset,
  onRunTool,
}: {
  asset: Asset;
  job?: Job;
  specs: JobSpec[];
  selected: boolean;
  onSelectAsset: (assetId: string) => void;
  onRunTool: AssetLibraryProps["onRunTool"];
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
  const descriptors = useMemo(() => (job ? descriptorsFromJob(job) : []), [job]);
  const tools = useMemo(() => compatibleSpecs(asset, specs), [asset, specs]);
  const name = assetName(asset, job);
  const size = formatBytes(asset.size_bytes);

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
                Overlay
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
      </div>
      {tools.length > 0 && asset.job_id && (
        <div className="assetTools">
          {tools.slice(0, 3).map(({ spec, inputKey }) => (
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

export function AssetLibrary({
  projectId,
  jobs,
  specs,
  selectedAssetId,
  onSelectAsset,
  onRunTool,
}: AssetLibraryProps) {
  const [search, setSearch] = useState("");
  const assetsQuery = useQuery({
    queryKey: ["project-assets", projectId],
    queryFn: () => listProjectAssets(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: 2000,
  });

  const jobsById = useMemo(() => new Map(jobs.map((job) => [job.id, job])), [jobs]);
  const groups = useMemo<AssetGroup[]>(() => {
    const q = search.trim().toLowerCase();
    const byKey = new Map<string, AssetGroup>();
    for (const asset of assetsQuery.data ?? []) {
      const job = asset.job_id ? jobsById.get(asset.job_id) : undefined;
      const name = assetName(asset, job);
      const haystack = `${name} ${asset.kind} ${asset.id} ${asset.path ?? ""}`.toLowerCase();
      if (q && !haystack.includes(q)) continue;
      const groupInfo = assetGroup(asset.kind);
      const group = byKey.get(groupInfo.key) ?? { ...groupInfo, assets: [] };
      group.assets.push(asset);
      byKey.set(group.key, group);
    }
    return [...byKey.values()].sort(
      (a, b) => ASSET_GROUP_ORDER.indexOf(a.key) - ASSET_GROUP_ORDER.indexOf(b.key),
    );
  }, [assetsQuery.data, jobsById, search]);

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
      {projectId && !assetsQuery.isLoading && groups.length === 0 && (
        <div className="empty">No matching assets in this project.</div>
      )}
      <div className="assetGroups">
        {groups.map((group) => (
          <details key={group.key} className="assetGroup" open>
            <summary>
              <span>{group.label}</span>
              <span className="assetCount">{group.assets.length}</span>
            </summary>
            <div className="assetRows">
              {group.assets.map((asset) => (
                <AssetRow
                  key={asset.id}
                  asset={asset}
                  job={asset.job_id ? jobsById.get(asset.job_id) : undefined}
                  specs={specs}
                  selected={asset.id === selectedAssetId}
                  onSelectAsset={onSelectAsset}
                  onRunTool={onRunTool}
                />
              ))}
            </div>
          </details>
        ))}
      </div>
    </aside>
  );
}
