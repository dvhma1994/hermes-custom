import { lazy, type LazyExoticComponent, type ComponentType } from "react";

/**
 * Central route registry for the dashboard SPA.
 *
 * Every page is loaded with React.lazy so Vite can split it into a
 * separate chunk.  Keep the import paths narrow and avoid adding
 * side-effect imports here.
 */

export const LazyConfigPage = lazy(() => import("@/pages/ConfigPage"));
export const LazyDocsPage = lazy(() => import("@/pages/DocsPage"));
export const LazyEnvPage = lazy(() => import("@/pages/EnvPage"));
export const LazyFilesPage = lazy(() => import("@/pages/FilesPage"));
export const LazySessionsPage = lazy(() => import("@/pages/SessionsPage"));
export const LazyLogsPage = lazy(() => import("@/pages/LogsPage"));
export const LazyAnalyticsPage = lazy(() => import("@/pages/AnalyticsPage"));
export const LazyModelsPage = lazy(() => import("@/pages/ModelsPage"));
export const LazyCronPage = lazy(() => import("@/pages/CronPage"));
export const LazyProfilesPage = lazy(() => import("@/pages/ProfilesPage"));
export const LazyProfileBuilderPage = lazy(() => import("@/pages/ProfileBuilderPage"));
export const LazySkillsPage = lazy(() => import("@/pages/SkillsPage"));
export const LazyPluginsPage = lazy(() => import("@/pages/PluginsPage"));
export const LazyMcpPage = lazy(() => import("@/pages/McpPage"));
export const LazyPairingPage = lazy(() => import("@/pages/PairingPage"));
export const LazyChannelsPage = lazy(() => import("@/pages/ChannelsPage"));
export const LazyWebhooksPage = lazy(() => import("@/pages/WebhooksPage"));
export const LazySystemPage = lazy(() => import("@/pages/SystemPage"));
export const LazyChatPage = lazy(() => import("@/pages/ChatPage"));

export interface DashboardRoute {
  path: string;
  element: LazyExoticComponent<ComponentType<any>>;
  key: string;
}

export const dashboardRoutes: DashboardRoute[] = [
  { path: "/config", element: LazyConfigPage, key: "config" },
  { path: "/docs", element: LazyDocsPage, key: "docs" },
  { path: "/env", element: LazyEnvPage, key: "env" },
  { path: "/files", element: LazyFilesPage, key: "files" },
  { path: "/sessions", element: LazySessionsPage, key: "sessions" },
  { path: "/logs", element: LazyLogsPage, key: "logs" },
  { path: "/analytics", element: LazyAnalyticsPage, key: "analytics" },
  { path: "/models", element: LazyModelsPage, key: "models" },
  { path: "/cron", element: LazyCronPage, key: "cron" },
  { path: "/profiles", element: LazyProfilesPage, key: "profiles" },
  { path: "/profile-builder", element: LazyProfileBuilderPage, key: "profile-builder" },
  { path: "/skills", element: LazySkillsPage, key: "skills" },
  { path: "/plugins/*", element: LazyPluginsPage, key: "plugins" },
  { path: "/mcp", element: LazyMcpPage, key: "mcp" },
  { path: "/pairing", element: LazyPairingPage, key: "pairing" },
  { path: "/channels", element: LazyChannelsPage, key: "channels" },
  { path: "/webhooks", element: LazyWebhooksPage, key: "webhooks" },
  { path: "/system", element: LazySystemPage, key: "system" },
  { path: "/chat", element: LazyChatPage, key: "chat" },
];
