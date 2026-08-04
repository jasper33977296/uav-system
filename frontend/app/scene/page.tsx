export default function Scene() {
  return (
    <div className="page-placeholder">
      <div className="card">
        <h3>場景管理</h3>
        <p>尚未實作（roadmap 4：干擾區編輯）。</p>
        <p className="hint">
          後端已就緒：<code>POST/DELETE /api/zones</code>。地圖畫圈 → 設定半徑與
          severity；模擬模式下改動最多 30 秒後生效（backend 每 30 秒重讀）。
        </p>
      </div>
    </div>
  );
}
