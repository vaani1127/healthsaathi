const KEY = "hs.device";

/** A random id for this browser, used only to tell a user's devices apart in their sessions list. */
export function deviceId(): string {
  try {
    let id = localStorage.getItem(KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(KEY, id);
    }
    return id;
  } catch {
    return "unknown";
  }
}
