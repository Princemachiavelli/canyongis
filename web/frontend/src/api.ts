export type User = { id: string; username: string };
export type AuthMe = {
  authenticated: boolean;
  single_user: boolean;
  user: User | null;
};
export type Project = {
  id: string;
  name: string;
  role: "owner" | "editor" | "viewer";
};

export type Bounds = {
  west: number;
  south: number;
  east: number;
  north: number;
};

export type JobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled";

export type Job = {
  id: string;
  kind: string;
  status: JobStatus;
  created_at: string;
  updated_at: string;
  params: Record<string, unknown>;
  result?: Record<string, unknown>;
  error?: string;
  progress?: string[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  // Inject active project header from localStorage
  try {
    const projectId = localStorage.getItem("activeProjectId");
    if (projectId) {
      headers.set("X-Project-Id", projectId);
    }
  } catch {
    // localStorage may not be available
  }
  const res = await fetch(path, {
    ...init,
    headers,
  });
  if (!res.ok) {
    const text = await res.text();
    let detail: unknown;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      detail = parsed.detail;
    } catch {
      if (text) throw new Error(text);
    }
    if (Array.isArray(detail))
      throw new Error(detail.map((item) => JSON.stringify(item)).join("\n"));
    if (detail && typeof detail === "object")
      throw new Error(JSON.stringify(detail, null, 2));
    if (detail) throw new Error(String(detail));
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export async function getAuthMe(): Promise<AuthMe> {
  return request("/api/auth/me");
}

export async function login(
  username: string,
  password: string,
): Promise<{ user: User }> {
  return request<{ user: User }>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export async function logout(): Promise<void> {
  await request("/api/auth/logout", { method: "POST" });
}

export async function listProjects(): Promise<Project[]> {
  const data = await request<{ projects: Project[] }>("/api/projects");
  return data.projects;
}

export async function listJobs(): Promise<Job[]> {
  const data = await request<{ jobs: Job[] }>("/api/jobs");
  return data.jobs;
}

export async function deleteJobRecord(jobId: string): Promise<void> {
  await request<{
    deleted: boolean;
    job_id: string;
    deleted_outputs?: string[];
  }>(`/api/jobs/${jobId}`, { method: "DELETE" });
}

export async function cancelJob(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}

export async function deleteGeneratedKmlLayer(
  jobId: string,
  layerKey: string,
): Promise<void> {
  await request<{
    deleted: boolean;
    job_id: string;
    layer_key: string;
    deleted_outputs?: string[];
  }>(`/api/jobs/${jobId}/kml-layer/${layerKey}`, { method: "DELETE" });
}

export async function buildDem(
  name: string,
  bounds: Bounds,
  bufferM: number,
): Promise<Job> {
  return request<Job>("/api/jobs/build-dem", {
    method: "POST",
    body: JSON.stringify({ name, bounds, buffer_m: bufferM }),
  });
}

export async function uploadKmlFile(
  file: File,
  containerDemJobId?: string,
): Promise<Job> {
  const body = new FormData();
  body.append("file", file);
  if (containerDemJobId) body.append("container_dem_job_id", containerDemJobId);
  return request<Job>("/api/jobs/import-kml-file", {
    method: "POST",
    body,
  });
}

export async function importRopewikiKml(
  source: string,
  containerDemJobId?: string,
): Promise<Job> {
  return request<Job>("/api/jobs/import-kml-ropewiki", {
    method: "POST",
    body: JSON.stringify({ source, container_dem_job_id: containerDemJobId }),
  });
}

export type KmlSnapTarget = "dem" | "tpi" | "geomorphon" | "curvature";

export async function snapKml(
  kmlJobId: string,
  payload: {
    target: KmlSnapTarget;
    dem_job_id?: string;
    compare_job_id?: string;
    search_radius_m?: number;
    sample_step_m?: number;
    high_threshold?: number;
  },
): Promise<Job> {
  return request<Job>(`/api/jobs/${kmlJobId}/snap-kml`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export type TrackCategory = "descent" | "approach" | "unclassified";

export type TrackProfilePayload = {
  dem_job_id: string;
  category: TrackCategory;
  track_index: number;
  sample_step_m?: number;
  rappel_window_m?: number;
  min_rappel_drop_m?: number;
  min_rappel_slope?: number;
};

export async function runTrackProfile(
  kmlJobId: string,
  payload: TrackProfilePayload,
): Promise<Job> {
  return request<Job>(`/api/jobs/${kmlJobId}/track-profile`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export type JobSpecVisualization = {
  key: string;
  label: string;
  url_field: string;
};

export type JobSpec = {
  key: string;
  label: string;
  description: string;
  runner_kind: "dem" | "context" | string;
  visualizations: JobSpecVisualization[];
  inputs: Array<{
    key: string;
    label: string;
    kind: string;
    required: boolean;
    default: string;
  }>;
  request_defaults: Record<string, unknown>;
  request_schema: Record<string, unknown>;
  available: boolean;
  unavailable_reason: string | null;
};

export async function listJobSpecs(): Promise<JobSpec[]> {
  const data = await request<{ specs: JobSpec[] }>("/api/job-specs");
  return data.specs;
}

export async function runDemJob(
  demJobId: string,
  jobKey: string,
  body: Record<string, unknown> = {},
): Promise<Job> {
  return request<Job>(`/api/jobs/${demJobId}/run/${jobKey}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function runTool(
  toolKey: string,
  payload: {
    inputs?: Record<string, unknown>;
    params?: Record<string, unknown>;
  } = {},
): Promise<Job> {
  return request<Job>(`/api/jobs/${toolKey}`, {
    method: "POST",
    body: JSON.stringify({
      inputs: payload.inputs ?? {},
      params: payload.params ?? {},
    }),
  });
}

export async function trimJobRuns(
  demJobId: string,
  jobKey: string,
): Promise<{
  deleted: number;
  deleted_jobs: string[];
  deleted_outputs: string[];
}> {
  return request(`/api/jobs/${demJobId}/trim-runs/${jobKey}`, {
    method: "POST",
  });
}

export async function runRavineSpine(
  jobId: string,
  parentKmlJobId?: string,
): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/ravine-spine`, {
    method: "POST",
    body: JSON.stringify({
      threshold_pct: 94,
      hysteresis_ratio: 0.75,
      branch_keep_ratio: 0.2,
      max_path_slope: 0.3,
      parent_kml_job_id: parentKmlJobId,
    }),
  });
}
