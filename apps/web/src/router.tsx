import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";

import { UpdatePrompt } from "@/components/UpdatePrompt";
import { HomePage } from "@/features/home/HomePage";

const rootRoute = createRootRoute({
  component: () => (
    <>
      <Outlet />
      <UpdatePrompt />
    </>
  ),
});

const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: HomePage,
});

const routeTree = rootRoute.addChildren([homeRoute]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
