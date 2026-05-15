import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { GitBranch, X } from "lucide-react";
import { getProjectPipeline, type ProjectPipelineAsset } from "./api";

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

export function PipelineView({ projectId, onClose, onSelectAsset }: PipelineViewProps) {
  const pipelineQuery = useQuery({
    queryKey: ["project-pipeline", projectId],
    queryFn: () => getProjectPipeline(projectId as string),
    enabled: Boolean(projectId),
  });
  const pipeline = pipelineQuery.data;
  const assetsById = useMemo(
    () => new Map((pipeline?.assets ?? []).map((asset) => [asset.id, asset])),
    [pipeline],
  );
  const depths = useMemo(() => {
    const map = new Map<string, number>();
    for (const asset of pipeline?.assets ?? []) map.set(asset.id, 0);
    for (let i = 0; i < (pipeline?.assets.length ?? 0); i += 1) {
      let changed = false;
      for (const edge of pipeline?.edges ?? []) {
        const next = Math.max(map.get(edge.consumer) ?? 0, (map.get(edge.producer) ?? 0) + 1);
        if (next !== map.get(edge.consumer)) {
          map.set(edge.consumer, next);
          changed = true;
        }
      }
      if (!changed) break;
    }
    return map;
  }, [pipeline]);
  const maxDepth = Math.max(0, ...depths.values());

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
        <div
          className="pipelineCanvas"
          style={{ gridTemplateColumns: `repeat(${maxDepth + 1}, minmax(220px, 1fr))` }}
        >
          {pipeline.assets.map((asset) => {
            const incoming = pipeline.edges.filter((edge) => edge.consumer === asset.id);
            return (
              <button
                key={asset.id}
                className="pipelineNode"
                style={{ gridColumn: (depths.get(asset.id) ?? 0) + 1 }}
                onClick={() => onSelectAsset(asset.id)}
              >
                <strong>{assetLabel(asset)}</strong>
                <span>{asset.kind}</span>
                <code>{asset.id}</code>
                {incoming.length > 0 && (
                  <div className="pipelineInputs">
                    {incoming.map((edge) => {
                      const producer = assetsById.get(edge.producer);
                      return (
                        <span key={`${edge.input_key}:${edge.producer}`}>
                          {edge.input_key} from {producer ? assetLabel(producer) : edge.producer}
                        </span>
                      );
                    })}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
