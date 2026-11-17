// Asynchronous AJAX API client for Decision-IQ canvas
export async function fetchCanvasGraph(roadmapId) {
  const response = await fetch(`/api/canvas/${roadmapId}`, {
    method: 'GET',
    headers: { 'Accept': 'application/json' }
  });
  return await response.json();
}
