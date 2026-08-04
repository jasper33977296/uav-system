export default function Flights() {
  return (
    <div className="page-placeholder">
      <div className="card">
        <h3>架次列表</h3>
        <p>尚未實作（roadmap 2：歷史回放）。</p>
        <p className="hint">
          後端已就緒：<code>GET /api/sessions</code>（含每架次摘要統計）、
          <code>GET /api/sessions/{"{id}"}/track</code>（軌跡＋鏈路時序）。
        </p>
      </div>
    </div>
  );
}
