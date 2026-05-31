# ---- Build stage: compile the Angular app to static files ----
FROM node:22-alpine AS build

WORKDIR /app

# Install dependencies first for better layer caching
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Production config applies fileReplacements -> environment.production.ts (apiUrl: /api)
RUN npm run build -- --configuration production

# ---- Serve stage: nginx serves the static bundle + proxies /api to the backend ----
FROM nginx:alpine

# The @angular-devkit application builder emits the browser bundle here
COPY --from=build /app/dist/frontend/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
