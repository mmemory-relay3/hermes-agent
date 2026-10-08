// "This device" behind a remote primary: the join/spawn phase of ensureForcedLocalBackend against an
// in-memory pool. Covers the older-runtime fallback (a host that does not advertise
// `socket_profile_default` would run every profile in its launch home) and the shared-host path.
import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  ensureForcedLocalBackend,
  type ForcedLocalBackendDeps,
  type ForcedLocalPoolEntry
} from './forced-local-backend'
import { LOCAL_HOST_POOL_KEY } from './host-backend-singleton'

function harness(status: Record<string, unknown>) {
  const pool = new Map<string, ForcedLocalPoolEntry>()
  const spawned: string[] = []
  let statusReads = 0

  const deps: ForcedLocalBackendDeps = {
    pool,
    isolated: false,
    inFlightStop: () => null,
    promote: () => undefined,
    assertNotPassiveSpawn: () => undefined,
    evictLru: async () => undefined,
    staleAfterWait: () => false,
    retry: async () => {
      throw new Error('unexpected retry')
    },
    spawn: async (profile, _entry, { poolKey }) => {
      spawned.push(`${poolKey}=${profile}`)

      return { baseUrl: `http://127.0.0.1/${poolKey}`, wsUrl: `ws://127.0.0.1/${poolKey}/api/ws?token=t`, profile }
    },
    onSpawnFailure: async () => undefined,
    armReaper: () => undefined,
    fetchStatus: async () => {
      statusReads += 1

      return status
    }
  }

  const open = (profileKey: string) =>
    ensureForcedLocalBackend(deps, {
      profileKey,
      profilePoolKey: `conn:local::${profileKey}`,
      passive: false,
      spawnPriority: 'foreground'
    })

  return { open, spawned, statusReads: () => statusReads }
}

test('a host that scopes sockets serves every local profile on one backend, read once', async () => {
  const h = harness({ socket_profile_default: true })

  const reviewer = await h.open('reviewer')
  const calendar = await h.open('calendar-demo')

  assert.deepEqual(h.spawned, [`${LOCAL_HOST_POOL_KEY}=default`])
  assert.equal(new URL(reviewer.wsUrl).searchParams.get('profile'), 'reviewer')
  assert.equal(new URL(calendar.wsUrl).searchParams.get('profile'), 'calendar-demo')
  assert.equal(h.statusReads(), 1)
})

test('an older runtime that ignores the socket profile keeps a backend homed at each profile', async () => {
  const h = harness({ version: 'older' })

  const host = await h.open('default')
  const reviewer = await h.open('reviewer')

  // `default` is the host's own launch home; a non-default profile must not ride it.
  assert.equal(new URL(host.wsUrl).searchParams.get('profile'), null)
  assert.equal(reviewer.profile, 'reviewer')
  assert.deepEqual(h.spawned, [`${LOCAL_HOST_POOL_KEY}=default`, 'conn:local::reviewer=reviewer'])
})

test('the pinned host takes no pool slot: with maxBackends 1 a profile mutation child still starts', async () => {
  const { LocalBackendSpawnCoordinator, acquirePoolSpawnSlot, BackgroundSlotRetryBackoff } =
    await import('./pool-spawn-coordinator')

  const coordinator = new LocalBackendSpawnCoordinator(1)

  const slotDeps = {
    coordinator,
    backoff: new BackgroundSlotRetryBackoff(),
    timeoutMs: 50,
    signal: new AbortController().signal,
    maxBackends: () => 1,
    log: () => undefined
  }

  // The host spawn (`pinned`) skips acquirePoolSpawnSlot entirely; the only optional child gets the slot.
  const restChild = {}
  await acquirePoolSpawnSlot(slotDeps, 'local-rest::reviewer', 'reviewer', restChild, 'foreground')
  assert.equal(coordinator.activeCount, 1)
})
