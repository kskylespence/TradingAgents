import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Suspense, lazy } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";

import { AnnouncementBanner } from "@/components/AnnouncementBanner";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Button } from "@/components/ui/button";
import { Toaster } from "@/components/ui/toaster";
import { useAuth } from "@/hooks/useAuth";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { cn } from "@/lib/utils";

const Login = lazy(() => import("@/routes/Login"));
const NewRun = lazy(() => import("@/routes/NewRun"));
const RunView = lazy(() => import("@/routes/RunView"));
const History = lazy(() => import("@/routes/History"));
const Settings = lazy(() => import("@/routes/Settings"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function NavBar() {
  const { user, logout } = useAuth();
  const isAdmin = useIsAdmin();
  if (!user) return null;

  const linkCls = ({ isActive }: { isActive: boolean }) =>
    cn(
      "text-sm font-medium transition-colors hover:text-emerald-400",
      isActive ? "text-emerald-400" : "text-muted-foreground",
    );

  return (
    <header className="sticky top-0 z-40 border-b border-emerald-900/40 bg-[hsl(var(--terminal-bg))]/95 backdrop-blur">
      <div className="container flex h-14 items-center gap-6">
        <div className="flex flex-col leading-none">
          <div className="font-semibold tracking-tight text-emerald-400">
            HedgeFund Terminal
          </div>
          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
            Multi-agent analysis
          </div>
        </div>
        <nav className="flex items-center gap-4">
          <NavLink to="/new" className={linkCls}>
            New
          </NavLink>
          <NavLink to="/history" className={linkCls}>
            History
          </NavLink>
          {isAdmin ? (
            <NavLink to="/settings" className={linkCls}>
              Settings
            </NavLink>
          ) : null}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-sm text-muted-foreground">
          <span className="font-mono text-xs uppercase tracking-wide">
            {user.username}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              void logout();
            }}
          >
            Sign out
          </Button>
        </div>
      </div>
    </header>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-svh flex-col bg-[hsl(var(--terminal-bg))]">
      <NavBar />
      <AnnouncementBanner />
      <main className="flex-1">{children}</main>
    </div>
  );
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const isAdmin = useIsAdmin();
  if (!isAdmin) {
    return <Navigate to="/new" replace />;
  }
  return <>{children}</>;
}

function AppRoutes() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-svh items-center justify-center text-muted-foreground">
          Loading...
        </div>
      }
    >
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/new"
          element={
            <ProtectedRoute>
              <Layout>
                <NewRun />
              </Layout>
            </ProtectedRoute>
          }
        />
        <Route
          path="/runs/:runId"
          element={
            <ProtectedRoute>
              <Layout>
                <RunView />
              </Layout>
            </ProtectedRoute>
          }
        />
        <Route
          path="/history"
          element={
            <ProtectedRoute>
              <Layout>
                <History />
              </Layout>
            </ProtectedRoute>
          }
        />
        <Route
          path="/settings"
          element={
            <ProtectedRoute>
              <Layout>
                <AdminRoute>
                  <Settings />
                </AdminRoute>
              </Layout>
            </ProtectedRoute>
          }
        />
        <Route
          path="*"
          element={
            <ProtectedRoute>
              <Layout>
                <NewRun />
              </Layout>
            </ProtectedRoute>
          }
        />
      </Routes>
    </Suspense>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
        <Toaster />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
