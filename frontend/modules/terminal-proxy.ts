import { defineNuxtModule } from '@nuxt/kit'
import { createProxyServer } from 'httpxy'
import type { IncomingMessage } from 'node:http'
import type { Socket } from 'node:net'

// Nitro's devProxy handles HTTP requests, but upgrades otherwise reach its
// worker and receive the SPA HTML instead of the authenticated FastAPI stream.
export default defineNuxtModule({
  setup(_options, nuxt) {
    if (!nuxt.options.dev) return
    nuxt.hook('ready', () => {
      const server = nuxt.server
      if (!server) return
      const fallback = server.upgrade.bind(server)
      const proxy = createProxyServer({})
      const target = new URL(process.env.NUXT_API_PROXY_TARGET ?? 'http://127.0.0.1:8765/api').origin
      server.upgrade = (req: IncomingMessage, socket: Socket, head: Buffer) => {
        if (/^\/api\/v2\/settings\/claude-terminal\/sessions\/[0-9a-f-]+\/stream$/.test(req.url ?? '')) {
          return proxy.ws(req, socket, { target, changeOrigin: true }, head).catch(() => {
            socket.destroy()
          })
        }
        return fallback(req, socket, head)
      }
    })
  },
})
