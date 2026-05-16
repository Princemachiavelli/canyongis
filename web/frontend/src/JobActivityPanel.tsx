import { Fragment, useMemo } from "react";
import { Activity, Square, Trash2, X } from "lucide-react";
import type { Job } from "./api";

const HIDDEN_PARAM_KEYS = new Set(["_inputs", "job_key", "source_job_id", "layer"]);

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

function StatusBadge({ status }: { status: Job["status"] }) {
  return <span className={`statusBadge ${status}`}>{status}</span>;
}

function JobParamsDetails({ job }: { job: Job }) {
  const inputs = useMemo(() => {
    const raw = (job.params as { _inputs?: unknown })._inputs;
    if (!raw || typeof raw !== "object" || Array.isArray(raw))
      return {} as Record<string, unknown>;
    return raw as Record<string, unknown>;
  }, [job.params]);
  const params = useMemo(
    () =>
      Object.entries(job.params as Record<string, unknown>)
        .filter(([k, v]) => !HIDDEN_PARAM_KEYS.has(k) && v !== null && v !== undefined)
        .sort(([a], [b]) => a.localeCompare(b)),
    [job.params],
  );
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

function jobDisplayName(job: Job): string {
  const name =
    (job.result?.name as string | undefined) ?? (job.params.name as string | undefined);
  return name ? `${name} (${job.kind})` : job.kind;
}

function formatJobTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function JobActivityPanel({
  jobs,
  onClose,
  onDeleteJob,
  onCancelJob,
}: {
  jobs: Job[];
  onClose: () => void;
  onDeleteJob: (job: Job) => void;
  onCancelJob: (job: Job) => void;
}) {
  const sorted = useMemo(
    () => [...jobs].sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
    [jobs],
  );
  const active = sorted.filter((job) => job.status === "queued" || job.status === "running");
  return (
    <aside className="jobActivityPanel">
      <div className="jobActivityHead">
        <div>
          <Activity size={16} />
          <strong>Jobs</strong>
          <span>{active.length} running</span>
        </div>
        <button className="iconButton" onClick={onClose} aria-label="Close jobs">
          <X size={14} />
        </button>
      </div>

      <section className="jobActivitySection">
        <h2>Running</h2>
        {active.length === 0 && <div className="empty">No queued or running jobs.</div>}
        {active.map((job) => (
          <div key={job.id} className={`jobActivityRow ${job.status}`}>
            <div className="jobActivityTitle">
              <strong>{jobDisplayName(job)}</strong>
              <StatusBadge status={job.status} />
            </div>
            <div className="jobActivityMeta">
              <code>{job.id}</code>
              <span>{formatJobTime(job.created_at)}</span>
            </div>
            <JobParamsDetails job={job} />
            <ProgressTail job={job} onCancel={onCancelJob} />
          </div>
        ))}
      </section>

      <section className="jobActivitySection">
        <h2>History</h2>
        {sorted.length === 0 && <div className="empty">No jobs yet.</div>}
        {sorted.map((job) => (
          <div key={job.id} className={`jobActivityRow ${job.status}`}>
            <div className="jobActivityTitle">
              <strong>{jobDisplayName(job)}</strong>
              <StatusBadge status={job.status} />
            </div>
            <div className="jobActivityMeta">
              <code>{job.id}</code>
              <span>{formatJobTime(job.created_at)}</span>
            </div>
            <JobParamsDetails job={job} />
            <ProgressTail job={job} onDelete={onDeleteJob} onCancel={onCancelJob} />
          </div>
        ))}
      </section>
    </aside>
  );
}
