import { useState, useRef, useCallback, useEffect, KeyboardEvent } from 'react';
import { useT, MessageKeys } from '../i18n';
import { clearCachedFix, geoErrorKey, geolocationSupported, getBrowserPosition, loadCachedFix } from '../lib/geo';
import type { GeoFailure } from '../lib/geo';
import type { GeoPoint } from '../types';
import styles from './ChatInput.module.css';

interface Props {
  onSend: (text: string) => void;
  onStop: () => void;
  onClear: () => void;
  disabled: boolean;
  /**
   * Called whenever the browser fix changes (acquired or cleared). The parent
   * attaches the current value to the next /chat request — and to nothing else,
   * so a fix never leaks into the message text.
   */
  onLocationChange?: (fix: GeoPoint | null) => void;
}

const PRESET_KEYS = ['preset.1', 'preset.2'] as const;

type GeoStatus = 'idle' | 'locating' | 'ready' | 'error';

export default function ChatInput({ onSend, onStop, onClear, disabled, onLocationChange }: Props) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { t } = useT();

  // --- 阶段四：浏览器定位 ---
  // Four explicit states so the UI can be reasoned about (and demoed):
  //   idle     → button invites a click
  //   locating → spinner, button disabled, no double requests
  //   ready    → coordinates chip + "重新定位"
  //   error    → red dot, red border, one-line explanation under the bar
  const [geoStatus, setGeoStatus] = useState<GeoStatus>('idle');
  const [fix, setFix] = useState<GeoPoint | null>(null);
  const [geoError, setGeoError] = useState<MessageKeys | null>(null);
  const [geoSupported, setGeoSupported] = useState(true);

  // Reuse a recent fix after a page refresh (9-minute TTL in lib/geo.ts) so the
  // user does not have to re-authorise for every reload.
  useEffect(() => {
    setGeoSupported(geolocationSupported());
    const cached = loadCachedFix();
    if (cached) {
      setFix(cached);
      setGeoStatus('ready');
      onLocationChange?.(cached);
    }
    // Runs once on mount — onLocationChange is a stable callback from App.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleLocate = useCallback(async () => {
    if (geoStatus === 'locating' || disabled) return;
    setGeoStatus('locating');
    setGeoError(null);
    try {
      const next = await getBrowserPosition();
      setFix(next);
      setGeoStatus('ready');
      onLocationChange?.(next);
    } catch (err) {
      const failure = err as GeoFailure;
      // A failed re-locate must not keep sending stale coordinates.
      setFix(null);
      setGeoStatus('error');
      setGeoError(geoErrorKey(failure?.code ?? 'unknown'));
      onLocationChange?.(null);
    }
  }, [geoStatus, disabled, onLocationChange]);

  const handleClearLocation = useCallback(() => {
    clearCachedFix();
    setFix(null);
    setGeoError(null);
    setGeoStatus('idle');
    onLocationChange?.(null);
  }, [onLocationChange]);

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [value, disabled, onSend]);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  };

  const handlePreset = (text: string) => {
    if (disabled) return;
    onSend(text);
  };

  const geoLabel =
    geoStatus === 'locating'
      ? t('geo.locating')
      : geoStatus === 'ready'
        ? t('geo.retry')
        : geoStatus === 'error'
          ? t('geo.retry')
          : t('geo.button');

  const coordsTitle = fix ? `${fix.lon}, ${fix.lat}` : undefined;

  return (
    <div className={styles.bar}>
      <div className={styles.presets}>
        {geoSupported && (
          <button
            type="button"
            className={[
              styles.geoBtn,
              geoStatus === 'ready' ? styles.geoBtnReady : '',
              geoStatus === 'error' ? styles.geoBtnError : '',
              geoStatus === 'locating' ? styles.geoBtnBusy : '',
            ].join(' ')}
            onClick={handleLocate}
            disabled={disabled || geoStatus === 'locating'}
            aria-label={t('aria.geolocate')}
            title={geoStatus === 'error' && geoError ? t(geoError) : coordsTitle || t('aria.geolocate')}
          >
            {geoStatus === 'locating' ? (
              <span className={styles.geoSpinner} aria-hidden="true" />
            ) : (
              <span className={styles.geoPin} aria-hidden="true">📍</span>
            )}
            <span className={styles.geoLabel}>{geoLabel}</span>
            {geoStatus === 'error' && <span className={styles.geoDot} aria-hidden="true" />}
          </button>
        )}

        {geoStatus === 'ready' && fix && (
          <span className={styles.geoChip} title={coordsTitle}>
            <span className={styles.geoChipPin} aria-hidden="true">✓</span>
            <span className={styles.geoChipText}>
              {t('geo.ready')}
              {typeof fix.accuracy === 'number' ? ` · ${t('geo.accuracy')} ±${fix.accuracy}m` : ''}
            </span>
            <button
              type="button"
              className={styles.geoChipClear}
              onClick={handleClearLocation}
              aria-label={t('geo.clear')}
              title={t('geo.clear')}
            >
              ×
            </button>
          </span>
        )}

        {PRESET_KEYS.map(key => (
          <button
            key={key}
            className={styles.presetChip}
            onClick={() => handlePreset(t(key as MessageKeys))}
            disabled={disabled}
          >
            {t(key as MessageKeys)}
          </button>
        ))}
      </div>

      {geoStatus === 'error' && geoError && (
        <p className={styles.geoError} role="alert" aria-live="polite">
          {t(geoError)}
        </p>
      )}
      {geoStatus === 'ready' && fix && (
        <p className={styles.geoHint} aria-live="polite">
          {t('geo.hintLocated')}
        </p>
      )}

      <div className={`${styles.inputWrap} ${disabled ? styles.inputDisabled : ''}`}>
        <textarea
          ref={textareaRef}
          className={styles.textarea}
          placeholder={t("chat.placeholder")}
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          rows={1}
          disabled={disabled}
        />
        <button
          className={`${styles.sendBtn} ${(!value.trim() || disabled) ? styles.sendDisabled : ''}`}
          onClick={handleSend}
          disabled={!value.trim() || disabled}
          aria-label={t("aria.send")}
        >
          <svg viewBox="0 0 20 20" fill="none" width="16" height="16">
            <path d="M3 10L17 3l-4 7 4 7L3 10z" fill="currentColor"/>
          </svg>
        </button>
        <button
          className={styles.clearBtn}
          onClick={onClear}
          disabled={disabled}
          aria-label={t("aria.clearHistory")}
          title={t("aria.clearHistory")}
        >
          <svg viewBox="0 0 24 24" fill="none" width="16" height="16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
            <path d="M10 11v6"/>
            <path d="M14 11v6"/>
            <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
          </svg>
        </button>
        {disabled && (
          <button
            className={styles.stopBtn}
            onClick={onStop}
            aria-label={t("aria.stopGeneration")}
            title={t("aria.stopGeneration")}
          >
            <svg viewBox="0 0 20 20" fill="none" width="14" height="14">
              <rect x="4" y="4" width="12" height="12" rx="2" fill="currentColor"/>
            </svg>
          </button>
        )}
      </div>
      <p className={styles.hint}>{t("chat.hint")}</p>
    </div>
  );
}
