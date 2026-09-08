/** @type {import('next').NextConfig} */
// `/missions` 的舊轉址已於階段 2 移除（doc/mission-vs-plan-design.md §4）：
// 那個網址從此屬於「任務」，不能再指向路徑管理。移除前確認過存取日誌沒有人
// 還在打舊路徑。
const nextConfig = {};

export default nextConfig;
