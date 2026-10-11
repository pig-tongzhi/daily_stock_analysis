/**
 * 卡通友好（playful）风格的可复用基元。
 *
 * 全部来自首页 `HomeDashboard` 里原本页面私有的写法，抽出来的目的只有一个：
 * 其它页面不用再复制 Tailwind 类名串，直接引用这里的组件即可。
 *
 * 设计约束（与 index.css 的 `--playful-*` 令牌一一对应）：
 * - 浅色主题是暖奶油纸，深色主题是「刻意偏暖的深灰」，不是冷黑；
 * - 卡片底色仍走既有的中性 card 色，暖色只做边框、阴影、点缀色；
 * - 涨跌语义沿用 A 股习惯：红涨绿跌（`tone="up"` / `tone="down"`）。
 *
 * 用法示例：
 * ```tsx
 * <PlayfulPage>
 *   <PlayfulSection>
 *     <PlayfulCard>
 *       <PlayfulSectionTitle emoji="⭐" title="自选股" meta="12 只" />
 *       <PlayfulStatGrid>
 *         <PlayfulStat label="命中率" value="63.2%" tone="grape" />
 *       </PlayfulStatGrid>
 *     </PlayfulCard>
 *   </PlayfulSection>
 * </PlayfulPage>
 * ```
 */
export * from './PlayfulPage';
export * from './PlayfulCard';
export * from './PlayfulSectionTitle';
export * from './PlayfulStat';
export * from './PlayfulChip';
export * from './PlayfulList';
export * from './PlayfulTrack';
export * from './PlayfulEmptyState';

/**
 * 复用首页已有的内联 SVG 吉祥物，不另造第二个。
 * 页面级问候条可以直接 `<PlayfulMascot className="h-[72px] w-[104px]" />`。
 */
export { HomePlayfulMascot, HomePlayfulMascot as PlayfulMascot } from '../home/HomePlayfulMascot';
