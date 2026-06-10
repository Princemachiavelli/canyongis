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

export type Asset = {
  id: string;
  project_id: string;
  project_name: string;
  job_id: string | null;
  kind: string;
  path: string | null;
  url: string | null;
  bounds_wgs84: number[] | null;
  format: string | null;
  media_type: string | null;
  size_bytes: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
  linked: boolean;
  linked_at: string | null;
  projects?: { id: string; name: string }[];
};

export type AssetLineageNode = {
  id: string;
  kind: string;
  path: string | null;
  url: string | null;
  job_id: string | null;
  bounds_wgs84: number[] | null;
};

export type AssetLineageEdge = {
  consumer: string;
  input_key: string;
  producer: string;
};

export type AssetLineage = {
  root: string;
  nodes: Record<string, AssetLineageNode>;
  edges: AssetLineageEdge[];
};

export type ProjectPipelineAsset = {
  id: string;
  job_id: string | null;
  kind: string;
  path: string | null;
  url: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type ProjectPipeline = {
  assets: ProjectPipelineAsset[];
  edges: AssetLineageEdge[];
};

export type ElevationProbe = {
  lat: number;
  lng: number;
  elevation_m: number | null;
  asset_id: string | null;
  asset_label: string | null;
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

export async function createProject(name: string): Promise<Project> {
  return request<Project>("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export async function updateProject(projectId: string, name: string): Promise<Project> {
  return request<Project>(`/api/projects/${projectId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export async function deleteProject(projectId: string): Promise<{ deleted: boolean; project_id: string }> {
  return request(`/api/projects/${projectId}`, { method: "DELETE" });
}

export async function listAssetLibrary(kind?: string): Promise<Asset[]> {
  const params = new URLSearchParams();
  if (kind) params.set("kind", kind);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  const data = await request<{ assets: Asset[] }>(`/api/assets${suffix}`);
  return data.assets;
}

export async function listProjectAssets(
  projectId: string,
  kind?: string,
): Promise<Asset[]> {
  const params = new URLSearchParams();
  if (kind) params.set("kind", kind);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  const data = await request<{ assets: Asset[] }>(
    `/api/projects/${projectId}/assets${suffix}`,
  );
  return data.assets;
}

export async function getAssetLineage(assetId: string): Promise<AssetLineage> {
  return request<AssetLineage>(`/api/assets/${assetId}/lineage`);
}

export async function linkProjectAsset(
  projectId: string,
  assetId: string,
): Promise<{ linked: boolean; asset_id: string; project_id: string }> {
  return request(`/api/projects/${projectId}/assets/link`, {
    method: "POST",
    body: JSON.stringify({ asset_id: assetId }),
  });
}

export async function unlinkProjectAsset(
  projectId: string,
  assetId: string,
): Promise<{ deleted: boolean; asset_id: string; project_id: string }> {
  return request(`/api/projects/${projectId}/assets/${assetId}`, {
    method: "DELETE",
  });
}

export async function hideProjectAsset(
  projectId: string,
  assetId: string,
): Promise<{ hidden: boolean; mode: "hidden" | "unlinked"; asset_id: string; project_id: string }> {
  return request(`/api/projects/${projectId}/assets/${assetId}/hide`, {
    method: "POST",
  });
}

export async function unhideProjectAsset(
  projectId: string,
  assetId: string,
): Promise<{ unhidden: boolean; asset_id: string; project_id: string }> {
  return request(`/api/projects/${projectId}/assets/${assetId}/unhide`, {
    method: "POST",
  });
}

export async function listHiddenProjectAssets(projectId: string): Promise<string[]> {
  const data = await request<{ asset_ids: string[] }>(
    `/api/projects/${projectId}/assets/hidden`,
  );
  return data.asset_ids;
}

export async function getProjectPipeline(projectId: string): Promise<ProjectPipeline> {
  return request<ProjectPipeline>(`/api/projects/${projectId}/pipeline`);
}

export async function getProjectElevation(
  projectId: string,
  lat: number,
  lng: number,
): Promise<ElevationProbe> {
  const params = new URLSearchParams({ lat: String(lat), lng: String(lng) });
  return request<ElevationProbe>(`/api/projects/${projectId}/elevation?${params.toString()}`);
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

export async function retryJob(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/retry`, { method: "POST" });
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

export async function resolveMapUrl(url: string): Promise<string> {
  const data = await request<{ url: string }>("/api/resolve-map-url", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
  return data.url;
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

export async function setKmlContainer(
  kmlJobId: string,
  containerDemJobId: string | null,
): Promise<Job> {
  return request<Job>(`/api/jobs/${kmlJobId}/container`, {
    method: "POST",
    body: JSON.stringify({ container_dem_job_id: containerDemJobId }),
  });
}

export async function syncKmlImport(kmlJobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${kmlJobId}/sync-kml`, { method: "POST" });
}

export async function importRopewikiBulk(args: {
  bounds?: Bounds;
  containerDemJobId?: string;
  maxResults: number;
}): Promise<Job> {
  return request<Job>("/api/jobs/import-kml-ropewiki-bulk", {
    method: "POST",
    body: JSON.stringify({
      params: {
        bounds: args.bounds,
        container_dem_job_id: args.containerDemJobId,
        max_results: args.maxResults,
      },
      inputs: args.containerDemJobId ? { container: args.containerDemJobId } : {},
    }),
  });
}

export type KmlSnapTarget = "dem" | "tpi" | "geomorphon" | "curvature";

export type TrackCategory = "descent" | "approach" | "unclassified";

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
