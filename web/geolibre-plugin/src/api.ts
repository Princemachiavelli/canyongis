// HTTP client for the canyongis web server, ported from web/frontend/src/api.ts.
// Differences from the React app's client: the plugin runs on a different origin
// than the API, so every request carries an explicit base URL and
// credentials: "include" (cookie sessions), and relative output URLs returned by
// the server are absolutized against that base.

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

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";

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

export type JobSpecInput = {
  key: string;
  label: string;
  kind: string;
  required: boolean;
  default: string;
};

export type JobSpec = {
  key: string;
  label: string;
  description: string;
  runner_kind: string;
  visualizations: Array<{ key: string; label: string; url_field: string }>;
  inputs: JobSpecInput[];
  request_defaults: Record<string, unknown>;
  request_schema: Record<string, unknown>;
  available: boolean;
  unavailable_reason: string | null;
};

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export class CanyonGisClient {
  baseUrl: string;
  projectId: string | undefined;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  /** Absolutize a server-relative URL such as "/outputs/...". */
  absUrl(url: string): string {
    if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return url;
    return url.startsWith("/") ? `${this.baseUrl}${url}` : `${this.baseUrl}/${url}`;
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers = new Headers(init?.headers);
    if (!(init?.body instanceof FormData) && !headers.has("content-type")) {
      headers.set("content-type", "application/json");
    }
    if (this.projectId) headers.set("X-Project-Id", this.projectId);
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers,
      credentials: "include",
    });
    if (!res.ok) {
      const text = await res.text();
      let detail: unknown;
      try {
        detail = (JSON.parse(text) as { detail?: unknown }).detail;
      } catch {
        if (text) throw new ApiError(res.status, text);
      }
      if (Array.isArray(detail)) {
        throw new ApiError(res.status, detail.map((item) => JSON.stringify(item)).join("\n"));
      }
      if (detail && typeof detail === "object") {
        throw new ApiError(res.status, JSON.stringify(detail));
      }
      if (detail) throw new ApiError(res.status, String(detail));
      throw new ApiError(res.status, `${res.status} ${res.statusText}`);
    }
    return (await res.json()) as T;
  }

  health(): Promise<{ status: string }> {
    return this.request("/api/health");
  }

  authMe(): Promise<AuthMe> {
    return this.request("/api/auth/me");
  }

  login(username: string, password: string): Promise<{ user: User }> {
    return this.request("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  }

  async logout(): Promise<void> {
    await this.request("/api/auth/logout", { method: "POST" });
  }

  async listProjects(): Promise<Project[]> {
    const data = await this.request<{ projects: Project[] }>("/api/projects");
    return data.projects;
  }

  async listJobs(): Promise<Job[]> {
    const data = await this.request<{ jobs: Job[] }>("/api/jobs");
    return data.jobs;
  }

  getJob(jobId: string): Promise<Job> {
    return this.request(`/api/jobs/${jobId}`);
  }

  async listJobSpecs(): Promise<JobSpec[]> {
    const data = await this.request<{ specs: JobSpec[] }>("/api/job-specs");
    return data.specs;
  }

  buildDem(name: string, bounds: Bounds, bufferM: number): Promise<Job> {
    return this.request("/api/jobs/build-dem", {
      method: "POST",
      body: JSON.stringify({ name, bounds, buffer_m: bufferM }),
    });
  }

  runTool(
    toolKey: string,
    payload: { inputs?: Record<string, unknown>; params?: Record<string, unknown> } = {},
  ): Promise<Job> {
    return this.request(`/api/jobs/${toolKey}`, {
      method: "POST",
      body: JSON.stringify({ inputs: payload.inputs ?? {}, params: payload.params ?? {} }),
    });
  }

  importRopewikiKml(source: string, containerDemJobId?: string): Promise<Job> {
    return this.request("/api/jobs/import-kml-ropewiki", {
      method: "POST",
      body: JSON.stringify({ source, container_dem_job_id: containerDemJobId }),
    });
  }

  cancelJob(jobId: string): Promise<Job> {
    return this.request(`/api/jobs/${jobId}/cancel`, { method: "POST" });
  }

  retryJob(jobId: string): Promise<Job> {
    return this.request(`/api/jobs/${jobId}/retry`, { method: "POST" });
  }

  deleteJob(jobId: string): Promise<unknown> {
    return this.request(`/api/jobs/${jobId}`, { method: "DELETE" });
  }
}
