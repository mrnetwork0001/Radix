/**
 * The whole router: two internal routes, `/` (landing) and `/app` (console),
 * switched on pathname in `main.tsx`. Lives in lib/ rather than main.tsx so
 * lazy-loaded chunks can navigate without importing the entry module.
 */

export function isAppPath(pathname: string): boolean {
  return pathname === '/app' || pathname.startsWith('/app/');
}

/** SPA navigation for the internal routes; new-tab clicks still work. */
export function navigate(path: string): void {
  window.history.pushState(null, '', path);
  window.dispatchEvent(new PopStateEvent('popstate'));
}
