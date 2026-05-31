// Production build: the app is served by nginx, which reverse-proxies /api to
// the backend on the same origin. Keeping this relative avoids hardcoding the
// backend host and removes the need for CORS.
export const environment = {
  apiUrl: '/api'
};
