import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "@/lib/api";
import type { AuthUser, LoginRequest } from "@/lib/types";

export const AUTH_QUERY_KEY = ["auth", "me"] as const;

/**
 * Drives every protected route. Calls GET /api/auth/me on mount;
 * unauthenticated requests return 401 → the query resolves to `null`.
 *
 * `login` and `logout` invalidate the query so dependent hooks refetch.
 */
export function useAuth() {
  const queryClient = useQueryClient();

  const meQuery = useQuery({
    queryKey: AUTH_QUERY_KEY,
    queryFn: async (): Promise<AuthUser | null> => {
      try {
        return await api.get<AuthUser>("/api/auth/me");
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null;
        throw err;
      }
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const loginMutation = useMutation({
    mutationFn: (creds: LoginRequest) => api.post<void>("/api/auth/login", creds),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: AUTH_QUERY_KEY });
    },
  });

  const logoutMutation = useMutation({
    mutationFn: () => api.post<void>("/api/auth/logout"),
    onSuccess: async () => {
      queryClient.setQueryData(AUTH_QUERY_KEY, null);
      await queryClient.invalidateQueries({ queryKey: AUTH_QUERY_KEY });
    },
  });

  return {
    user: meQuery.data ?? null,
    isLoading: meQuery.isLoading,
    isAuthenticated: !!meQuery.data,
    error: meQuery.error,
    login: loginMutation.mutateAsync,
    isLoggingIn: loginMutation.isPending,
    loginError: loginMutation.error,
    logout: logoutMutation.mutateAsync,
    isLoggingOut: logoutMutation.isPending,
    refetch: meQuery.refetch,
  };
}
