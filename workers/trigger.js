// Cloudflare Worker：每天定时触发杭电图书馆云端预约 workflow。
//
// 部署方式（全部在 Cloudflare 控制台完成，无需命令行）：
//   1. dash.cloudflare.com → Workers & Pages → Create → Worker
//   2. 把本文件内容整段粘进在线编辑器，Deploy
//   3. Settings → Variables and Secrets，添加加密变量：
//        GH_PAT     = fine-grained PAT（Actions: Read and write）
//        TRIGGER_KEY = 随便一串随机字符（用于手动 HTTP 触发时校验）
//        REPO       = TaoTao-319/HDU-Library-CloudBooking
//        WORKFLOW   = main.yml
//   4. Settings → Triggers → Cron Triggers → 添加：20 11 * * *
//      （UTC 11:20 = 北京时间 19:20）
//
// 手动测试：浏览器访问 https://<你的-worker>.workers.dev/?key=<TRIGGER_KEY>
//   返回 {"status":204,"ok":true} 即成功。

export default {
  // Cron 触发入口
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  // 手动触发入口（便于测试）
  async fetch(request, env) {
    const key = new URL(request.url).searchParams.get('key');
    if (!key || key !== env.TRIGGER_KEY) {
      return new Response('forbidden', { status: 403 });
    }
    const result = await dispatch(env);
    return new Response(JSON.stringify(result), {
      headers: { 'content-type': 'application/json' },
    });
  },
};

async function dispatch(env) {
  const repo = env.REPO || 'TaoTao-319/HDU-Library-CloudBooking';
  const workflow = env.WORKFLOW || 'main.yml';
  const response = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.GH_PAT}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'hdu-library-cloud-trigger',
      },
      body: JSON.stringify({ ref: env.REF || 'main' }),
    },
  );
  // GitHub 成功返回 204 No Content
  return { status: response.status, ok: response.ok };
}
