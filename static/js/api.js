export async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  return { response, data };
}

export async function postJson(url) {
  return fetchJson(url, { method: "POST" });
}
