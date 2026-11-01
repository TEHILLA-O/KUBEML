import { FormEvent, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { Shell } from "./components/Shell";
import { useApp } from "./store";
import type { User } from "./types";
import { ClusterWorkspace } from "./workspaces/Cluster";
import { DriftWorkspace } from "./workspaces/Drift";
import { LabWorkspace } from "./workspaces/Lab";
import { MissionWorkspace } from "./workspaces/Mission";
import { PolicyWorkspace } from "./workspaces/Policy";
import { RegistryWorkspace } from "./workspaces/Registry";
import { ReliabilityWorkspace } from "./workspaces/Reliability";
import { ReleaseWorkspace } from "./workspaces/Releases";
import { TimelineWorkspace } from "./workspaces/Timeline";

export function App() {
  const user = useApp((state) => state.user);
  if (!user) return <Login />;
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<MissionWorkspace />} />
        <Route path="lab" element={<LabWorkspace />} />
        <Route path="registry" element={<RegistryWorkspace />} />
        <Route path="releases" element={<ReleaseWorkspace />} />
        <Route path="drift" element={<DriftWorkspace />} />
        <Route path="reliability" element={<ReliabilityWorkspace />} />
        <Route path="cluster" element={<ClusterWorkspace />} />
        <Route path="timeline" element={<TimelineWorkspace />} />
        <Route path="policy" element={<PolicyWorkspace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

function Login() {
  const signIn = useApp((state) => state.signIn);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("kubeml-local");
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const result = await api<{ token: string; user: User }>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      signIn(result.token, result.user);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed.");
    }
  }

  return (
    <main className="login">
      <section className="login-copy">
        <p className="kicker">Model reliability platform</p>
        <h1 className="mt-3 max-w-xl text-5xl font-medium leading-tight">Which model is serving, and what evidence says it should stay there.</h1>
        <p className="mt-4 max-w-lg text-muted">Local sign-in issues a real token. The seeded passwords are for this machine only.</p>
      </section>
      <form className="login-panel" onSubmit={submit}>
        <h2 className="mb-4 text-xl">Enter the control room</h2>
        <label className="field">Username<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label>
        <label className="field">Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
        {error && <p className="mb-3 text-sm text-bad">{error}</p>}
        <button className="primary w-full" type="submit">Sign in</button>
        <div className="mt-4 grid grid-cols-2 gap-2">
          {["admin", "engineer", "approver", "observer"].map((role) => (
            <button key={role} className="quiet" type="button" onClick={() => { setUsername(role); setPassword("kubeml-local"); }}>
              Use {role}
            </button>
          ))}
        </div>
      </form>
    </main>
  );
}
