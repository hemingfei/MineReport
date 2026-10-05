/** 列表加载骨架：与最终卡片布局同形，避免载入完成时跳动。 */
export function ListSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="skeleton-list" aria-hidden>
      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton-row" key={i}>
          <div className="skeleton sk-70" />
          <div className="skeleton sk-35" />
        </div>
      ))}
    </div>
  );
}
