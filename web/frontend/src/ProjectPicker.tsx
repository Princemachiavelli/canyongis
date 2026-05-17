import { useEffect, useMemo, useState, type ChangeEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createProject, deleteProject, listProjects, updateProject, type Project } from "./api";
import { useAppStore } from "./state";

export function ProjectPicker({
  queryClient: qc,
}: {
  queryClient: ReturnType<typeof useQueryClient>;
}) {
  const { data: projects } = useQuery<Project[]>({
    queryKey: ["projects"],
    queryFn: listProjects,
    staleTime: 60_000,
  });
  const activeProjectId = useAppStore((state) => state.activeProjectId);
  const setActiveProjectId = useAppStore((state) => state.setActiveProjectId);
  const activeProject = useMemo(
    () => projects?.find((project) => project.id === activeProjectId),
    [activeProjectId, projects],
  );
  const [draftName, setDraftName] = useState("");

  const refreshProjectData = () => {
    qc.invalidateQueries({ queryKey: ["projects"] });
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["project-assets"] });
    qc.invalidateQueries({ queryKey: ["project-pipeline"] });
    qc.invalidateQueries({ queryKey: ["asset-library"] });
  };

  const createMutation = useMutation({
    mutationFn: (name: string) => createProject(name),
    onSuccess: (project) => {
      setActiveProjectId(project.id);
      setDraftName(project.name);
      refreshProjectData();
    },
  });
  const updateMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => updateProject(id, name),
    onSuccess: (project) => {
      setDraftName(project.name);
      refreshProjectData();
    },
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProject(id),
    onSuccess: () => {
      setActiveProjectId(undefined);
      setDraftName("");
      refreshProjectData();
    },
  });

  const handleChange = (e: ChangeEvent<HTMLSelectElement>) => {
    const id = e.target.value || undefined;
    setActiveProjectId(id);
    refreshProjectData();
  };

  const trimmedName = draftName.trim();
  const busy = createMutation.isPending || updateMutation.isPending || deleteMutation.isPending;
  const canSave = Boolean(trimmedName && activeProject && trimmedName !== activeProject.name);
  const saveProject = () => {
    if (!trimmedName) return;
    if (activeProject) {
      updateMutation.mutate({ id: activeProject.id, name: trimmedName });
    } else {
      createMutation.mutate(trimmedName);
    }
  };
  const newProject = () => {
    createMutation.mutate(trimmedName || "Untitled project");
  };
  const removeProject = () => {
    if (!activeProject || !window.confirm(`Delete project "${activeProject.name}"?`)) return;
    deleteMutation.mutate(activeProject.id);
  };

  useEffect(() => {
    if (!projects || projects.length === 0) return;
    if (activeProjectId && projects.some((p) => p.id === activeProjectId)) return;
    setActiveProjectId(projects[0].id);
    refreshProjectData();
  }, [activeProjectId, projects, qc, setActiveProjectId]);

  useEffect(() => {
    setDraftName(activeProject?.name ?? "");
  }, [activeProject?.id, activeProject?.name]);

  return (
    <div className="projectPicker">
      <select
        value={activeProjectId || ""}
        onChange={handleChange}
        disabled={!projects || projects.length === 0 || busy}
      >
        {(!projects || projects.length === 0) && <option value="">No project</option>}
        {projects?.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      <input
        aria-label="Project label"
        placeholder="Project label"
        value={draftName}
        onChange={(event) => setDraftName(event.target.value)}
        disabled={busy}
      />
      <button type="button" onClick={saveProject} disabled={busy || !canSave}>
        Save
      </button>
      <button type="button" onClick={newProject} disabled={busy}>
        New
      </button>
      <button
        type="button"
        className="danger"
        onClick={removeProject}
        disabled={busy || !activeProject}
      >
        Delete
      </button>
    </div>
  );
}
