import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { GitBranch, Play, X } from "lucide-react";
import {
  getAssetLineage,
  listProjectAssets,
  type Asset,
  type AssetLineageNode,
  type Job,
  type JobSpec,
} from "./api";
import { assetName, compatibleSpecs } from "./assetUtils";
import { ToolForm, type ToolDialogResult } from "./ToolDialog";

export type InspectorTool = {
  spec: JobSpec;
  initialInputs?: Record<string, string | undefined>;
  initialParams?: Record<string, unknown>;
  extraParams?: Record<string, unknown>;
};

type InspectorProps = {
  projectId?: string;
  selectedAssetId?: string;
  tool?: InspectorTool;
  jobs: Job[];
  specs: JobSpec[];
  isRunningTool?: boolean;
  onSelectAsset: (assetId: string) => void;
  onClearSelection: () => void;
  onOpenTool: (spec: JobSpec, initialInputs: Record<string, string | undefined>) => void;
  onCloseTool: () => void;
  onRunTool: (tool: InspectorTool, result: ToolDialogResult) => void;
};

function nodeLabel(node: AssetLineageNode): string {
  if (node.path) return node.path.split("/").pop() ?? node.id;
  return node.id;
}

function ParamsBlock({ job }: { job?: Job }) {
  if (!job) return <div className="empty">No producing job metadata.</div>;
  return (
    <pre className="inspectorJson">
      {JSON.stringify(
        {
          id: job.id,
          kind: job.kind,
          status: job.status,
          params: job.params,
        },
        null,
        2,
      )}
    </pre>
  );
}

function LineageView({ assetId }: { assetId: string }) {
  const lineageQuery = useQuery({
    queryKey: ["asset-lineage", assetId],
    queryFn: () => getAssetLineage(assetId),
  });
  const lineage = lineageQuery.data;
  if (lineageQuery.isLoading) return <div className="empty">Loading lineage...</div>;
  if (lineageQuery.error) return <pre className="error">{String(lineageQuery.error)}</pre>;
  if (!lineage || lineage.edges.length === 0) {
    return <div className="empty">No recorded lineage edges.</div>;
  }
  return (
    <div className="lineageList">
      {lineage.edges.map((edge) => {
        const producer = lineage.nodes[edge.producer];
        const consumer = lineage.nodes[edge.consumer];
        return (
          <div key={`${edge.consumer}:${edge.input_key}:${edge.producer}`} className="lineageEdge">
            <GitBranch size={13} />
            <span>{nodeLabel(producer)}</span>
            <code>{edge.input_key}</code>
            <span>{nodeLabel(consumer)}</span>
          </div>
        );
      })}
    </div>
  );
}

function AssetInspector({
  asset,
  job,
  specs,
  onOpenTool,
}: {
  asset: Asset;
  job?: Job;
  specs: JobSpec[];
  onOpenTool: InspectorProps["onOpenTool"];
}) {
  const tools = useMemo(() => compatibleSpecs(asset, specs), [asset, specs]);
  return (
    <div className="inspectorBody">
      <section className="inspectorSection">
        <h2>{assetName(asset, job)}</h2>
        <div className="assetMeta inspectorMeta">
          <span>{asset.kind}</span>
          {asset.linked && <span>linked from {asset.project_name}</span>}
        </div>
        <code className="inspectorId">{asset.id}</code>
      </section>

      <section className="inspectorSection">
        <h2>Lineage</h2>
        <LineageView assetId={asset.id} />
      </section>

      <section className="inspectorSection">
        <h2>Tools</h2>
        {tools.length === 0 || !asset.job_id ? (
          <div className="empty">No compatible tools for this asset.</div>
        ) : (
          <div className="inspectorTools">
            {tools.map(({ spec, inputKey }) => (
              <button
                key={`${spec.key}:${inputKey}`}
                disabled={!spec.available}
                title={spec.unavailable_reason ?? spec.description}
                onClick={() => onOpenTool(spec, { [inputKey]: asset.job_id ?? undefined })}
              >
                <Play size={13} />
                {spec.label}
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="inspectorSection">
        <h2>Producing Job</h2>
        <ParamsBlock job={job} />
      </section>
    </div>
  );
}

export function Inspector({
  projectId,
  selectedAssetId,
  tool,
  jobs,
  specs,
  isRunningTool,
  onSelectAsset,
  onClearSelection,
  onOpenTool,
  onCloseTool,
  onRunTool,
}: InspectorProps) {
  const assetsQuery = useQuery({
    queryKey: ["project-assets", projectId],
    queryFn: () => listProjectAssets(projectId as string),
    enabled: Boolean(projectId),
    refetchInterval: 2000,
  });
  const jobsById = useMemo(() => new Map(jobs.map((job) => [job.id, job])), [jobs]);
  const assets = assetsQuery.data ?? [];
  const selectedAsset = assets.find((asset) => asset.id === selectedAssetId);
  const job = selectedAsset?.job_id ? jobsById.get(selectedAsset.job_id) : undefined;

  return (
    <aside className="inspectorPanel">
      <div className="inspectorHead">
        <div>
          <span>Inspector</span>
          <strong>{tool ? "Tool" : selectedAsset ? "Asset" : "Ready"}</strong>
        </div>
        {(tool || selectedAsset) && (
          <button
            type="button"
            className="iconButton"
            aria-label="Clear inspector"
            onClick={tool ? onCloseTool : onClearSelection}
          >
            <X size={14} />
          </button>
        )}
      </div>

      {tool ? (
        <ToolForm
          spec={tool.spec}
          jobs={jobs}
          initialInputs={tool.initialInputs}
          initialParams={tool.initialParams}
          isPending={isRunningTool}
          onCancel={onCloseTool}
          onRun={(result) => onRunTool(tool, result)}
        />
      ) : selectedAsset ? (
        <AssetInspector asset={selectedAsset} job={job} specs={specs} onOpenTool={onOpenTool} />
      ) : (
        <div className="inspectorEmpty">
          <strong>Select an asset or pick a tool.</strong>
          <span>Asset lineage, job parameters, and compatible tools appear here.</span>
          {assets.slice(0, 5).map((asset) => (
            <button key={asset.id} onClick={() => onSelectAsset(asset.id)}>
              {assetName(asset, asset.job_id ? jobsById.get(asset.job_id) : undefined)}
            </button>
          ))}
        </div>
      )}
    </aside>
  );
}
