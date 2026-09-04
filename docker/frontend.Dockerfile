# syntax=docker/dockerfile:1

# The Nuxt frontend image, added in Phase 3 (docs/IMPLEMENTATION_PLAN.md). It runs
# `nuxt dev`, and the image says so rather than pretending otherwise: dev
# dependencies are installed and the sources are bind-mounted over it by
# docker-compose.yml, so the composition serves the app with HMR against the `api`
# service.
#
# There is deliberately no production stage. `ssr: false` (frontend/nuxt.config.ts)
# means the shipped artifact is a static bundle, and server/app.py already mounts
# one — `<dist>/assets` at `/assets`, with an index.html fallback — so production
# keeps V1's single-origin model: one FastAPI process serving `nuxt generate`
# output, no Node runtime at all. An image whose only job was to hold those files
# would introduce a second origin, a second port and a CORS story the deployment
# does not have.
#
# The build context is the repository root (not `frontend/`) so that one
# .dockerignore governs both images; it is what keeps `.env`, `data/` and `cv/`
# out of the context, and every COPY below names `frontend/` explicitly.
ARG NODE_VERSION=22
FROM node:${NODE_VERSION}-slim

# `development` is what `nuxt dev` expects, and it is also what makes `npm ci`
# install devDependencies (vitest, playwright, vue-tsc) instead of skipping them.
ENV NODE_ENV=development
# A build has no business making an outbound analytics request.
ENV NUXT_TELEMETRY_DISABLED=1

WORKDIR /app

# Manifests first, so editing a component does not reinstall node_modules.
# `.npmrc` is part of the install input, not an afterthought: it carries
# `legacy-peer-deps=true`, without which `npm ci` crashes on this dependency set
# (frontend/.npmrc records the npm bug and the peer-range audit behind it).
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./

# `npm ci`, never `npm install`: the lockfile is the contract, and a container
# that quietly resolved different versions than the host would make any
# reproduction meaningless. `--ignore-scripts` because `postinstall` is `nuxt
# prepare`, which needs nuxt.config.ts and the app sources — not copied yet.
RUN npm ci --ignore-scripts

COPY frontend/ ./

# Generates .nuxt/ (types, tsconfig, module manifest) at build time, so the
# container starts serving instead of preparing.
RUN npx nuxt prepare

# node:slim ships uid/gid 1000 as `node`. The tree is handed to it because
# `nuxt dev` writes .nuxt/ and node_modules/.vite while running; nothing here
# needs root, and the ownership is inherited by the named volumes the composition
# mounts over those two paths.
RUN chown -R node:node /app
USER node

EXPOSE 3000

# `--host 0.0.0.0`: Nuxt binds loopback by default, which inside a container means
# the published port reaches nothing. Who may connect is decided by the
# composition, which publishes to 127.0.0.1 on the host.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
