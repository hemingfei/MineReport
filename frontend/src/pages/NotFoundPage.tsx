import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="empty-state">
      <h2>页面不存在</h2>
      <p>
        <Link to="/">返回研报库</Link>
      </p>
    </div>
  );
}
