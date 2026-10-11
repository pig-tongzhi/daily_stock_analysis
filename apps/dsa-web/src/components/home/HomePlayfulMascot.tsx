import type React from 'react';

/**
 * 首页问候条里的小盆栽吉祥物。
 *
 * 刻意用内联 SVG 手绘，而不是 emoji 或外部图片：
 * - 需求明确禁止新增依赖与外部图片资源；
 * - emoji 在不同系统上渲染差异很大，会破坏“卡通友好但克制”的观感。
 *
 * 颜色走 currentColor / 主题令牌，避免把奶油色写死而在深色主题下过曝。
 * 这里的三档绿色是吉祥物自身的枝叶颜色（属于插画，不属于主题系统），
 * 深色主题下由 CSS 变量 --playful-mint 提亮，保证不会糊在深底上。
 */
export const HomePlayfulMascot: React.FC<{ className?: string }> = ({ className }) => (
  <svg
    className={className}
    width="104"
    height="72"
    viewBox="0 0 112 76"
    role="img"
    aria-hidden="true"
    focusable="false"
  >
    {/* 地面阴影：让盆栽“站”在卡片上，而不是浮着 */}
    <ellipse cx="56" cy="70" rx="34" ry="5" fill="currentColor" opacity="0.08" />
    {/* 花盆 */}
    <path d="M38 62 h36 l-5 8 H43 z" fill="#ff9d7a" />
    <rect x="34" y="44" width="44" height="20" rx="8" fill="#ff7f66" />
    <rect x="30" y="40" width="52" height="10" rx="5" fill="#ff9d7a" />
    {/* 叶片 */}
    <path d="M56 40 C56 26 46 22 40 22 C40 32 48 38 56 40 Z" fill="#3ec98f" />
    <path d="M56 40 C56 22 68 16 78 16 C78 30 68 38 56 40 Z" fill="var(--playful-mint, #25c99a)" />
    <line x1="56" y1="40" x2="56" y2="26" stroke="#1aa87c" strokeWidth="2.5" strokeLinecap="round" />
    {/* 金币：象征“看数据”，不指向任何具体数值 */}
    <circle cx="24" cy="18" r="9" fill="var(--playful-sun, #ffc53d)" />
    <text
      x="24"
      y="22.5"
      textAnchor="middle"
      fontSize="11"
      fontWeight="700"
      fill="rgba(0,0,0,0.45)"
      fontFamily="sans-serif"
    >
      ¥
    </text>
    <circle cx="92" cy="40" r="6" fill="var(--playful-grape, #7b61ff)" opacity="0.28" />
    <circle cx="100" cy="24" r="4" fill="#3fa9f5" opacity="0.32" />
  </svg>
);

export default HomePlayfulMascot;
