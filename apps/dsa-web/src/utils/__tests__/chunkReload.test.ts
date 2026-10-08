import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { isChunkLoadError, reloadForFreshBundle } from '../chunkReload';

describe('chunkReload', () => {
  const reload = vi.fn();
  const originalLocation = window.location;

  beforeEach(() => {
    window.sessionStorage.clear();
    reload.mockClear();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...originalLocation, reload },
    });
  });

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation });
  });

  describe('isChunkLoadError', () => {
    it.each([
      'Failed to fetch dynamically imported module: /assets/StockScreeningPage-abc.js',
      'Importing a module script failed.',
      'error loading dynamically imported module',
      'ChunkLoadError: Loading chunk 12 failed.',
      'asset not found',
    ])('recognises %s', (message) => {
      expect(isChunkLoadError(new Error(message))).toBe(true);
    });

    it('ignores ordinary render errors', () => {
      expect(isChunkLoadError(new Error('Cannot read properties of undefined'))).toBe(false);
      expect(isChunkLoadError(undefined)).toBe(false);
    });
  });

  describe('reloadForFreshBundle', () => {
    it('reloads once so the page picks up the current manifest', () => {
      expect(reloadForFreshBundle()).toBe(true);
      expect(reload).toHaveBeenCalledTimes(1);
    });

    it('refuses a second reload inside the guard window, so a broken build cannot loop', () => {
      expect(reloadForFreshBundle()).toBe(true);
      expect(reloadForFreshBundle()).toBe(false);
      expect(reloadForFreshBundle()).toBe(false);
      expect(reload).toHaveBeenCalledTimes(1);
    });

    it('reloads again once the guard window has passed', () => {
      vi.useFakeTimers();
      try {
        expect(reloadForFreshBundle()).toBe(true);
        vi.advanceTimersByTime(20_000);
        expect(reloadForFreshBundle()).toBe(true);
        expect(reload).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    });

    it('refuses rather than looping when storage is unavailable', () => {
      // jsdom hands back a fresh Storage wrapper on every property access, so
      // vi.spyOn(window.sessionStorage, ...) silently targets a different object
      // than the code reads. Replace the property itself instead.
      const blocked = {
        getItem: () => {
          throw new Error('storage blocked');
        },
        setItem: () => {
          throw new Error('storage blocked');
        },
      };
      const original = Object.getOwnPropertyDescriptor(window, 'sessionStorage');
      Object.defineProperty(window, 'sessionStorage', {
        configurable: true,
        value: blocked,
      });
      try {
        expect(reloadForFreshBundle()).toBe(false);
        expect(reload).not.toHaveBeenCalled();
      } finally {
        if (original) {
          Object.defineProperty(window, 'sessionStorage', original);
        }
      }
    });
  });
});
