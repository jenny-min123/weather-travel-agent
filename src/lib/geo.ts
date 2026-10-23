/**
 * Browser geolocation helper (阶段四：前端定位)
 *
 * Wraps `navigator.geolocation.getCurrentPosition` into a promise, adds a
 * short-lived cache and — most importantly — turns the browser's numeric error
 * codes into something we can put in front of a user.
 *
 * Coordinate system: the API returns **WGS84**. It is sent to the backend as
 * WGS84 (`coordSys: 'wgs84'`) and converted to GCJ-02 exactly once on the
 * server (`agents/_geo.py`). Never convert here — double conversion shifts the
 * point by another few hundred metres.
 */
import type { GeoPoint } from '../types';
import type { MessageKeys } from '../i18n';

export type GeoErrorCode =
  | 'unsupported'
  | 'insecure'
  | 'denied'
  | 'unavailable'
  | 'timeout'
  | 'unknown';

export interface GeoFailure {
  code: GeoErrorCode;
  /** Raw browser message — logged only, never shown to the user. */
  detail?: string;
}

/** Cache key + TTL. A fix older than this is re-requested instead of reused. */
const CACHE_KEY = 'eo_geo_fix';
const CACHE_TTL_MS = 9 * 60 * 1000;

/** Bounded wait: a user staring at a spinner is worse than a clear failure. */
const TIMEOUT_MS = 8000;
const MAX_AGE_MS = 60 * 1000;

export function geolocationSupported(): boolean {
  return typeof navigator !== 'undefined' && 'geolocation' in navigator;
}

/**
 * Geolocation is gated on a secure context. `localhost` counts as secure per
 * spec, but an explicit check keeps the error message honest when the app is
 * opened over a LAN IP (`http://192.168.x.x:8080`), where the browser silently
 * refuses to return a position at all.
 */
export function isSecureContextForGeo(): boolean {
  if (typeof window === 'undefined') return false;
  if (window.isSecureContext) return true;
  const host = window.location.hostname;
  return host === 'localhost' || host === '127.0.0.1' || host === '::1';
}

export function loadCachedFix(): GeoPoint | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as GeoPoint;
    if (!Number.isFinite(parsed?.lon) || !Number.isFinite(parsed?.lat)) return null;
    if (Date.now() - (parsed.ts || 0) > CACHE_TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveCachedFix(fix: GeoPoint): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(fix));
  } catch {
    /* private mode / quota — the fix is still usable for this session */
  }
}

export function clearCachedFix(): void {
  try {
    localStorage.removeItem(CACHE_KEY);
  } catch {
    /* ignore */
  }
}

function mapError(err: GeolocationPositionError): GeoFailure {
  switch (err.code) {
    case err.PERMISSION_DENIED:
      return { code: 'denied', detail: err.message };
    case err.POSITION_UNAVAILABLE:
      return { code: 'unavailable', detail: err.message };
    case err.TIMEOUT:
      return { code: 'timeout', detail: err.message };
    default:
      return { code: 'unknown', detail: err.message };
  }
}

/**
 * Ask the browser for one position fix.
 * Resolves with a `GeoPoint`; rejects with a `GeoFailure` (never throws raw).
 */
export function getBrowserPosition(): Promise<GeoPoint> {
  return new Promise((resolve, reject) => {
    if (!geolocationSupported()) {
      reject({ code: 'unsupported' } as GeoFailure);
      return;
    }
    if (!isSecureContextForGeo()) {
      reject({ code: 'insecure' } as GeoFailure);
      return;
    }

    navigator.geolocation.getCurrentPosition(
      position => {
        const fix: GeoPoint = {
          lon: Number(position.coords.longitude.toFixed(6)),
          lat: Number(position.coords.latitude.toFixed(6)),
          accuracy: Number.isFinite(position.coords.accuracy)
            ? Math.round(position.coords.accuracy)
            : undefined,
          coordSys: 'wgs84',
          ts: Date.now(),
        };
        saveCachedFix(fix);
        resolve(fix);
      },
      error => reject(mapError(error)),
      { enableHighAccuracy: true, timeout: TIMEOUT_MS, maximumAge: MAX_AGE_MS },
    );
  });
}

/** Error code → i18n key. Keeps the copy in one place per language. */
export function geoErrorKey(code: GeoErrorCode): MessageKeys {
  const keys: Record<GeoErrorCode, MessageKeys> = {
    unsupported: 'geo.error.unsupported',
    insecure: 'geo.error.insecure',
    denied: 'geo.error.denied',
    unavailable: 'geo.error.unavailable',
    timeout: 'geo.error.timeout',
    unknown: 'geo.error.unknown',
  };
  return keys[code] ?? 'geo.error.unknown';
}
