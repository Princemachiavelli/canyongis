// The CanyonGIS map control: connection/login, AOI → DEM mosaic, Ropewiki
// import, job-spec tool forms, and a job activity list with layer handoff to
// GeoLibre. All data display/organization is delegated to GeoLibre's layer
// panel; this control only launches work and registers finished layers.

import { ApiError, CanyonGisClient } from "./api";
import type { AuthMe, Bounds, Job, JobSpec, Project } from "./api";
import { clearAoi, startAoiDraw } from "./aoi";
import type { AoiDrawHandle } from "./aoi";
import { buildToolForm, missingRequiredInput, toolFormPayload } from "./forms";
import type { ToolFormState } from "./forms";
import { addJobLayers, jobBounds, jobName } from "./layers";
import type { GeoLibreAppAPI, MapControl, MapLike } from "./geolibre";

const POLL_MS = 3000;
const STORAGE_SERVER = "canyongis.serverUrl";
const STORAGE_PROJECT = "canyongis.projectId";

/** The current HTTP(S) origin, or null when there isn't one (e.g. file://). */
function httpOrigin(): string | null {
  try {
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    return origin.startsWith("http") ? origin : null;
  } catch {
    return null;
  }
}

/** Default canyongis server URL.
 *
 * The plugin is normally served by the canyongis web server itself, so the API
 * lives at the same origin and no manual URL is needed. Fall back to the local
 * dev port only when there is no usable HTTP origin.
 */
const DEFAULT_SERVER = httpOrigin() ?? "http://127.0.0.1:8765";

export type PluginSettings = {
  serverUrl: string;
  projectId?: string;
  /** One-shot: hydrate all succeeded-job layers after the next connect.
      Set by exported .geolibre.json projects; cleared once performed. */
  autoLoad?: boolean;
};

export function loadSettings(): PluginSettings {
  let serverUrl = DEFAULT_SERVER;
  let projectId: string | undefined;
  try {
    // When served from a real origin the plugin talks to that same origin, so
    // it is authoritative — ignore any stored URL (a stale one would otherwise
    // block auto-connect). Only honor a stored URL when there's no usable
    // origin (e.g. opened via file://). Explicit overrides still arrive via
    // URL params / project state through applySettings().
    if (httpOrigin() === null) serverUrl = localStorage.getItem(STORAGE_SERVER) || DEFAULT_SERVER;
    projectId = localStorage.getItem(STORAGE_PROJECT) ?? undefined;
  } catch {
    // localStorage unavailable (e.g. blocked); fall back to defaults
  }
  return { serverUrl, projectId };
}

function persistSettings(settings: PluginSettings): void {
  try {
    localStorage.setItem(STORAGE_SERVER, settings.serverUrl);
    if (settings.projectId) localStorage.setItem(STORAGE_PROJECT, settings.projectId);
    else localStorage.removeItem(STORAGE_PROJECT);
  } catch {
    // best-effort persistence only
  }
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label: string, onClick: () => void, className = ""): HTMLButtonElement {
  const b = el("button", `cgBtn ${className}`.trim(), label);
  b.type = "button";
  b.onclick = onClick;
  return b;
}

function section(title: string, open = true): { root: HTMLElement; body: HTMLElement } {
  const root = el("details", "cgSection") as unknown as HTMLDetailsElement;
  root.open = open;
  const head = el("summary", "cgSectionHead", title);
  const body = el("div", "cgSectionBody");
  root.append(head, body);
  return { root: root as unknown as HTMLElement, body };
}

export class CanyonGisControl implements MapControl {
  readonly settings: PluginSettings;

  private readonly app: GeoLibreAppAPI;
  private map?: MapLike;
  private container?: HTMLElement;

  private client?: CanyonGisClient;
  private me?: AuthMe;
  private projects: Project[] = [];
  private specs: JobSpec[] = [];
  private jobs: Job[] = [];

  private bounds?: Bounds;
  private drawHandle?: AoiDrawHandle;

  private selectedSpecKey?: string;
  private toolState: ToolFormState = { inputs: {}, params: {} };

  private pollTimer?: number;
  private jobStatuses = new Map<string, string>();
  private statusesPrimed = false;
  private addedDescriptors = new Set<string>();

  // dynamic regions
  private statusEl!: HTMLElement;
  private authBody!: HTMLElement;
  private aoiText!: HTMLElement;
  private toolBody!: HTMLElement;
  private toolFormEl!: HTMLElement;
  private jobsBody!: HTMLElement;
  private logEl!: HTMLElement;
  private logLines: string[] = [];

  constructor(app: GeoLibreAppAPI, settings: PluginSettings) {
    this.app = app;
    this.settings = settings;
  }

  // --- MapControl ---

  onAdd(map: MapLike): HTMLElement {
    this.map = map;
    this.container = this.build();
    void this.connect();
    return this.container;
  }

  onRemove(): void {
    this.stopPolling();
    this.drawHandle?.cancel();
    if (this.map) clearAoi(this.map);
    this.container?.remove();
    this.container = undefined;
    this.map = undefined;
  }

  /** Re-apply settings restored from a saved project. */
  applySettings(settings: Partial<PluginSettings>): void {
    if (settings.serverUrl) this.settings.serverUrl = settings.serverUrl;
    if (settings.projectId !== undefined) this.settings.projectId = settings.projectId;
    if (this.container) void this.connect();
  }

  // --- connection / auth ---

  private async connect(): Promise<void> {
    this.stopPolling();
    this.me = undefined;
    this.projects = [];
    this.specs = [];
    this.jobs = [];
    this.statusesPrimed = false;
    this.jobStatuses.clear();
    this.client = new CanyonGisClient(this.settings.serverUrl);
    this.client.projectId = this.settings.projectId;
    persistSettings(this.settings);
    this.setStatus("Connecting…");
    try {
      await this.client.health();
      this.me = await this.client.authMe();
    } catch (error) {
      this.setStatus(`Unreachable: ${this.errText(error)}`);
      this.renderAuth();
      return;
    }
    if (!this.me.authenticated) {
      this.setStatus("Connected — login required");
      this.renderAuth();
      return;
    }
    await this.onAuthenticated();
  }

  private async onAuthenticated(): Promise<void> {
    if (!this.client || !this.me) return;
    const who = this.me.user?.username ?? "local";
    this.setStatus(`Connected as ${who}`);
    try {
      this.projects = await this.client.listProjects();
      if (
        this.settings.projectId &&
        !this.projects.some((p) => p.id === this.settings.projectId)
      ) {
        this.settings.projectId = undefined;
        this.client.projectId = undefined;
      }
      this.specs = await this.client.listJobSpecs();
    } catch (error) {
      this.setStatus(`Connected, but: ${this.errText(error)}`);
    }
    this.renderAuth();
    this.renderToolPicker();
    await this.refreshJobs();
    if (this.settings.autoLoad) {
      this.settings.autoLoad = false;
      this.loadProjectLayers();
    }
    this.startPolling();
  }

  /** Add layers for every succeeded job in the project, then zoom to them. */
  private loadProjectLayers(): void {
    const succeeded = this.jobs.filter((job) => job.status === "succeeded");
    if (succeeded.length === 0) {
      this.log("No succeeded jobs in this project yet");
      return;
    }
    let union: [number, number, number, number] | undefined;
    for (const job of succeeded) {
      this.addLayersForJob(job, { zoom: false });
      const b = jobBounds(job);
      if (b) {
        union = union
          ? [
              Math.min(union[0], b[0]),
              Math.min(union[1], b[1]),
              Math.max(union[2], b[2]),
              Math.max(union[3], b[3]),
            ]
          : b;
      }
    }
    if (union) this.app.fitBounds?.(union);
    this.log(`Loaded layers from ${succeeded.length} job(s)`);
  }

  private async login(username: string, password: string): Promise<void> {
    if (!this.client) return;
    try {
      await this.client.login(username, password);
      this.me = await this.client.authMe();
      await this.onAuthenticated();
    } catch (error) {
      this.setStatus(`Login failed: ${this.errText(error)}`);
    }
  }

  private async logout(): Promise<void> {
    if (!this.client) return;
    try {
      await this.client.logout();
    } catch {
      // server may be gone; treat as logged out locally
    }
    await this.connect();
  }

  // --- polling / jobs ---

  private startPolling(): void {
    this.stopPolling();
    void this.refreshJobs();
    this.pollTimer = window.setInterval(() => void this.refreshJobs(), POLL_MS);
  }

  private stopPolling(): void {
    if (this.pollTimer !== undefined) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = undefined;
    }
  }

  private async refreshJobs(): Promise<void> {
    if (!this.client || !this.me?.authenticated) return;
    let jobs: Job[];
    try {
      jobs = await this.client.listJobs();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        this.stopPolling();
        this.me = undefined;
        this.setStatus("Session expired — login required");
        this.renderAuth();
      }
      return;
    }
    jobs.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    this.jobs = jobs;
    if (this.statusesPrimed) {
      for (const job of jobs) {
        const prev = this.jobStatuses.get(job.id);
        if (prev !== undefined && prev !== "succeeded" && job.status === "succeeded") {
          this.handleJobSucceeded(job);
        }
      }
    }
    this.jobStatuses = new Map(jobs.map((j) => [j.id, j.status]));
    this.statusesPrimed = true;
    this.renderJobs();
  }

  private handleJobSucceeded(job: Job): void {
    this.log(`${jobName(job)} (${job.kind}) succeeded`);
    this.addLayersForJob(job, { zoom: true });
  }

  private addLayersForJob(job: Job, opts: { zoom: boolean }): void {
    const added = addJobLayers(
      this.app,
      this.client!,
      job,
      (m) => this.log(m),
      this.addedDescriptors,
    );
    if (added.length === 0 && opts.zoom) {
      this.log(`${jobName(job)}: no new map layers in result`);
    }
    if (opts.zoom) {
      const b = jobBounds(job);
      if (b) this.app.fitBounds?.(b);
    }
  }

  // --- actions ---

  private async buildDem(name: string, bufferM: number): Promise<void> {
    if (!this.client) return;
    if (!this.bounds) {
      this.log("Draw an AOI first");
      return;
    }
    try {
      const job = await this.client.buildDem(name || "AOI DEM", this.bounds, bufferM);
      this.log(`DEM mosaic queued (${job.id})`);
      void this.refreshJobs();
    } catch (error) {
      this.log(`DEM build failed: ${this.errText(error)}`);
    }
  }

  private async importRopewiki(source: string): Promise<void> {
    if (!this.client) return;
    if (!source.trim()) {
      this.log("Enter a Ropewiki URL or page title");
      return;
    }
    try {
      const job = await this.client.importRopewikiKml(source.trim());
      this.log(`Ropewiki import queued (${job.id})`);
      void this.refreshJobs();
    } catch (error) {
      this.log(`Import failed: ${this.errText(error)}`);
    }
  }

  private async runSelectedTool(): Promise<void> {
    if (!this.client || !this.selectedSpecKey) return;
    const spec = this.specs.find((s) => s.key === this.selectedSpecKey);
    if (!spec) return;
    const missing = missingRequiredInput(spec, this.toolState);
    if (missing) {
      this.log(`Required input "${missing}" has no compatible job`);
      return;
    }
    try {
      const job = await this.client.runTool(spec.key, toolFormPayload(this.toolState));
      this.log(`${spec.label} queued (${job.id})`);
      void this.refreshJobs();
    } catch (error) {
      this.log(`${spec.label} failed: ${this.errText(error)}`);
    }
  }

  // --- DOM ---

  private build(): HTMLElement {
    const root = el("div", "maplibregl-ctrl canyongis-ctrl");
    const head = el("div", "cgHead");
    head.append(el("strong", "", "CanyonGIS"));
    const collapse = button("–", () => {
      body.classList.toggle("cgHidden");
      collapse.textContent = body.classList.contains("cgHidden") ? "+" : "–";
    }, "cgCollapse");
    head.appendChild(collapse);
    const body = el("div", "cgBody");
    root.append(head, body);

    // Connection status and login stay visible; the server URL is an optional,
    // collapsed override since the plugin defaults to the serving origin.
    this.statusEl = el("div", "cgStatus", "Not connected");
    this.authBody = el("div");

    const server = section("Server (advanced)", false);
    const serverInput = el("input", "cgInput") as HTMLInputElement;
    serverInput.type = "text";
    serverInput.value = this.settings.serverUrl;
    serverInput.placeholder = DEFAULT_SERVER;
    const connectBtn = button("Connect", () => {
      this.settings.serverUrl = serverInput.value.trim().replace(/\/+$/, "") || DEFAULT_SERVER;
      void this.connect();
    }, "cgPrimary");
    const serverRow = el("div", "cgRow");
    serverRow.append(serverInput, connectBtn);
    server.body.append(serverRow);

    // AOI / DEM
    const aoi = section("AOI → DEM mosaic");
    this.aoiText = el("div", "cgMeta", "No AOI selected");
    const drawBtn = button("Draw AOI", () => this.beginAoiDraw(drawBtn));
    const clearBtn = button("Clear", () => {
      this.bounds = undefined;
      if (this.map) clearAoi(this.map);
      this.renderAoiText();
    });
    const aoiRow = el("div", "cgRow");
    aoiRow.append(drawBtn, clearBtn);
    const nameInput = el("input", "cgInput") as HTMLInputElement;
    nameInput.type = "text";
    nameInput.placeholder = "DEM name";
    const bufferInput = el("input", "cgInput cgNarrow") as HTMLInputElement;
    bufferInput.type = "number";
    bufferInput.value = "2000";
    bufferInput.title = "Buffer (m)";
    const demBtn = button("Build DEM", () => {
      void this.buildDem(nameInput.value, Number(bufferInput.value) || 2000);
    }, "cgPrimary");
    const demRow = el("div", "cgRow");
    demRow.append(nameInput, bufferInput, demBtn);
    aoi.body.append(aoiRow, this.aoiText, demRow);

    // Import
    const imp = section("Import route");
    const ropewikiInput = el("input", "cgInput") as HTMLInputElement;
    ropewikiInput.type = "text";
    ropewikiInput.placeholder = "Ropewiki URL or title";
    const impBtn = button("Import", () => {
      void this.importRopewiki(ropewikiInput.value);
    }, "cgPrimary");
    const impRow = el("div", "cgRow");
    impRow.append(ropewikiInput, impBtn);
    imp.body.append(impRow);

    // Tools
    const tools = section("Tools");
    this.toolBody = tools.body;
    this.toolFormEl = el("div", "cgToolForm");
    this.renderToolPicker();

    // Jobs
    const jobsSec = section("Jobs");
    this.jobsBody = jobsSec.body;

    // Log
    this.logEl = el("div", "cgLog");

    body.append(
      this.statusEl,
      this.authBody,
      aoi.root,
      imp.root,
      tools.root,
      jobsSec.root,
      server.root,
      this.logEl,
    );
    return root;
  }

  private beginAoiDraw(drawBtn: HTMLButtonElement): void {
    if (!this.map) return;
    this.drawHandle?.cancel();
    drawBtn.disabled = true;
    this.drawHandle = startAoiDraw(this.map, (bounds) => {
      drawBtn.disabled = false;
      this.bounds = bounds;
      this.renderAoiText();
    });
  }

  private renderAoiText(): void {
    if (!this.bounds) {
      this.aoiText.textContent = "No AOI selected";
      return;
    }
    const b = this.bounds;
    this.aoiText.textContent =
      `${b.west.toFixed(4)}, ${b.south.toFixed(4)} → ` +
      `${b.east.toFixed(4)}, ${b.north.toFixed(4)}`;
  }

  private renderAuth(): void {
    this.authBody.replaceChildren();
    if (!this.me) return;
    if (!this.me.authenticated) {
      const userInput = el("input", "cgInput") as HTMLInputElement;
      userInput.type = "text";
      userInput.placeholder = "username";
      userInput.autocomplete = "username";
      const passInput = el("input", "cgInput") as HTMLInputElement;
      passInput.type = "password";
      passInput.placeholder = "password";
      passInput.autocomplete = "current-password";
      const loginBtn = button("Login", () => {
        void this.login(userInput.value, passInput.value);
      }, "cgPrimary");
      passInput.onkeydown = (ev) => {
        if (ev.key === "Enter") loginBtn.click();
      };
      const row = el("div", "cgRow");
      row.append(userInput, passInput, loginBtn);
      this.authBody.appendChild(row);
      return;
    }
    if (this.projects.length > 0) {
      const select = el("select", "cgInput") as HTMLSelectElement;
      for (const project of this.projects) {
        const o = el("option", "", `${project.name} (${project.role})`) as HTMLOptionElement;
        o.value = project.id;
        select.appendChild(o);
      }
      if (this.settings.projectId) select.value = this.settings.projectId;
      else if (this.projects[0]) this.settings.projectId = this.projects[0].id;
      select.onchange = () => {
        this.settings.projectId = select.value;
        if (this.client) this.client.projectId = select.value;
        persistSettings(this.settings);
        this.statusesPrimed = false;
        this.jobStatuses.clear();
        void this.refreshJobs();
      };
      if (this.client) this.client.projectId = this.settings.projectId;
      const row = el("div", "cgRow");
      row.append(select);
      row.appendChild(button("Load layers", () => this.loadProjectLayers()));
      if (!this.me.single_user) {
        row.appendChild(button("Logout", () => void this.logout()));
      }
      this.authBody.appendChild(row);
    }
  }

  private renderToolPicker(): void {
    this.toolBody.replaceChildren();
    if (this.specs.length === 0) {
      this.toolBody.appendChild(el("div", "cgMeta", "Connect to load tools"));
      return;
    }
    const select = el("select", "cgInput") as HTMLSelectElement;
    const placeholder = el("option", "", "Select a tool…") as HTMLOptionElement;
    placeholder.value = "";
    select.appendChild(placeholder);
    for (const spec of this.specs) {
      const o = el("option", "", spec.available ? spec.label : `${spec.label} (unavailable)`);
      (o as HTMLOptionElement).value = spec.key;
      (o as HTMLOptionElement).disabled = !spec.available;
      select.appendChild(o);
    }
    if (this.selectedSpecKey) select.value = this.selectedSpecKey;
    select.onchange = () => {
      this.selectedSpecKey = select.value || undefined;
      this.toolState = { inputs: {}, params: {} };
      this.renderToolForm();
    };
    const runBtn = button("Run", () => void this.runSelectedTool(), "cgPrimary");
    const row = el("div", "cgRow");
    row.append(select, runBtn);
    this.toolBody.append(row, this.toolFormEl);
    this.renderToolForm();
  }

  private renderToolForm(): void {
    this.toolFormEl.replaceChildren();
    const spec = this.specs.find((s) => s.key === this.selectedSpecKey);
    if (!spec) return;
    buildToolForm(this.toolFormEl, spec, this.jobs, this.toolState);
  }

  private renderJobs(): void {
    this.jobsBody.replaceChildren();
    if (this.jobs.length === 0) {
      this.jobsBody.appendChild(el("div", "cgMeta", "No jobs yet"));
      return;
    }
    for (const job of this.jobs.slice(0, 25)) {
      const row = el("div", `cgJob cgJob-${job.status}`);
      const title = el("div", "cgJobTitle");
      title.append(
        el("span", "cgJobName", jobName(job)),
        el("span", "cgJobKind", job.kind),
        el("span", `cgJobStatus cgStatus-${job.status}`, job.status),
      );
      row.appendChild(title);
      if (job.status === "running" && job.progress && job.progress.length > 0) {
        row.appendChild(el("div", "cgJobProgress", job.progress[job.progress.length - 1]));
      }
      if (job.status === "failed" && job.error) {
        row.appendChild(el("div", "cgJobError", job.error.slice(0, 200)));
      }
      const actions = el("div", "cgRow cgJobActions");
      if (job.status === "succeeded") {
        actions.appendChild(
          button("Add to map", () => this.addLayersForJob(job, { zoom: false })),
        );
        const b = jobBounds(job);
        if (b) actions.appendChild(button("Zoom", () => this.app.fitBounds?.(b)));
      }
      if (job.status === "queued" || job.status === "running") {
        actions.appendChild(
          button("Cancel", () => {
            void this.client?.cancelJob(job.id).then(() => this.refreshJobs());
          }),
        );
      }
      if (job.status === "failed" || job.status === "canceled") {
        actions.appendChild(
          button("Retry", () => {
            void this.client
              ?.retryJob(job.id)
              .then(() => this.refreshJobs())
              .catch((e: unknown) => this.log(this.errText(e)));
          }),
        );
      }
      if (job.status !== "queued" && job.status !== "running") {
        actions.appendChild(
          button("Delete", () => {
            void this.client
              ?.deleteJob(job.id)
              .then(() => this.refreshJobs())
              .catch((e: unknown) => this.log(this.errText(e)));
          }, "cgDanger"),
        );
      }
      row.appendChild(actions);
      this.jobsBody.appendChild(row);
    }
  }

  private setStatus(text: string): void {
    if (this.statusEl) this.statusEl.textContent = text;
  }

  private log(message: string): void {
    this.logLines.push(message);
    this.logLines = this.logLines.slice(-4);
    if (this.logEl) {
      this.logEl.replaceChildren(
        ...this.logLines.map((line) => el("div", "cgLogLine", line)),
      );
    }
  }

  private errText(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
  }
}
