// Schema-driven tool forms, ported from web/frontend/src/ToolDialog.tsx to
// plain DOM. The same JSON-schema field resolution and job-input compatibility
// rules apply; rendering is imperative instead of React.

import type { Job, JobSpec } from "./api";

export type JsonSchema = {
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
  return nonNullVariant(schema).enum;
}

function resolveItems(schema: JsonSchema): JsonSchema | undefined {
  const inner = nonNullVariant(schema);
  if (inner.items) return inner.items;
  if (inner.prefixItems && inner.prefixItems.length > 0) return inner.prefixItems[0];
  return undefined;
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

function isJobIdField(name: string): boolean {
  return name.endsWith("_job_id");
}

function isHiddenParam(spec: JobSpec, name: string): boolean {
  return (
    isJobIdField(name) ||
    (spec.key === "track-profile" && (name === "category" || name === "track_index"))
  );
}

// --- job ↔ input-kind compatibility (port of ToolDialog jobMatchesKind) ---

function jobOutputs(job: Job): Array<{ role?: string; asset_kind?: string }> {
  const outs = job.result?.outputs;
  return Array.isArray(outs) ? (outs as Array<{ role?: string; asset_kind?: string }>) : [];
}

export function jobDemAncestor(job: Job): string | undefined {
  const params = job.params as { dem_job_id?: string };
  if (typeof params.dem_job_id === "string" && params.dem_job_id) return params.dem_job_id;
  const result = job.result as { dem_job_id?: string } | undefined;
  if (result && typeof result.dem_job_id === "string" && result.dem_job_id) {
    return result.dem_job_id;
  }
  return undefined;
}

export function jobMatchesKind(job: Job, kind: string): boolean {
  if (job.status !== "succeeded") return false;
  const outs = jobOutputs(job);
  if (outs.length > 0) {
    if (kind === "raster.dem") {
      return outs.some((o) => o.role === "dem" && o.asset_kind === "raster");
    }
    if (kind === "raster.score") {
      return outs.some((o) => o.role === "ravine-score" && o.asset_kind === "raster");
    }
    if (kind === "vector.kml") {
      return outs.some((o) => o.role === "route" && o.asset_kind === "vector");
    }
  }
  if (kind === "raster.dem") {
    return job.kind === "build-dem" && typeof job.result?.dem_path === "string";
  }
  if (kind === "raster.score") {
    return (
      typeof job.result?.b_tpi_path === "string" ||
      typeof job.result?.a_geomorphon_path === "string" ||
      typeof job.result?.c_curvature_path === "string"
    );
  }
  if (kind === "vector.kml") {
    return (
      (job.kind === "kml-import" || job.kind === "kml-snap") &&
      typeof job.result?.kml_path === "string"
    );
  }
  return false;
}

export function jobLabel(job: Job): string {
  const name =
    (job.result?.name as string | undefined) ?? (job.params.name as string | undefined);
  return name ? `${name} (${job.id})` : `${job.kind} ${job.id}`;
}

// --- DOM form builder ---

export type ToolFormState = {
  inputs: Record<string, string>;
  params: Record<string, unknown>;
};

function field(label: string, control: HTMLElement, hint?: string): HTMLLabelElement {
  const wrap = document.createElement("label");
  wrap.className = "cgField";
  const span = document.createElement("span");
  span.textContent = label;
  if (hint) {
    const h = document.createElement("em");
    h.textContent = ` ${hint}`;
    span.appendChild(h);
  }
  wrap.append(span, control);
  return wrap;
}

function paramControl(
  name: string,
  schema: JsonSchema,
  state: ToolFormState,
): HTMLElement {
  const enumOptions = resolveEnum(schema);
  const current = state.params[name];
  if (enumOptions && enumOptions.length > 0) {
    const select = document.createElement("select");
    for (const opt of enumOptions) {
      const o = document.createElement("option");
      o.value = String(opt);
      o.textContent = String(opt);
      select.appendChild(o);
    }
    if (current != null) select.value = String(current);
    select.onchange = () => {
      state.params[name] = enumOptions.find((o) => String(o) === select.value);
    };
    return field(schema.title ?? name, select);
  }
  const t = resolveType(schema);
  if (t === "boolean") {
    const wrap = document.createElement("label");
    wrap.className = "cgField cgFieldCheck";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(current);
    input.onchange = () => {
      state.params[name] = input.checked;
    };
    const span = document.createElement("span");
    span.textContent = schema.title ?? name;
    wrap.append(input, span);
    return wrap;
  }
  if (t === "number" || t === "integer") {
    const input = document.createElement("input");
    input.type = "number";
    input.step = t === "integer" ? "1" : "any";
    if (current != null && current !== "") input.value = String(current);
    const nullable = isNullable(schema);
    input.onchange = () => {
      if (input.value === "") {
        if (nullable) state.params[name] = null;
        else delete state.params[name];
        return;
      }
      const n = Number(input.value);
      if (Number.isFinite(n)) state.params[name] = t === "integer" ? Math.trunc(n) : n;
    };
    return field(schema.title ?? name, input);
  }
  if (t === "array") {
    const itemType = resolveItems(schema) ? resolveType(resolveItems(schema)!) : undefined;
    const input = document.createElement("input");
    input.type = "text";
    if (Array.isArray(current)) input.value = current.map((v) => String(v)).join(", ");
    input.onchange = () => {
      state.params[name] = parseCsv(input.value, itemType);
    };
    return field(schema.title ?? name, input, "(comma-separated)");
  }
  const input = document.createElement("input");
  input.type = "text";
  if (current != null) input.value = String(current);
  input.onchange = () => {
    state.params[name] = input.value;
  };
  return field(schema.title ?? name, input);
}

/**
 * Build the form contents for a tool spec into `container`, initializing and
 * then mutating `state` as the user edits. Re-rendered from scratch on spec or
 * job-list changes by the caller.
 */
export function buildToolForm(
  container: HTMLElement,
  spec: JobSpec,
  jobs: Job[],
  state: ToolFormState,
): void {
  container.replaceChildren();

  const schema = (spec.request_schema ?? {}) as JsonSchema;
  const props = schema.properties ?? {};
  const defaults = spec.request_defaults ?? {};
  const paramKeys = Object.keys(props).filter((k) => !isHiddenParam(spec, k));

  for (const k of paramKeys) {
    if (!(k in state.params)) {
      if (k in defaults) state.params[k] = defaults[k];
      else if (props[k]?.default !== undefined) state.params[k] = props[k].default;
    }
  }

  const jobInputs = spec.inputs.filter(
    (inp) => inp.kind.startsWith("raster.") || inp.kind.startsWith("vector."),
  );
  const demInput = jobInputs.find((i) => i.kind === "raster.dem");

  const matching = (kind: string, demId?: string): Job[] => {
    let out = jobs.filter((j) => jobMatchesKind(j, kind));
    if (kind === "raster.score" && demId) {
      out = out.filter((j) => jobDemAncestor(j) === demId);
    }
    return out.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  };

  // Initialize input selections: DEM first, then DEM-scoped score inputs.
  for (const inp of jobInputs) {
    const demId = inp.kind === "raster.dem" ? undefined : state.inputs[demInput?.key ?? ""];
    const options = matching(inp.kind, demId);
    const current = state.inputs[inp.key];
    if (!current || !options.some((j) => j.id === current)) {
      state.inputs[inp.key] = options[0]?.id ?? "";
    }
  }

  if (spec.description) {
    const desc = document.createElement("p");
    desc.className = "cgToolDesc";
    desc.textContent = spec.description;
    container.appendChild(desc);
  }

  for (const inp of jobInputs) {
    const select = document.createElement("select");
    const demId = inp.kind === "raster.dem" ? undefined : state.inputs[demInput?.key ?? ""];
    const options = matching(inp.kind, demId);
    if (!inp.required) {
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "(none)";
      select.appendChild(none);
    } else if (options.length === 0) {
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "No compatible job";
      select.appendChild(none);
    }
    for (const job of options) {
      const o = document.createElement("option");
      o.value = job.id;
      o.textContent = jobLabel(job);
      select.appendChild(o);
    }
    select.value = state.inputs[inp.key] ?? "";
    select.onchange = () => {
      state.inputs[inp.key] = select.value;
      // Changing the DEM invalidates DEM-scoped selections; rebuild.
      if (inp.kind === "raster.dem") buildToolForm(container, spec, jobs, state);
    };
    container.appendChild(field(inp.label, select, inp.required ? undefined : "(optional)"));
  }

  // track-profile: replace the hidden category/track_index params with a
  // single track picker fed from the selected KML job's parsed tracks.
  if (spec.key === "track-profile") {
    const kmlKey = jobInputs.find((inp) => inp.kind === "vector.kml")?.key;
    const kmlJob = kmlKey ? jobs.find((j) => j.id === state.inputs[kmlKey]) : undefined;
    const select = document.createElement("select");
    const categories: Array<[string, string, string]> = [
      ["descent", "descent_geojson", "Descent"],
      ["approach", "approach_geojson", "Approach"],
      ["unclassified", "unclassified_geojson", "Track"],
    ];
    let count = 0;
    for (const [category, fieldName, label] of categories) {
      const data = kmlJob?.result?.[fieldName] as { features?: unknown[] } | undefined;
      const features = Array.isArray(data?.features) ? data.features : [];
      features.forEach((featRaw, idx) => {
        const feat = featRaw as {
          id?: unknown;
          properties?: Record<string, unknown> | null;
        };
        const props2 = feat.properties ?? {};
        const rawName = props2.name ?? props2.Name ?? props2.title;
        const name =
          typeof rawName === "string" && rawName ? rawName : `${label} ${idx + 1}`;
        const rawIdx = Number(feat.id ?? props2.id ?? props2.index);
        const trackIndex = Number.isFinite(rawIdx) ? Math.trunc(rawIdx) : idx;
        const o = document.createElement("option");
        o.value = `${category}:${trackIndex}`;
        o.textContent = `${label}: ${name}`;
        select.appendChild(o);
        count += 1;
      });
    }
    if (count === 0) {
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "No tracks in selected KML";
      select.appendChild(none);
    } else {
      const first = select.options[0].value.split(":");
      state.params.category = first[0];
      state.params.track_index = Number(first[1]);
    }
    select.onchange = () => {
      const [category, trackIndex] = select.value.split(":");
      if (!category) return;
      state.params.category = category;
      state.params.track_index = Number(trackIndex);
    };
    container.appendChild(field("Track", select));
  }

  for (const k of paramKeys) {
    container.appendChild(paramControl(k, props[k], state));
  }
}

/** The required job input (if any) that has no selection. */
export function missingRequiredInput(spec: JobSpec, state: ToolFormState): string | undefined {
  for (const inp of spec.inputs) {
    if (!inp.kind.startsWith("raster.") && !inp.kind.startsWith("vector.")) continue;
    if (inp.required && !state.inputs[inp.key]) return inp.label;
  }
  return undefined;
}

/** Final payload: drop empty inputs and undefined params. */
export function toolFormPayload(state: ToolFormState): {
  inputs: Record<string, string>;
  params: Record<string, unknown>;
} {
  const inputs: Record<string, string> = {};
  for (const [k, v] of Object.entries(state.inputs)) if (v) inputs[k] = v;
  const params: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(state.params)) {
    if (v !== undefined) params[k] = v;
  }
  return { inputs, params };
}
