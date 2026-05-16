import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { listProjects, type Project } from "./api";
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

  const handleChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const id = e.target.value || undefined;
    setActiveProjectId(id);
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["project-assets"] });
  };

  useEffect(() => {
    if (!projects || projects.length === 0) return;
    if (activeProjectId && projects.some((p) => p.id === activeProjectId)) return;
    setActiveProjectId(projects[0].id);
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["project-assets"] });
  }, [activeProjectId, projects, qc, setActiveProjectId]);

  if (!projects || projects.length === 0) return null;

  return (
    <div className="projectPicker">
      <select
        value={activeProjectId || ""}
        onChange={handleChange}
        disabled={projects.length <= 1}
      >
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </div>
  );
}
