/**
 * Recovery for stale lazy-loaded chunks.
 *
 * Every rebuild stamps new hashed filenames. A page that is already open keeps
 * the old manifest, so its next `import()` asks for a chunk that no longer
 * exists; the server answers 404 with `text/javascript`, the browser tries to
 * parse the body as a module and the route error boundary shows a generic
 * "page failed to load" card. Reloading once picks up the new manifest, so do
 * that automatically instead of making the user press the button.
 */

const RELOAD_FLAG = 'dsh.chunkReloadAt';
const RELOAD_GUARD_MS = 15_000;

/** True when the failure looks like a stale or missing dynamic import. */
export function isChunkLoadError(error: unknown): boolean {
  const message =
    error instanceof Error ? error.message : typeof error === 'string' ? error : '';
  return /ChunkLoadError|Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|asset not found/i.test(
    message,
  );
}

/**
 * Reload once so the page picks up the current bundle.
 *
 * Returns false without reloading when this page already reloaded for the same
 * reason recently, so a genuinely broken build cannot trap the browser in a
 * reload loop — the caller should then show its normal error UI.
 */
export function reloadForFreshBundle(): boolean {
  try {
    const previous = Number(window.sessionStorage.getItem(RELOAD_FLAG) ?? '0');
    if (Number.isFinite(previous) && Date.now() - previous < RELOAD_GUARD_MS) {
      return false;
    }
    window.sessionStorage.setItem(RELOAD_FLAG, String(Date.now()));
  } catch {
    // No sessionStorage (private mode, blocked storage): refuse rather than
    // risk a loop we cannot detect.
    return false;
  }
  window.location.reload();
  return true;
}
