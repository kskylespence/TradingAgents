import { useAuth } from "@/hooks/useAuth";

export function useIsAdmin(): boolean {
  const { user } = useAuth();
  return user?.role === "admin";
}
