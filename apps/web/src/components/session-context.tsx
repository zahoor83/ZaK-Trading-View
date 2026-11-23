"use client";

import {
  createContext,
  useContext,
  type ReactNode,
} from "react";

export type WorkspaceUser = {
  id: string;
  email: string;
};

const SessionContext = createContext<WorkspaceUser | null>(null);

export function SessionProvider({
  user,
  children,
}: {
  user: WorkspaceUser;
  children: ReactNode;
}) {
  return (
    <SessionContext.Provider value={user}>
      {children}
    </SessionContext.Provider>
  );
}

export function useWorkspaceUser(): WorkspaceUser {
  const user = useContext(SessionContext);

  if (!user) {
    throw new Error("Authenticated workspace session is missing.");
  }

  return user;
}