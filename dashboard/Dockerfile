# Multi-stage: node build -> nginx:alpine.
#
# The build stage never ships. The runtime image is nginx plus a few hundred KB
# of static assets -- no node, no npm, no source, and nothing installable.
#
# BUILD CONTEXT IS THE REPO ROOT, not dashboard/. The image needs
# infra/nginx/dashboard.conf as well as the app, and a Docker build context
# cannot reach above itself. Every COPY below is therefore repo-root relative:
#
#     docker build -f dashboard/Dockerfile .
#
# Compose sets `context: .` with `dockerfile: dashboard/Dockerfile` to match.

# --- build ----------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Manifests first, so `npm ci` is only re-run when dependencies actually change
# rather than on every source edit.
COPY dashboard/package.json dashboard/package-lock.json* ./
# `npm ci` when a lockfile is present (reproducible), `npm install` when it is
# not, so a fresh clone without a committed lockfile still builds.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY dashboard/tsconfig.json dashboard/tsconfig.app.json dashboard/tsconfig.node.json ./
COPY dashboard/vite.config.ts dashboard/index.html ./
COPY dashboard/src ./src

# `npm run build` runs `tsc -b` first, so a type error fails the image build
# rather than shipping a broken bundle.
RUN npm run build

# --- runtime --------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# The stock config would otherwise also listen on :80 and shadow ours.
RUN rm -f /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html

# The vhost is baked in rather than bind-mounted so the image is self-contained;
# compose can still override it with a mount during development.
COPY infra/nginx/dashboard.conf /etc/nginx/conf.d/dashboard.conf

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/nginx-health || exit 1

CMD ["nginx", "-g", "daemon off;"]
