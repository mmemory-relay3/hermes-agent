// "This device" while the primary is remote: one forced-local backend per decision of
// `forcedLocalBackend()` (host-backend-singleton.ts). Every profile joins the ONE pinned host
// backend, scoped per socket/request; only escape hatches (isolated backend, unscopable REST
// mutation) get a process of their own. The pinned host is never a passive-read refusal or an
// LRU victim, and holds no pool slot: it hosts every local profile's cron and bot work.
//
// Older runtimes ignore a socket's `?profile=` (they predate `socket_profile_default` in
// /api/status) and would run every profile's RPCs in the host's launch home, so against one a
// non-default profile keeps its own process homed at the profile, exactly as before.
//
// Dependency-injected so the join/spawn phase is testable without Electron; main.ts wires the
// real pool, stopper, spawner and reaper.

import { forcedLocalBackend, LOCAL_HOST_POOL_KEY, scopeHostDescriptor } from './host-backend-singleton'
import type { LocalBackendSpawnPriority } from './pool-spawn-coordinator'

export interface ForcedLocalPoolEntry {
  connectionPromise: null | Promise<any>
  lastActiveAt: number
  pinned?: boolean
  /** The host's `/api/status.socket_profile_default`, read once per host generation. */
  socketProfileDefault?: boolean
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
  /** The public `/api/status` of a backend (rejects when unreachable). */
  fetchStatus: (baseUrl: string) => Promise<any>
}

export interface ForcedLocalBackendRequest {
  profileKey: string
  profilePoolKey: string
  passive: boolean
  spawnPriority: LocalBackendSpawnPriority
  unscopableRequest?: boolean
}

type Placement = ReturnType<typeof forcedLocalBackend>

export async function ensureForcedLocalBackend(deps: ForcedLocalBackendDeps, req: ForcedLocalBackendRequest) {
  const local = forcedLocalBackend(req.profileKey, req.profilePoolKey, {
    isolated: deps.isolated,
    unscopableRequest: req.unscopableRequest
  })

  if (!local.pinned) {
    return joinOrSpawn(deps, req, local)
  }

  const host = await joinOrSpawn(deps, req, local)

  // The host launches as `default`: its own home already scopes that profile on any runtime.
  if (req.profileKey === 'default' || (await hostScopesSockets(deps, host))) {
    return scopeHostDescriptor(host, req.profileKey)
  }

  return joinOrSpawn(deps, req, { pinned: false, poolKey: req.profilePoolKey, spawnProfile: req.profileKey })
}

async function hostScopesSockets(deps: ForcedLocalBackendDeps, host: { baseUrl?: string }): Promise<boolean> {
  const entry = deps.pool.get(LOCAL_HOST_POOL_KEY)

  if (typeof entry?.socketProfileDefault === 'boolean') {
    return entry.socketProfileDefault
  }

  const status = await deps.fetchStatus(String(host.baseUrl || ''))
  const supported = status?.socket_profile_default === true

  if (entry) {
    entry.socketProfileDefault = supported
  }

  return supported
}

async function joinOrSpawn(deps: ForcedLocalBackendDeps, req: ForcedLocalBackendRequest, local: Placement) {
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

    return existing.connectionPromise
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

  return connectionPromise
}
