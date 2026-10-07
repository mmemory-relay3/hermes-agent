// "This device" while the primary is remote: one forced-local backend per decision of
// `forcedLocalBackend()` (host-backend-singleton.ts). Every profile joins the ONE pinned host
// backend, scoped per socket/request; only escape hatches (isolated backend, unscopable REST
// mutation) get a process of their own. The pinned host is never a passive-read refusal or an
// LRU victim: it hosts every local profile's cron and bot work.
//
// Dependency-injected so the join/spawn phase is testable without Electron; main.ts wires the
// real pool, stopper, spawner and reaper.

import { forcedLocalBackend, scopeHostDescriptor } from './host-backend-singleton'
import type { LocalBackendSpawnPriority } from './pool-spawn-coordinator'

export interface ForcedLocalPoolEntry {
  connectionPromise: null | Promise<any>
  lastActiveAt: number
  pinned?: boolean
  [key: string]: unknown
}

export interface ForcedLocalBackendDeps {
  pool: Map<string, ForcedLocalPoolEntry>
  isolated: boolean
  inFlightStop: (poolKey: string) => Promise<unknown> | null | undefined
  promote: (entry: ForcedLocalPoolEntry) => void
  assertNotPassiveSpawn: (passive: boolean, poolKey: string) => void
  evictLru: () => Promise<unknown>
  /** True when the registry changed or another dial filled the key while we waited. */
  staleAfterWait: (poolKey: string) => boolean
  retry: () => Promise<any>
  spawn: (
    profile: string,
    entry: ForcedLocalPoolEntry,
    opts: { poolKey: string; unscopableRequest?: boolean }
  ) => Promise<any>
  onSpawnFailure: (label: string, error: unknown, poolKey: string, entry: ForcedLocalPoolEntry) => Promise<void>
  armReaper: () => void
}

export interface ForcedLocalBackendRequest {
  profileKey: string
  profilePoolKey: string
  passive: boolean
  spawnPriority: LocalBackendSpawnPriority
  unscopableRequest?: boolean
}

export async function ensureForcedLocalBackend(deps: ForcedLocalBackendDeps, req: ForcedLocalBackendRequest) {
  const local = forcedLocalBackend(req.profileKey, req.profilePoolKey, {
    isolated: deps.isolated,
    unscopableRequest: req.unscopableRequest
  })

  const descriptorFor = async (connectionPromise: Promise<any>) =>
    local.pinned ? scopeHostDescriptor(await connectionPromise, req.profileKey) : connectionPromise

  const stopping = deps.inFlightStop(local.poolKey)

  if (stopping) {
    await stopping
  }

  const existing = deps.pool.get(local.poolKey)

  if (existing?.connectionPromise) {
    if (!req.passive) {
      existing.lastActiveAt = Date.now()
    }

    if (req.spawnPriority === 'foreground') {
      deps.promote(existing)
    }

    return descriptorFor(existing.connectionPromise)
  }

  if (!local.pinned) {
    deps.assertNotPassiveSpawn(req.passive, local.poolKey)
    await deps.evictLru()
  }

  // Never start a child from a removed or edited connection's old descriptor.
  if (deps.staleAfterWait(local.poolKey)) {
    return deps.retry()
  }

  const entry: ForcedLocalPoolEntry = {
    process: null,
    port: null,
    token: null,
    connectionPromise: null,
    lastActiveAt: Date.now(),
    pinned: local.pinned,
    remoteBaseUrl: null,
    releaseLocalBackendSlot: null,
    localBackendSlotKey: null,
    localBackendSpawnRequest: null,
    spawnPriority: req.spawnPriority
  }

  const connectionPromise = deps
    .spawn(local.spawnProfile, entry, { poolKey: local.poolKey, unscopableRequest: req.unscopableRequest })
    .catch(async error => {
      // A forced-local child whose spawn rejects before the child exists must still be logged.
      await deps.onSpawnFailure(`"${local.spawnProfile}" (forced-local)`, error, local.poolKey, entry)
      throw error
    })

  entry.connectionPromise = connectionPromise
  deps.pool.set(local.poolKey, entry)
  deps.armReaper()

  return descriptorFor(connectionPromise)
}
