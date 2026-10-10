/**
 * 路由级假故障的自动恢复。
 *
 * 两类故障看起来都像「页面加载失败」，但都不是用户的问题，重载即可恢复：
 *
 * 1. **旧 chunk**：每次重建都会换掉哈希文件名。已打开的页面仍持有旧 manifest，
 *    下一次 `import()` 去要不存在的文件，服务器回 404 +`text/javascript`，
 *    浏览器把响应体当模块解析 → 路由边界显示通用失败卡片。
 *
 * 2. **外部改写了 DOM**：浏览器机器翻译（Chrome/Edge 的「翻译为中文」）会把文本
 *    节点替换成 `<font>`。React 之后更新这些被外部改过的节点时抛
 *    `NotFoundError: Failed to execute 'removeChild'` —— 与代码无关，但同样表现为
 *    这张卡片。干净浏览器永远复现不出来，只有在装了翻译的机器上才出现。
 *    （`index.html` 已声明 notranslate 从源头规避，这里仍保留兜底。）
 */

const RELOAD_FLAG = 'dsh.chunkReloadAt';
const RELOAD_COUNT_FLAG = 'dsh.chunkReloadCount';
/** 统计窗口内的重载次数上限。 */
const RELOAD_WINDOW_MS = 60_000;
const MAX_RELOADS_PER_WINDOW = 3;

/** 失败是否属于「重载即可恢复」的那一类。 */
export function isChunkLoadError(error: unknown): boolean {
  const message =
    error instanceof Error ? error.message : typeof error === 'string' ? error : '';
  return /ChunkLoadError|Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|asset not found|Failed to execute 'removeChild' on 'Node'|Failed to execute 'insertBefore' on 'Node'|The node to be removed is not a child of this node/i.test(
    message,
  );
}

/**
 * 重载一次以取得当前 bundle。
 *
 * 之前是「15 秒内只许重载一次」，于是**第二次失败直接落到失败卡片** —— 而第二次失败
 * 恰恰是最常见的：重载后用户立刻再点一次，或重载本身又撞上另一处旧引用。
 * 现在改成窗口内计数：60 秒内最多 3 次，既能自愈连续故障，又不会因构建真损坏而
 * 陷入无限重载循环（超出上限时返回 false，调用方照常显示失败卡片）。
 */
export function reloadForFreshBundle(): boolean {
  try {
    const now = Date.now();
    const previous = Number(window.sessionStorage.getItem(RELOAD_FLAG) ?? '0');
    const previousCount = Number(window.sessionStorage.getItem(RELOAD_COUNT_FLAG) ?? '0');

    const withinWindow = Number.isFinite(previous) && now - previous < RELOAD_WINDOW_MS;
    const count = withinWindow && Number.isFinite(previousCount) ? previousCount : 0;

    if (count >= MAX_RELOADS_PER_WINDOW) {
      return false;
    }

    window.sessionStorage.setItem(RELOAD_FLAG, String(now));
    window.sessionStorage.setItem(RELOAD_COUNT_FLAG, String(count + 1));
  } catch {
    // 没有 sessionStorage（隐私模式、存储被禁）：无法检测循环，因此拒绝重载。
    return false;
  }
  window.location.reload();
  return true;
}
