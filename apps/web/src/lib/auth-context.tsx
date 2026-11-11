"use client";

import React, { createContext, useContext, useState } from "react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  apiFetch,
  getStoredToken,
  setStoredToken,
  User,
} from "@/lib/api";

interface AuthContextValue {
  user: User | null;
  isLoading: boolean;
  setSession: (token: string, user: User) => void;
  signOut: () => void;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function AuthProviderInner({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [hasToken, setHasToken] = useState<boolean>(() =>
    Boolean(getStoredToken())
  );

  const { data: user, isLoading } = useQuery<User | null>({
    queryKey: ["auth", "me"],
    queryFn: async () => {
      const token = getStoredToken();
      if (!token) return null;
      try {
        return await apiFetch<User>("/auth/me");
      } catch {
        setStoredToken(null);
        setHasToken(false);
        return null;
      }
    },
    enabled: hasToken,
    retry: false,
  });

  const setSession = (token: string, nextUser: User) => {
    setStoredToken(token);
    setHasToken(true);
    queryClient.setQueryData(["auth", "me"], nextUser);
  };

  const signOut = () => {
    setStoredToken(null);
    setHasToken(false);
    queryClient.clear();
  };

  const refreshUser = async () => {
    await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
  };

  return (
    <AuthContext.Provider
      value={{
        user: hasToken ? user ?? null : null,
        isLoading: hasToken ? isLoading : false,
        setSession,
        signOut,
        refreshUser,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProviderInner>{children}</AuthProviderInner>
    </QueryClientProvider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside Providers");
  }
  return ctx;
}
