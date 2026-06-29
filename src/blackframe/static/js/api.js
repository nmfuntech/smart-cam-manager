function getCsrfToken() {
  return document
    .querySelector('meta[name="blackframe-csrf-token"]')
    ?.getAttribute("content") || "";
}

let authRedirectInFlight = false;

function isLoginUrl(url) {
  try {
    return new URL(url, window.location.origin).pathname === "/login";
  } catch {
    return false;
  }
}

export function redirectToLogin(targetUrl) {
  if (authRedirectInFlight) {
    return;
  }
  authRedirectInFlight = true;
  window.dispatchEvent(new CustomEvent("blackframe:auth-required"));
  window.location.href = targetUrl;
}

export async function fetchJson(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = getCsrfToken();
    if (csrfToken && !headers.has("X-CSRF-Token")) {
      headers.set("X-CSRF-Token", csrfToken);
    }
  }

  const response = await fetch(url, {
    cache: "no-store",
    credentials: "same-origin",
    redirect: "manual",
    ...options,
    headers,
  });
  if (
    response.type === "opaqueredirect"
    || (response.status >= 300 && response.status < 400)
  ) {
    redirectToLogin("/login");
    throw new Error("Authentication required");
  }
  if (response.redirected && isLoginUrl(response.url)) {
    redirectToLogin(response.url);
    throw new Error("Authentication required");
  }
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : {};
  if (response.status === 401 && data.redirect) {
    redirectToLogin(data.redirect);
    throw new Error("Authentication required");
  }
  return { response, data };
}

export async function postJson(url) {
  return fetchJson(url, { method: "POST" });
}

export async function fetchBlobUrl(url) {
  const response = await fetch(url, {
    cache: "no-store",
    credentials: "same-origin",
    redirect: "manual",
  });
  if (
    response.type === "opaqueredirect"
    || (response.status >= 300 && response.status < 400)
  ) {
    redirectToLogin("/login");
    throw new Error("Authentication required");
  }
  if (response.redirected && isLoginUrl(response.url)) {
    redirectToLogin(response.url);
    throw new Error("Authentication required");
  }
  if (response.status === 401) {
    const redirect = response.headers.get("X-Auth-Redirect") || "/login";
    redirectToLogin(redirect);
    throw new Error("Authentication required");
  }
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}
