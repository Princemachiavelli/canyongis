import { useEffect, useMemo, useRef, useState } from "react";
import { Play, X } from "lucide-react";
import type { Job, JobSpec } from "./api";

type JsonSchema = {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  default?: unknown;
  enum?: unknown[];
  items?: JsonSchema;
  prefixItems?: JsonSchema[];
  anyOf?: JsonSchema[];
  title?: string;
  description?: string;
};

function nonNullVariant(schema: JsonSchema): JsonSchema {
  if (schema.anyOf) {
    const variant = schema.anyOf.find((s) => s.type !== "null");
    if (variant) return { ...variant, default: schema.default };
  }
  return schema;
}

function resolveType(schema: JsonSchema): string | undefined {
  const inner = nonNullVariant(schema);
  if (Array.isArray(inner.type)) return inner.type.find((t) => t !== "null");
  if (typeof inner.type === "string") return inner.type;
  if (inner.enum && inner.enum.length > 0) {
    return typeof inner.enum[0] === "number" ? "number" : "string";
  }
  return undefined;
}

function isNullable(schema: JsonSchema): boolean {
  if (Array.isArray(schema.type)) return schema.type.includes("null");
  if (schema.anyOf) return schema.anyOf.some((s) => s.type === "null");
  return false;
}

function resolveEnum(schema: JsonSchema): unknown[] | undefined {
  const inner = nonNullVariant(schema);
  if (inner.enum) return inner.enum;
  return undefined;
}

function resolveItems(schema: JsonSchema): JsonSchema | undefined {
  const inner = nonNullVariant(schema);
  if (inner.items) return inner.items;
  if (inner.prefixItems && inner.prefixItems.length > 0) return inner.prefixItems[0];
  return undefined;
}

function isJobIdField(name: string): boolean {
  return name.endsWith("_job_id");
}

function toCsv(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value.map((v) => String(v)).join(", ");
}

function parseCsv(raw: string, itemType?: string): unknown[] {
  const parts = raw
    .split(",")
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
  if (itemType === "number" || itemType === "integer") {
    const nums: number[] = [];
    for (const p of parts) {
      const n = Number(p);
      if (Number.isFinite(n)) nums.push(itemType === "integer" ? Math.trunc(n) : n);
    }
    return nums;
  }
  return parts;
}

function jobOutputs(job: Job): Array<{ role?: string; asset_kind?: string }> {
  const outs = job.result?.outputs;
  return Array.isArray(outs) ? (outs as Array<{ role?: string; asset_kind?: string }>) : [];
}

function jobDemAncestor(job: Job): string | undefined {
  const params = job.params as { dem_job_id?: string };
  if (typeof params.dem_job_id === "string" && params.dem_job_id) return params.dem_job_id;
  const result = job.result as { dem_job_id?: string } | undefined;
  if (result && typeof result.dem_job_id === "string" && result.dem_job_id)
    return result.dem_job_id;
  return undefined;
}

function jobMatchesKind(job: Job, kind: string): boolean {
  if (job.status !== "succeeded") return false;
  const outs = jobOutputs(job);
  if (outs.length > 0) {
    if (kind === "raster.dem")
      return outs.some((o) => o.role === "dem" && o.asset_kind === "raster");
    if (kind === "raster.score")
      return outs.some((o) => o.role === "ravine-score" && o.asset_kind === "raster");
    if (kind === "vector.kml")
      return outs.some((o) => o.role === "route" && o.asset_kind === "vector");
  }
  if (kind === "raster.dem")
    return job.kind === "build-dem" && typeof job.result?.dem_path === "string";
  if (kind === "raster.score")
    return (
      typeof job.result?.b_tpi_path === "string" ||
      typeof job.result?.a_geomorphon_path === "string" ||
      typeof job.result?.c_curvature_path === "string"
    );
  if (kind === "vector.kml")
    return (
      (job.kind === "kml-import" || job.kind === "kml-snap") &&
      typeof job.result?.kml_path === "string"
    );
  return false;
}

function jobLabel(job: Job): string {
  const name =
    (job.result?.name as string | undefined) ?? (job.params.name as string | undefined);
  return name ? `${name} (${job.id})` : `${job.kind} ${job.id}`;
}

export type ToolDialogInputDefaults = Record<string, string | undefined>;

export type ToolDialogResult = {
  inputs: Record<string, string>;
  params: Record<string, unknown>;
};

function NumberField({
  name,
  schema,
  value,
  onChange,
  nullable,
}: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
  nullable: boolean;
}) {
  const isInt = resolveType(schema) === "integer";
  const empty = value === undefined || value === null || value === "";
  return (
    <label className="toolField">
      <span>{schema.title ?? name}</span>
      <input
        type="number"
        step={isInt ? 1 : "any"}
        value={empty ? "" : String(value)}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") {
            onChange(nullable ? null : undefined);
            return;
          }
          const n = Number(raw);
          if (Number.isFinite(n)) onChange(isInt ? Math.trunc(n) : n);
        }}
      />
    </label>
  );
}

function StringField({
  name,
  schema,
  value,
  onChange,
}: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  return (
    <label className="toolField">
      <span>{schema.title ?? name}</span>
      <input
        type="text"
        value={value == null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function EnumField({
  name,
  schema,
  value,
  onChange,
  options,
}: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
  options: unknown[];
}) {
  return (
    <label className="toolField">
      <span>{schema.title ?? name}</span>
      <select
        value={value == null ? "" : String(value)}
        onChange={(e) => {
          const raw = e.target.value;
          const match = options.find((o) => String(o) === raw);
          onChange(match);
        }}
      >
        {options.map((opt) => (
          <option key={String(opt)} value={String(opt)}>
            {String(opt)}
          </option>
        ))}
      </select>
    </label>
  );
}

function BooleanField({
  name,
  schema,
  value,
  onChange,
}: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  return (
    <label className="toolField toolFieldCheckbox">
      <input
        type="checkbox"
        checked={Boolean(value)}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{schema.title ?? name}</span>
    </label>
  );
}

function ArrayField({
  name,
  schema,
  value,
  onChange,
}: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const items = resolveItems(schema);
  const itemType = items ? resolveType(items) : undefined;
  const display = toCsv(value);
  const [text, setText] = useState(display);
  useEffect(() => {
    setText(toCsv(value));
  }, [value]);
  return (
    <label className="toolField">
      <span>
        {schema.title ?? name}
        <span className="toolFieldHint"> (comma-separated)</span>
      </span>
      <input
        type="text"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          onChange(parseCsv(e.target.value, itemType));
        }}
      />
    </label>
  );
}

function SchemaField({
  name,
  schema,
  value,
  onChange,
}: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const enumOptions = resolveEnum(schema);
  if (enumOptions && enumOptions.length > 0) {
    return (
      <EnumField name={name} schema={schema} value={value} onChange={onChange} options={enumOptions} />
    );
  }
  const t = resolveType(schema);
  if (t === "boolean") return <BooleanField name={name} schema={schema} value={value} onChange={onChange} />;
  if (t === "number" || t === "integer")
    return (
      <NumberField
        name={name}
        schema={schema}
        value={value}
        onChange={onChange}
        nullable={isNullable(schema)}
      />
    );
  if (t === "array") return <ArrayField name={name} schema={schema} value={value} onChange={onChange} />;
  return <StringField name={name} schema={schema} value={value} onChange={onChange} />;
}

type ToolFormProps = {
  spec: JobSpec;
  jobs: Job[];
  initialInputs?: ToolDialogInputDefaults;
  initialParams?: Record<string, unknown>;
  onCancel?: () => void;
  onRun: (payload: ToolDialogResult) => void;
  isPending?: boolean;
  title?: string;
  showClose?: boolean;
};

export function ToolForm({
  spec,
  jobs,
  initialInputs,
  initialParams,
  onCancel,
  onRun,
  isPending,
  title,
  showClose = true,
}: ToolFormProps) {
  const schema = (spec.request_schema ?? {}) as JsonSchema;
  const props = (schema.properties ?? {}) as Record<string, JsonSchema>;
  const defaults = (spec.request_defaults ?? {}) as Record<string, unknown>;

  const paramKeys = useMemo(
    () => Object.keys(props).filter((k) => !isJobIdField(k)),
    [props],
  );

  const jobInputs = useMemo(
    () => spec.inputs.filter((inp) => inp.kind.startsWith("raster.") || inp.kind.startsWith("vector.")),
    [spec.inputs],
  );

  const demInputKey = useMemo(
    () => jobInputs.find((i) => i.kind === "raster.dem")?.key,
    [jobInputs],
  );

  const [paramState, setParamState] = useState<Record<string, unknown>>({});
  const [inputState, setInputState] = useState<Record<string, string>>({});

  const selectedDem = demInputKey ? inputState[demInputKey] : undefined;

  const compatibleJobsByKey = useMemo(() => {
    const map = new Map<string, Job[]>();
    for (const inp of jobInputs) {
      let matching = jobs.filter((j) => jobMatchesKind(j, inp.kind));
      if (inp.kind === "raster.score" && demInputKey && selectedDem) {
        matching = matching.filter((j) => jobDemAncestor(j) === selectedDem);
      }
      matching.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      map.set(inp.key, matching);
    }
    return map;
  }, [jobInputs, jobs, demInputKey, selectedDem]);

  useEffect(() => {
    const initParams: Record<string, unknown> = {};
    for (const k of paramKeys) {
      if (initialParams && k in initialParams) {
        initParams[k] = initialParams[k];
      } else if (k in defaults) {
        initParams[k] = defaults[k];
      } else if (props[k]?.default !== undefined) {
        initParams[k] = props[k].default;
      }
    }
    setParamState(initParams);

    const initInputs: Record<string, string> = {};
    let initialDem: string | undefined;
    for (const inp of jobInputs) {
      if (inp.kind !== "raster.dem") continue;
      const all = jobs
        .filter((j) => jobMatchesKind(j, inp.kind))
        .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      const preset = initialInputs?.[inp.key];
      const chosen =
        preset && all.find((j) => j.id === preset)
          ? preset
          : (all[0]?.id ?? "");
      initInputs[inp.key] = chosen;
      initialDem = chosen || undefined;
    }
    for (const inp of jobInputs) {
      if (inp.kind === "raster.dem") continue;
      let matching = jobs.filter((j) => jobMatchesKind(j, inp.kind));
      if (inp.kind === "raster.score" && demInputKey && initialDem) {
        matching = matching.filter((j) => jobDemAncestor(j) === initialDem);
      }
      matching.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      const preset = initialInputs?.[inp.key];
      initInputs[inp.key] =
        preset && matching.find((j) => j.id === preset)
          ? preset
          : (matching[0]?.id ?? "");
    }
    setInputState(initInputs);
  }, [spec.key, initialInputs, initialParams]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!demInputKey) return;
    setInputState((prev) => {
      let next = prev;
      for (const inp of jobInputs) {
        if (inp.kind !== "raster.score") continue;
        const current = prev[inp.key];
        if (!current) continue;
        const compat = (compatibleJobsByKey.get(inp.key) ?? []).map((j) => j.id);
        if (compat.includes(current)) continue;
        if (next === prev) next = { ...prev };
        next[inp.key] = compat[0] ?? "";
      }
      return next;
    });
  }, [selectedDem, demInputKey, jobInputs, compatibleJobsByKey]);

  const missingRequired = jobInputs.find(
    (inp) => inp.required && !inputState[inp.key],
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (missingRequired) return;
    const inputs: Record<string, string> = {};
    for (const [k, v] of Object.entries(inputState)) {
      if (v) inputs[k] = v;
    }
    const params: Record<string, unknown> = {};
    for (const k of paramKeys) {
      const v = paramState[k];
      if (v === undefined) continue;
      params[k] = v;
    }
    onRun({ inputs, params });
  };

  return (
      <form className="toolForm" onSubmit={handleSubmit}>
        <div className="dialogHead">
          <Play size={16} />
          <strong>{title ?? spec.label}</strong>
          {showClose && onCancel && (
            <button
              type="button"
              className="iconButton"
              aria-label="Close"
              style={{ marginLeft: "auto" }}
              onClick={onCancel}
            >
              <X size={14} />
            </button>
          )}
        </div>
        {spec.description && <div className="toolDescription">{spec.description}</div>}

        {jobInputs.length > 0 && (
          <div className="toolInputs">
            {jobInputs.map((inp) => {
              const options = compatibleJobsByKey.get(inp.key) ?? [];
              const value = inputState[inp.key] ?? "";
              return (
                <label key={inp.key} className="toolField">
                  <span>
                    {inp.label}
                    {!inp.required && <span className="toolFieldHint"> (optional)</span>}
                  </span>
                  <select
                    value={value}
                    onChange={(e) =>
                      setInputState((prev) => ({ ...prev, [inp.key]: e.target.value }))
                    }
                  >
                    {!inp.required && <option value="">(none)</option>}
                    {inp.required && options.length === 0 && (
                      <option value="">No compatible job</option>
                    )}
                    {options.map((job) => (
                      <option key={job.id} value={job.id}>
                        {jobLabel(job)}
                      </option>
                    ))}
                  </select>
                </label>
              );
            })}
          </div>
        )}

        {paramKeys.length > 0 && (
          <div className="toolParams">
            {paramKeys.map((k) => (
              <SchemaField
                key={k}
                name={k}
                schema={props[k]}
                value={paramState[k]}
                onChange={(v) => setParamState((prev) => ({ ...prev, [k]: v }))}
              />
            ))}
          </div>
        )}

        {missingRequired && (
          <div className="toolWarning">
            Required input "{missingRequired.label}" has no compatible job.
          </div>
        )}

        <div className="dialogActions">
          <button type="button" onClick={onCancel} disabled={!onCancel}>
            Cancel
          </button>
          <button
            type="submit"
            className="primary"
            disabled={Boolean(missingRequired) || isPending}
          >
            {isPending ? "Running..." : "Run"}
          </button>
        </div>
      </form>
  );
}

export function ToolDialog({
  open,
  onClose,
  ...props
}: ToolFormProps & { open: boolean; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  if (!open) return null;

  return (
    <dialog ref={dialogRef} className="appDialog toolDialog" onClose={onClose}>
      <ToolForm {...props} onCancel={onClose} />
    </dialog>
  );
}
