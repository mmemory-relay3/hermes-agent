/**
 * Remote primary + "This device" profiles (support f502bc6f): the Desktop's PRIMARY is a remote
 * backend, and the user also works with several LOCAL profiles through the registry's `local`
 * entry. Each local profile used to get its own `hermes serve` child: three filled the 3-slot pool
 * ("2/3 busy, 1 queued"), and the idle reaper later SIGTERM'd the one the user was chatting on,
 * leaving "This device · Backend offline".
 *
 * Contract: every local profile rides ONE local host backend, each socket scoped to its own
 * profile (`/api/ws?profile=`) so its sessions land in its own home, and that backend is never
 * idle-retired (it hosts cron and bot work nobody is watching).
 */

import * as fs from 'node:fs'
import * as path from 'node:path'

import { expect, test } from '@playwright/test'

import {
  backendProcesses,
  coreAppEnv,
  createCoreSandbox,
  launchCoreApp,
  recordWebSockets,
  storedSessionForMarker,
  waitForInteractive,
  writeProviderHome
} from './harness'
import { startScriptedProvider } from './provider'
import { startRemoteBackend } from './remote-helpers'

const REMOTE_ID = 'cloudhost'
const LOCAL_PROFILES = ['default', 'reviewer', 'calendar-demo', 'gg-probe']

const NONCE = Math.random()
  .toString(36)
  .slice(2, 8)
  .replace(/[^a-z0-9]/g, 'x')
  .padEnd(4, 'q')

const IDLE_MS = 60_000 // the pool's floor; the reaper ticks every 60s

function writeRemotePrimaryRegistry(userDataDir: string, url: string, token: string): void {
  fs.writeFileSync(
    path.join(userDataDir, 'connections.json'),
    JSON.stringify({
      version: 2,
      primary: REMOTE_ID,
      launchMode: 'primary',
      lastUsed: REMOTE_ID,
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        {
          id: REMOTE_ID,
          kind: 'remote',
          label: 'Cloud',
          url,
          authMode: 'token',
          token: { encoding: 'plain', value: token }
        }
      ]
    }),
    { encoding: 'utf8', mode: 0o600 }
  )
}

test('remote primary: every local profile shares ONE pinned local backend, scoped per socket', async () => {
  test.setTimeout(420_000)
  const provider = await startScriptedProvider()
  const remoteBox = createCoreSandbox('rp-remote')
  const clientBox = createCoreSandbox('rp-client')
  writeProviderHome(remoteBox.hermesHome, provider.url)
  writeProviderHome(clientBox.hermesHome, provider.url)

  for (const profile of LOCAL_PROFILES.slice(1)) {
    writeProviderHome(path.join(clientBox.hermesHome, 'profiles', profile), provider.url)
  }

  const remote = await startRemoteBackend(remoteBox)
  fs.mkdirSync(clientBox.userDataDir, { recursive: true })
  writeRemotePrimaryRegistry(clientBox.userDataDir, remote.url, remote.token)

  const { app, page } = await launchCoreApp(
    coreAppEnv(clientBox, {
      HERMES_DESKTOP_POOL_IDLE_MS: String(IDLE_MS),
      HERMES_DESKTOP_POOL_PINNED_IDLE_MS: String(IDLE_MS)
    })
  )

  const ws = recordWebSockets(page)

  try {
    await waitForInteractive(app, page)

    // Open every local profile through the registry's `local` entry, the "This device" route.
    const created = await page.evaluate(async profiles => {
      const desktop = (window as any).hermesDesktop
      const out: Record<string, { profile?: string; wsUrl: string; profileName?: string; error?: string }> = {}

      for (const profile of profiles) {
        const conn = await desktop.getConnectionFor({ connectionId: 'local', profile, priority: 'foreground' })
        const minted = await desktop.getGatewayWsUrlFor({ connectionId: 'local', profile })

        if (!minted?.ok) {
          throw new Error(`ws url for ${profile}: ${minted?.error}`)
        }

        const wsUrl: string = minted.wsUrl

        const sock = new WebSocket(wsUrl)

        ;(window as any).__rpSockets = { ...((window as any).__rpSockets || {}), [profile]: sock }
        await new Promise((resolve, reject) => {
          sock.onopen = resolve
          sock.onerror = () => reject(new Error(`socket for ${profile} failed`))
        })

        const reply: any = await new Promise(resolve => {
          sock.addEventListener('message', ev => {
            const msg = JSON.parse(String(ev.data))

            if (msg.id === 'create') {
              resolve(msg)
            }
          })
          sock.send(JSON.stringify({ jsonrpc: '2.0', id: 'create', method: 'session.create', params: { cols: 80 } }))
        })

        out[profile] = {
          profile: conn?.profile,
          wsUrl,
          profileName: reply?.result?.info?.profile_name,
          error: reply?.error?.message
        }
      }

      return out
    }, LOCAL_PROFILES)

    await test.step('one local backend serves all four profiles', async () => {
      const ports = new Set(Object.values(created).map(c => new URL(c.wsUrl).port))
      expect(ports.size, JSON.stringify(created, null, 1)).toBe(1)
      expect(new URL(created.default!.wsUrl).port).not.toBe(String(remote.port))
      expect(backendProcesses(clientBox), 'local `hermes serve` children').toHaveLength(1)
    })

    await test.step("each socket's sessions land in its own profile", async () => {
      for (const profile of LOCAL_PROFILES) {
        expect(created[profile]!.error, profile).toBeUndefined()
        expect(created[profile]!.profileName, profile).toBe(profile)
      }
    })

    await test.step("a real turn on each profile persists in that profile's own state.db", async () => {
      for (const [i, profile] of LOCAL_PROFILES.entries()) {
        provider.script(`U${i + 1}-${NONCE}`, [{ text: [`A${i + 1}-${NONCE}`] }])
      }

      await page.evaluate(
        async ([profiles, nonce]) => {
          const sockets = (window as any).__rpSockets

          await Promise.all(
            (profiles as string[]).map(
              (profile, i) =>
                new Promise<void>((resolve, reject) => {
                  const sock: WebSocket = sockets[profile]
                  let sid = ''
                  sock.addEventListener('message', ev => {
                    const msg = JSON.parse(String(ev.data))

                    if (msg.id === `c${i}`) {
                      sid = msg.result?.session_id

                      if (!sid) {
                        reject(new Error(`${profile}: ${JSON.stringify(msg.error)}`))

                        return
                      }

                      sock.send(
                        JSON.stringify({
                          jsonrpc: '2.0',
                          id: `p${i}`,
                          method: 'prompt.submit',
                          params: { session_id: sid, text: `U${i + 1}-${nonce} hi` }
                        })
                      )
                    } else if (msg.id === `p${i}` && msg.error) {
                      reject(new Error(`${profile} submit: ${msg.error.message}`))
                    } else if (
                      msg.method === 'event' &&
                      msg.params?.type === 'message.complete' &&
                      msg.params?.session_id === sid
                    ) {
                      resolve()
                    }
                  })
                  sock.send(
                    JSON.stringify({ jsonrpc: '2.0', id: `c${i}`, method: 'session.create', params: { cols: 80 } })
                  )
                })
            )
          )
        },
        [LOCAL_PROFILES, NONCE] as const
      )

      for (const [i, profile] of LOCAL_PROFILES.entries()) {
        await expect
          .poll(() => storedSessionForMarker(clientBox, profile, `U${i + 1}-${NONCE}`), { message: `${profile} row` })
          .not.toBeNull()

        for (const other of LOCAL_PROFILES.filter(p => p !== profile)) {
          expect(
            storedSessionForMarker(clientBox, other, `U${i + 1}-${NONCE}`),
            `${profile}'s turn leaked into ${other}`
          ).toBeNull()
        }
      }
    })

    await test.step('the This device session list keeps each row on its own profile', async () => {
      const listed = await page.evaluate(async () => {
        const res = await (window as any).hermesDesktop.api({
          connectionId: 'local',
          path: '/api/profiles/sessions?profile=all&limit=50'
        })

        return (res?.sessions || []).map((s: any) => [s.profile, s.preview || s.title || ''])
      })

      for (const [i, profile] of LOCAL_PROFILES.entries()) {
        const row = listed.find(([, text]: [string, string]) => String(text).includes(`U${i + 1}-${NONCE}`))
        expect(row?.[0], `${profile} row in ${JSON.stringify(listed)}`).toBe(profile)
      }
    })

    await test.step('the shared local backend survives the idle reaper', async () => {
      const pid = backendProcesses(clientBox)[0]!.pid
      // Two reaper ticks past both idle windows with nothing streaming.
      await page.waitForTimeout(IDLE_MS + 2 * 60_000 + 5_000)
      expect(
        backendProcesses(clientBox).map(p => p.pid),
        'the same local backend, never retired'
      ).toEqual([pid])

      const open = await page.evaluate(() =>
        Object.fromEntries(Object.entries((window as any).__rpSockets).map(([k, s]: any) => [k, s.readyState]))
      )

      expect(open).toEqual(Object.fromEntries(LOCAL_PROFILES.map(p => [p, 1])))
      expect(ws.sockets.filter(s => s.closed && new URL(s.url).port !== String(remote.port))).toEqual([])
    })
  } finally {
    await app.close().catch(() => undefined)
    await remote.kill()
    await provider.close()
  }
})
