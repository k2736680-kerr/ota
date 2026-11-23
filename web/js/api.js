async function api(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch (err) { body = { raw: text }; }
  if (!response.ok && body.message === undefined) {
    body.message = response.statusText;
  }
  body._status = response.status;
  return body;
}

function postJson(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
}
