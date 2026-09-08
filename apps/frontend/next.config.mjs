/** @type {import('next').NextConfig} */
// `/missions` 是「路徑管理」的舊網址（doc/mission-vs-plan-design.md §3.5）。
// **舊連結不讓它死掉**——同 `/captures` → `/info?tab=captures` 的作法。
// **這是有期限的**：階段 2 會把 `missions` 這個名字拿回來當「任務」，
// 到時候這條轉址要先移除，兩者不能重疊。
const nextConfig = {
  async redirects() {
    return [
      { source: "/missions", destination: "/plans", permanent: false },
      { source: "/missions/:path*", destination: "/plans/:path*", permanent: false },
    ];
  },
};

export default nextConfig;
