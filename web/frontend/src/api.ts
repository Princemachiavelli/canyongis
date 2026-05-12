export type Bounds = {
  west: number;
  south: number;
  east: number;
  north: number;
};

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

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
  const res = await fetch(path, {
    ...init,
    headers
  });
  if (!res.ok) {
    throw new Error(await res.text());
  }
  return (await res.json()) as T;
}

export async function listJobs(): Promise<Job[]> {
  const data = await request<{ jobs: Job[] }>("/api/jobs");
  return data.jobs;
}

export async function deleteFailedJob(jobId: string): Promise<void> {
  await request<{ deleted: boolean; job_id: string }>(`/api/jobs/${jobId}`, {
    method: "DELETE"
  });
}

export async function buildDem(name: string, bounds: Bounds, bufferM: number): Promise<Job> {
  return request<Job>("/api/jobs/build-dem", {
    method: "POST",
    body: JSON.stringify({ name, bounds, buffer_m: bufferM })
  });
}

export async function uploadKmlFile(file: File): Promise<Job> {
  const body = new FormData();
  body.append("file", file);
  return request<Job>("/api/jobs/import-kml-file", {
    method: "POST",
    body
  });
}

export async function importRopewikiKml(source: string): Promise<Job> {
  return request<Job>("/api/jobs/import-kml-ropewiki", {
    method: "POST",
    body: JSON.stringify({ source })
  });
}

export async function runDrainage(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/drainage`, {
    method: "POST",
    body: JSON.stringify({ thresholds: [200, 500, 1000, 5000], line_width_px: 2 })
  });
}

export async function runRavineCompare(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/ravine-compare`, {
    method: "POST",
    body: JSON.stringify({
      scales_m: [5, 10, 30, 100],
      combine: "normalized_sum",
      smooth_m: 2,
      norm_pct: 99.5
    })
  });
}

export async function runRavineSpine(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/ravine-spine`, {
    method: "POST",
    body: JSON.stringify({
      threshold_pct: 94,
      hysteresis_ratio: 0.75,
      branch_keep_ratio: 0.2,
      max_path_slope: 0.3
    })
  });
}
