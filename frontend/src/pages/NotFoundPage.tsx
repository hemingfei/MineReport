import { Link } from "react-router-dom";
import { Compass } from "@phosphor-icons/react";
import { EmptyState } from "../components/EmptyState";

export function NotFoundPage() {
  return (
    <EmptyState
      icon={<Compass />}
      title="页面不存在"
      hint="地址可能输错了，或页面已被移动。"
      action={
        <Link className="btn btn-ghost" to="/">
          返回研报库
        </Link>
      }
    />
  );
}
