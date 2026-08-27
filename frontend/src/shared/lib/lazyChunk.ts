/**
 * A lazily-loaded chunk that survives the application being redeployed underneath it.
 *
 * Vite names split chunks by the hash of their contents — `PdfViewer-DleM0HyK.js` — and a
 * new build writes new names and does not keep the old ones. A tab that was open across a
 * deployment is still running the previous main bundle, which asks for the previous chunk
 * name the first time somebody clicks a citation. That file is gone, the `import()`
 * rejects, and React's lazy boundary throws.
 *
 * The failure has an unmistakable shape from the reader's side: everything works until the
 * one feature that is code-split, which then breaks with an error that says nothing about
 * deployments. On an on-premise product that is updated in place while people are working,
 * that is not an edge case — it is what happens every time it is updated.
 *
 * **So a chunk that cannot be fetched reloads the page, once.** The application has been
 * replaced; fetching the replacement is what the reader wants, and it costs them a blink
 * rather than an error they cannot act on. The flag is what stops that from becoming a
 * loop: if the reload did not fix it, the chunk is genuinely unreachable — the server is
 * down, or the network is — and the error is allowed through to be handled as an error.
 *
 * `sessionStorage` rather than a module variable, because the reload discards the module.
 * Wrapped, because a browser with site data blocked throws on access, and a viewer that
 * fails to open over a privacy setting would be a worse bug than the one being fixed.
 */

const RELOADED = "zenith.chunk-reload";

function reloadedAlready(): boolean {
  try {
    return sessionStorage.getItem(RELOADED) !== null;
  } catch {
    // Cannot tell, so assume yes and let the error through rather than risk a reload loop.
    return true;
  }
}

function rememberReload(): void {
  try {
    sessionStorage.setItem(RELOADED, "1");
  } catch {
    // Nothing to do: the reload below still happens, and `reloadedAlready` will return
    // true next time for the same reason it failed here.
  }
}

/** Called once the application has loaded a chunk successfully, so the next stale-tab
    failure is allowed its own reload rather than being mistaken for a loop. */
export function chunkLoaded(): void {
  try {
    sessionStorage.removeItem(RELOADED);
  } catch {
    // Ignored, as above.
  }
}

export function lazyChunk<T>(load: () => Promise<T>): () => Promise<T> {
  return async () => {
    try {
      const module = await load();
      chunkLoaded();
      return module;
    } catch (error) {
      if (reloadedAlready()) throw error;
      rememberReload();
      window.location.reload();
      // Never settles: the page is going away, and resolving with anything here would
      // render a wrong component for the frame before it does.
      return new Promise<T>(() => {});
    }
  };
}
