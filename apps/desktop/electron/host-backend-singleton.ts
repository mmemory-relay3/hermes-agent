// Multiplex-only, Desktop half: ONE `hermes serve` per HOST serves every
// profile, so a local profile never gets a backend process of its own.
//
// `backend-discovery.ts` + `host-backend-attach.ts` made the PRIMARY backend
// attach to a backend the host is already running. This module removes the
// other producer of `hermes serve` children: the per-profile backend pool.
// Every local profile now resolves onto the same host backend and carries its
// own `profile` on the wire — the server binds a SESSION to a profile home
// (`session.create {profile}` -> `profile_home`) and a sessionless RPC to the
// explicit `profile` argument (`tui_gateway/server.py::@_profile_scoped`), so
// one process genuinely serves N homes.
//
// Two things are deliberately NOT collapsed:
//   * `HERMES_DESKTOP_ISOLATED_BACKEND=1` — the escape hatch that gives this
//     app a private backend instead of the host's.
//   * Remote / SSH / Cloud backends — a DIFFERENT host. Its ownership proof
//     (`remote-lifecycle.ts` isolated_count === 1) is about that machine's
//     process, not ours, and stays pooled.
//
// Pure and dependency-injected so the decision tests without Electron.

/** Everything the collapse decision needs about one profile's routing. */
export interface HostBackendCollapseOptions {
  /** `HERMES_DESKTOP_ISOLATED_BACKEND=1`: this app wants its own backend. */
  isolated?: boolean
  /** This profile points at its own remote host (connection.json / SSH). */
  profileRemoteOverride?: boolean
  /** The primary profile's backend is itself a remote host. */
  primaryRemoteActive?: boolean
  /**
   * This request MUTATES state the server cannot profile-scope
   * (`connection-config.ts::unscopableMutatingRequest`). The pooled backend's
   * own `HERMES_HOME` is then the only scope there is, so the collapse must
   * not swallow it — and the spawn guard below must not refuse it.
   */
  unscopableRequest?: boolean
}

/**
 * True when a LOCAL profile shares the one host backend instead of spawning
 * its own. False keeps the legacy pooled descriptor — which, for a remote
 * route, never meant a local child in the first place.
 */
export function sharesHostBackend(opts: HostBackendCollapseOptions = {}): boolean {
  if (opts.isolated || opts.unscopableRequest) {
    return false
  }

  return !opts.profileRemoteOverride && !opts.primaryRemoteActive
}

/**
 * Pool key of the one local host backend a remote-primary Desktop runs for
 * "This device" (the registry composite `conn:local::<profile>` form, so it
 * never collides with the v1 route's remote descriptor at the bare key).
 */
export const LOCAL_HOST_POOL_KEY = 'conn:local::default'

/**
 * Pool-key prefix of the process-scoped backend an unscopable mutating REST
 * call keeps. Outside the `conn:local::` namespace on purpose: the registry's
 * per-connection enumerations (session sources, roster, retirement parking)
 * must never mistake it for the profile's chat scope on the host backend.
 */
export const UNSCOPABLE_REST_POOL_PREFIX = 'local-rest::'

/**
 * Which local backend serves a forced-local profile ("This device" while the
 * primary is remote). Every profile shares the one host backend and is scoped
 * per socket/request exactly like a local primary; only the escape hatches of
 * `sharesHostBackend` keep a backend of the profile's own. The host is pinned:
 * no idle or LRU retirement, it hosts cron and bot work nobody is watching.
 */
export function forcedLocalBackend(
  profile: string,
  profilePoolKey: string,
  {
    isolated = false,
    unscopableRequest = false
  }: Pick<HostBackendCollapseOptions, 'isolated' | 'unscopableRequest'> = {}
): { pinned: boolean; poolKey: string; spawnProfile: string } {
  if (isolated) {
    return { pinned: false, poolKey: profilePoolKey, spawnProfile: profile }
  }

  // The host launches as `default`, so its own HERMES_HOME already scopes a
  // default-profile mutation; every other profile needs a process of its own.
  if (unscopableRequest && profile !== 'default') {
    return { pinned: false, poolKey: `${UNSCOPABLE_REST_POOL_PREFIX}${profile}`, spawnProfile: profile }
  }

  return { pinned: true, poolKey: LOCAL_HOST_POOL_KEY, spawnProfile: 'default' }
}

/**
 * The host backend's descriptor as `profile` sees it: the socket names its
 * profile (`/api/ws?profile=`, the backend's per-socket default) and REST
 * scoping reads `profile` like any shared local backend.
 */
export function scopeHostDescriptor<T extends { wsUrl?: string }>(
  connection: T,
  profile: string
): T & { profile: string } {
  // `default` is the host's launch profile: its socket stays unscoped, as a local primary's does.
  if (!connection.wsUrl || profile === 'default') {
    return { ...connection, profile }
  }

  const url = new URL(connection.wsUrl)
  url.searchParams.set('profile', profile)

  return { ...connection, profile, wsUrl: url.toString() }
}

/** Raised when something still tries to start a second local backend. */
export class SecondLocalBackendError extends Error {
  readonly poolKey: string

  constructor(poolKey: string) {
    super(
      `Refusing to start a second local Hermes backend for "${poolKey}": one backend serves every profile on this host. ` +
        'Set HERMES_DESKTOP_ISOLATED_BACKEND=1 for a private backend.'
    )
    this.name = 'SecondLocalBackendError'
    this.poolKey = poolKey
  }
}

/**
 * The single enforcement point for "never spawn a second backend".
 *
 * Routing is what normally prevents a pooled local spawn; this guard sits at
 * the one place a local `hermes serve` child is actually started, so a future
 * caller that reaches it through a path routing does not cover fails loudly
 * instead of quietly reintroducing a process per profile.
 */
export function assertNoSecondLocalBackend(poolKey: string, opts: HostBackendCollapseOptions = {}): void {
  if (sharesHostBackend(opts)) {
    throw new SecondLocalBackendError(poolKey)
  }
}

/**
 * A passive read (background tile reconcile, #103375) may only be served by a
 * backend that already exists: it never cold-starts a child and never
 * refreshes `lastActiveAt`, so an open-but-unviewed tile cannot keep the pool
 * saturated. Callers treat the rejection as "nothing to refresh yet".
 */
export function assertNotPassiveSpawn(passive: boolean, poolKey: string): void {
  if (passive) {
    throw new Error(`Passive read: no warm backend for "${poolKey}"`)
  }
}
