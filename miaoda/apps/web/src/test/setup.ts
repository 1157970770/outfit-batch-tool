/** jsdom 没实现的浏览器能力，按需补最小实现（只服务单测，不要在生产代码里依赖）。 */

if (!('createObjectURL' in URL)) {
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: () => 'blob:test',
  });
}

Object.defineProperty(URL, 'createObjectURL', {
  configurable: true,
  writable: true,
  value: vi.fn(() => `blob:test-${Math.random().toString(36).slice(2, 8)}`),
});

Object.defineProperty(URL, 'revokeObjectURL', {
  configurable: true,
  writable: true,
  value: vi.fn(),
});

if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({
      matches: false,
      media: '',
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

// React 19 的并发渲染在测试里仍会打印 act 警告，这里只屏蔽 ResizeObserver 缺失这类噪声。
if (!('ResizeObserver' in window)) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  Object.defineProperty(window, 'ResizeObserver', { configurable: true, value: ResizeObserverStub });
}
