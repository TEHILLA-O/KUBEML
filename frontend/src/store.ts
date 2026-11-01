import { create } from "zustand";
import type { User } from "./types";

type State = {
  user: User | null;
  theme: "dark" | "light";
  palette: boolean;
  selectedNode: string;
  signIn: (token: string, user: User) => void;
  signOut: () => void;
  setTheme: (theme: "dark" | "light") => void;
  setPalette: (open: boolean) => void;
  setSelectedNode: (id: string) => void;
};

const storedUser = sessionStorage.getItem("kubeml.user");

export const useApp = create<State>((set) => ({
  user: storedUser ? (JSON.parse(storedUser) as User) : null,
  theme: localStorage.getItem("kubeml.theme") === "light" ? "light" : "dark",
  palette: false,
  selectedNode: "inference",
  signIn: (token, user) => {
    sessionStorage.setItem("kubeml.token", token);
    sessionStorage.setItem("kubeml.user", JSON.stringify(user));
    set({ user });
  },
  signOut: () => {
    sessionStorage.removeItem("kubeml.token");
    sessionStorage.removeItem("kubeml.user");
    set({ user: null });
  },
  setTheme: (theme) => {
    localStorage.setItem("kubeml.theme", theme);
    document.documentElement.dataset.theme = theme;
    set({ theme });
  },
  setPalette: (palette) => set({ palette }),
  setSelectedNode: (selectedNode) => set({ selectedNode }),
}));
