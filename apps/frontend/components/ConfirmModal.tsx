"use client";
/** 危險操作確認 modal：取代 window.confirm（可承載結構化說明與樣式，
 * 且不被瀏覽器「阻止此網頁顯示對話方塊」機制吃掉）。 */
import { useEffect } from "react";

interface Props {
  title: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onClose: () => void;
  children: React.ReactNode;
}

export default function ConfirmModal({
  title, confirmLabel = "確認刪除", onConfirm, onClose, children,
}: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal confirm-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="name">{title}</span>
        </div>
        <div className="modal-text">{children}</div>
        <div className="modal-actions">
          <button className="btn-plain" onClick={onClose}>取消</button>
          <button className="btn-danger" onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
