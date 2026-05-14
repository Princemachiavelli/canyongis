# Login UI + Project Picker — implementation plan

Goal: unblock the 401 errors by adding a frontend login flow, plus a header
project picker that's wired end-to-end. Keep this refactor small & minimal.

## Design

- **Active project selection** uses an `X-Project-Id` request header (not a
  session cookie). Client stores the chosen project in `localStorage`; server
  validates membership and falls back to the user's Personal project when the
  header is missing.
  - Rationale: no session-cookie refactoring needed. Membership is still
    enforced server-side, so client-controlled state is fine.
- **Login UI** is a modal that blocks the app whenever
  `GET /api/auth/me` reports `authenticated: false`. Single-user mode
  (`CANYONGIS_SINGLE_USER=1`) auto-passes the gate.
- **Header** shows the username, the project dropdown, and a logout button.
  If the user has only one project, the dropdown is disabled (display-only).

## Backend (`src/canyongis/web/server.py`)

1. **`GET /api/projects`** — returns the active user's project memberships:
   ```json
   { "projects": [{ "id": "...", "name": "Personal", "role": "owner" }] }
   ```
2. **`_active_project_id(request, user)`** helper:
   - Read `X-Project-Id` header.
   - If present: verify the user has a membership row; else 403.
   - If absent: fall back to `ctx.store.ensure_personal_project(user)`.
3. Wrap as `ActiveProjectDep = Depends(_active_project_id)`.
4. Replace inline `ensure_personal_project(user)` calls with
   `project_id: str = ActiveProjectDep` on the routes that need it
   (`list_jobs`, all `ctx.submit` callers).
5. Refactor `ctx.submit(...)` signature to take `project_id` (and keep `user`
   for `created_by_user_id`) instead of computing the project itself.

## Frontend

### `web/frontend/src/api.ts`

- Inject `X-Project-Id` header (from `localStorage.activeProjectId`) on every
  request.
- Add:
  - `getAuthMe(): Promise<AuthMe>`
  - `login(username, password): Promise<User>`
  - `logout(): Promise<void>`
  - `listProjects(): Promise<Project[]>`

Types:
```ts
type User = { id: string; username: string };
type AuthMe = { authenticated: boolean; single_user: boolean; user: User | null };
type Project = { id: string; name: string; role: "owner" | "editor" | "viewer" };
```

### `web/frontend/src/state.ts` (zustand)

Add:
- `activeProjectId?: string`
- `setActiveProjectId(id?: string)` — also writes `localStorage`.

Initialize from `localStorage` on store creation.

### `web/frontend/src/AuthGate.tsx` (new)

Wraps `<App />`. Logic:
1. `useQuery(["auth/me"])` → fetches `/api/auth/me`.
2. If `authenticated === false` → render `<LoginForm />` modal:
   - username + password inputs, submit button, inline error message.
   - On success → invalidate `auth/me` (modal disappears).
3. If `authenticated === true` → render `children`.

### `web/frontend/src/App.tsx`

Inside the existing `<section className="toolbar">`:
- Add a `<ProjectPicker />` component (project dropdown).
- Add a `<UserMenu />` showing username and a Logout button.

`<ProjectPicker />`:
- `useQuery(["projects"])` lists user's projects.
- Reads/writes `activeProjectId` in the store.
- On change: store the choice and call `queryClient.invalidateQueries(["jobs"])`
  so the jobs list reloads under the new project.
- Disabled when there's only one project.

`<UserMenu />`:
- Logout → `logout()` then `queryClient.invalidateQueries(["auth/me"])`.

### `web/frontend/src/main.tsx`

Wrap `<App />` in `<AuthGate>`.

### `web/frontend/src/styles.css`

- `.authOverlay` — full-screen dim layer for the modal.
- `.loginCard` — centered card with username/password/submit.
- `.userMenu`, `.projectPicker` — header bits.

## Tests

- `tests/web/test_migration_integration.py`:
  - `test_projects_endpoint_lists_user_memberships`
  - `test_x_project_id_header_filters_jobs` — create 2 projects for the local
    user, post jobs into each via header, verify `list_jobs` returns only the
    selected project's jobs.
  - `test_x_project_id_header_rejects_non_member` — 403 when header refers to
    a project the user isn't a member of.

## Out of scope (deferred)

- Project creation/rename UI.
- Inviting other users to a project / role management.
- Session-stored active project (header-based is sufficient).
- Project-scoped URL paths (`/api/projects/{id}/jobs/...`) — Step 3+ in
  `MULIT_USER_FEATURE.md`.

## File touch list

- `src/canyongis/web/server.py` (modify)
- `web/frontend/src/api.ts` (modify)
- `web/frontend/src/state.ts` (modify)
- `web/frontend/src/App.tsx` (modify)
- `web/frontend/src/main.tsx` (modify)
- `web/frontend/src/styles.css` (modify)
- `web/frontend/src/AuthGate.tsx` (new)
- `tests/web/test_migration_integration.py` (modify)
