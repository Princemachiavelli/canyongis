import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAuthMe, login } from "./api";
import { useAppStore } from "./state";

type AuthGateProps = {
  children: React.ReactNode;
};

function LoginForm({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const loginMutation = useMutation({
    mutationFn: async () => {
      const result = await login(username, password);
      useAppStore.getState().setAuthenticatedUser(result.user);
    },
    onSuccess: onLogin,
    onError: (err: Error) => setError(err.message),
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    loginMutation.mutate();
  };

  return (
    <div className="authOverlay">
      <div className="loginCard">
        <h2>Canyongis Login</h2>
        <form onSubmit={handleSubmit}>
          <label>
            Username
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoFocus
            />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && <div className="error">{error}</div>}
          <button
            type="submit"
            className="primary"
            disabled={loginMutation.isPending}
          >
            {loginMutation.isPending ? "Logging in..." : "Login"}
          </button>
        </form>
      </div>
    </div>
  );
}

export function AuthGate({ children }: AuthGateProps) {
  const queryClient = useQueryClient();
  const setAuthenticatedUser = useAppStore(
    (state) => state.setAuthenticatedUser,
  );

  const authMeQuery = useQuery({
    queryKey: ["auth/me"],
    queryFn: getAuthMe,
    retry: false,
  });

  if (authMeQuery.isLoading) {
    return (
      <div className="authOverlay">
        <div className="loginCard">
          <p>Loading...</p>
        </div>
      </div>
    );
  }

  if (authMeQuery.error) {
    return (
      <div className="authOverlay">
        <div className="loginCard">
          <h2>Connection Error</h2>
          <p className="error">{authMeQuery.error.message}</p>
          <button
            onClick={() =>
              queryClient.invalidateQueries({ queryKey: ["auth/me"] })
            }
            className="primary"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  const data = authMeQuery.data!;

  if (data.authenticated && data.user) {
    setAuthenticatedUser(data.user);
    return <>{children}</>;
  }

  // Single-user mode auto-passes the gate
  if (data.single_user && data.user) {
    setAuthenticatedUser(data.user);
    return <>{children}</>;
  }

  return (
    <LoginForm
      onLogin={() => queryClient.invalidateQueries({ queryKey: ["auth/me"] })}
    />
  );
}
