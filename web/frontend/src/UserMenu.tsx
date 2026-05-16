import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { logout } from "./api";
import { useAppStore } from "./state";

export function UserMenu({
  queryClient: qc,
}: {
  queryClient: ReturnType<typeof useQueryClient>;
}) {
  const user = useAppStore((state) => state.authenticatedUser);
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await logout();
      useAppStore.getState().setAuthenticatedUser(undefined);
      qc.invalidateQueries({ queryKey: ["auth/me"] });
    } catch {
      setLoggingOut(false);
    }
  };

  if (!user) return null;

  return (
    <div className="userMenu">
      <span className="username">{user.username}</span>
      <button disabled={loggingOut} onClick={handleLogout}>
        {loggingOut ? "..." : "Logout"}
      </button>
    </div>
  );
}
